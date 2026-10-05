#!/usr/bin/env node
// Zenvi <-> Remotion helper (Node >= 18, ES module; shipped with Zenvi as package data).
//
// Runs inside a user's Remotion project with THAT project's own Remotion packages
// (@remotion/bundler, @remotion/renderer, @remotion/cli/config), resolved with
// createRequire(<project>/package.json). Zenvi never bundles or installs Remotion.
//
//   node helper.mjs probe        --project DIR [--entry src/index.ts]
//   node helper.mjs compositions --project DIR --entry FILE [--props FILE] [--bundle-dir DIR]
//   node helper.mjs render       --project DIR --entry FILE --composition ID --codec prores4444|h264
//                                --output FILE.mov|.mp4 [--props FILE] [--frames A-B] [--concurrency N]
//                                [--bundle-dir DIR]
//   node helper.mjs still        --project DIR --entry FILE --composition ID --frames 0,middle,last
//                                --out-dir DIR [--props FILE] [--bundle-dir DIR]
//
// Protocol: every machine-readable line on stdout is "@@zenvi " + one JSON object:
//   {"event":"hello","helper":1,"node":"v22.11.0","remotion":"4.0.532"}
//   {"event":"progress","stage":"config|bundling|browser|compositions|rendering|encoding|stills",
//    "progress":0..1|null,"message":"..."}
//   {"event":"warning","message":"..."}
//   {"event":"result", ...}                     (exactly one, on success)
//   {"event":"error","code":"...","message":"..."}
// Anything else on stdout/stderr is Remotion's own logging (Zenvi keeps the tail for error reports).
// Exit codes: 0 success, 2 handled error (an "error" event was printed), 130 cancelled.

import fs from 'node:fs';
import {createRequire} from 'node:module';
import path from 'node:path';
import {fileURLToPath} from 'node:url';

const HELPER_VERSION = 1;
const PREFIX = '@@zenvi ';
const LOG_LEVEL = 'error';

const emit = (event) => {
  process.stdout.write(PREFIX + JSON.stringify(event) + '\n');
};

class HelperError extends Error {
  constructor(code, message) {
    super(message);
    this.code = code;
  }
}

// ---------------------------------------------------------------------------------------------
// Arguments
// ---------------------------------------------------------------------------------------------

export const parseArgs = (argv) => {
  const [command, ...rest] = argv;
  const opts = {};
  for (let i = 0; i < rest.length; i++) {
    const arg = rest[i];
    if (!arg.startsWith('--')) {
      throw new HelperError('INVALID_ARGUMENT', `unexpected argument ${JSON.stringify(arg)}`);
    }
    const eq = arg.indexOf('=');
    if (eq > 2) {
      opts[arg.slice(2, eq)] = arg.slice(eq + 1);
      continue;
    }
    const key = arg.slice(2);
    const next = rest[i + 1];
    if (next === undefined || next.startsWith('--')) {
      opts[key] = true;
    } else {
      opts[key] = next;
      i++;
    }
  }
  return {command, opts};
};

const required = (opts, key) => {
  const value = opts[key];
  if (typeof value !== 'string' || !value) {
    throw new HelperError('INVALID_ARGUMENT', `--${key} is required`);
  }
  return value;
};

export const parseFrameRange = (value) => {
  if (value === undefined || value === null || value === true || value === '') {
    return null;
  }
  const text = String(value).trim();
  const m = /^(\d+)(?:-(\d+)?)?$/.exec(text);
  if (!m) {
    throw new HelperError('INVALID_ARGUMENT', `--frames must be N or A-B, got ${JSON.stringify(text)}`);
  }
  const start = Number(m[1]);
  if (m[2] === undefined && !text.includes('-')) {
    return start;
  }
  if (m[2] === undefined) {
    return [start, null];
  }
  const end = Number(m[2]);
  if (end < start) {
    throw new HelperError('INVALID_ARGUMENT', `--frames ${text}: the end comes before the start`);
  }
  return [start, end];
};

export const CODECS = {
  prores4444: {
    codec: 'prores',
    proResProfile: '4444',
    pixelFormat: 'yuva444p10le',
    imageFormat: 'png',
    extension: '.mov',
  },
  h264: {
    codec: 'h264',
    crf: 18,
    pixelFormat: 'yuv420p',
    imageFormat: 'jpeg',
    jpegQuality: 95,
    colorSpace: 'bt709',
    extension: '.mp4',
  },
};

const readProps = (file) => {
  if (!file || file === true) {
    return {};
  }
  let parsed;
  try {
    parsed = JSON.parse(fs.readFileSync(file, 'utf8'));
  } catch (err) {
    throw new HelperError('INVALID_ARGUMENT', `could not read the props file ${file}: ${err.message}`);
  }
  if (parsed === null || typeof parsed !== 'object' || Array.isArray(parsed)) {
    throw new HelperError('INVALID_ARGUMENT', 'input props must be a JSON object');
  }
  return parsed;
};

// ---------------------------------------------------------------------------------------------
// The project's own Remotion
// ---------------------------------------------------------------------------------------------

const makeRequire = (projectDir) => {
  const pkgJson = path.join(projectDir, 'package.json');
  if (!fs.existsSync(pkgJson)) {
    throw new HelperError('NOT_REMOTION', `${projectDir} has no package.json`);
  }
  const projectRequire = createRequire(pkgJson);
  const requires = [projectRequire];
  try {
    // pnpm and nested installs: @remotion/renderer and @remotion/bundler are @remotion/cli's dependencies
    requires.push(createRequire(projectRequire.resolve('@remotion/cli/package.json')));
  } catch {
    // no @remotion/cli: the project must depend on the packages directly
  }
  const load = (name) => {
    let last = null;
    for (const req of requires) {
      try {
        return req(name);
      } catch (err) {
        if (err && (err.code === 'MODULE_NOT_FOUND' || err.code === 'ERR_PACKAGE_PATH_NOT_EXPORTED')
            && String(err.message).includes(name)) {
          last = err;
          continue;
        }
        throw err;
      }
    }
    throw new HelperError('MISSING_DEPENDENCY',
      `${name} is not installed in ${projectDir}. Run npm install there: Zenvi renders with the project's own `
      + `Remotion. (${last ? String(last.message).split('\n')[0] : 'not found'})`);
  };
  const version = (name) => {
    for (const req of requires) {
      try {
        return req(`${name}/package.json`).version ?? null;
      } catch {
        // try the next resolver
      }
    }
    return null;
  };
  return {load, version};
};

const CONFIG_FILES = ['remotion.config.ts', 'remotion.config.js', 'remotion.config.mjs', 'remotion.config.cjs'];

// Apply remotion.config.* the way the CLI does (esbuild -> CommonJS -> evaluate), so webpack overrides
// (Tailwind, aliases), Config.setPublicDir and Config.setEntryPoint work exactly like `npx remotion render`.
const loadConfig = async (projectDir, rq, warnings) => {
  const file = CONFIG_FILES.map((f) => path.join(projectDir, f)).find((f) => fs.existsSync(f)) ?? null;
  let cliConfig = null;
  try {
    cliConfig = rq.load('@remotion/cli/config');
  } catch {
    cliConfig = null;
  }
  const internals = cliConfig?.ConfigInternals ?? null;
  if (!file || !internals) {
    return {file, internals};
  }
  try {
    const bundler = rq.load('@remotion/bundler');
    const esbuild = bundler.BundlerInternals?.esbuild ?? rq.load('esbuild');
    const tsconfig = path.join(projectDir, 'tsconfig.json');
    const result = await esbuild.build({
      platform: 'node',
      target: 'node16',
      bundle: true,
      format: 'cjs',
      entryPoints: [file],
      tsconfig: file.endsWith('.ts') && fs.existsSync(tsconfig) ? tsconfig : undefined,
      absWorkingDir: projectDir,
      outfile: 'bundle.js',
      write: false,
      packages: 'external',
      logLevel: 'silent',
    });
    const code = new TextDecoder().decode(result.outputFiles[0].contents);
    const configRequire = createRequire(file);
    const module = {exports: {}};
    const cwd = process.cwd();
    process.chdir(projectDir);
    try {
      // eslint-disable-next-line no-new-func
      new Function('require', 'module', 'exports', '__filename', '__dirname', code)(
        configRequire, module, module.exports, file, projectDir);
    } finally {
      process.chdir(cwd);
    }
    return {file, internals};
  } catch (err) {
    warnings.push(`could not apply ${path.basename(file)} (${String(err.message).split('\n')[0]}); `
      + 'rendering with Remotion\'s defaults');
    return {file, internals: null};
  }
};

const configValue = (fn, fallback = null) => {
  try {
    const value = fn();
    return value === undefined ? fallback : value;
  } catch {
    return fallback;
  }
};

const optionValue = (rq, name) => configValue(() => {
  const client = rq.load('@remotion/renderer/client');
  return client.BrowserSafeApis.options[name].getValue({commandLine: {}}).value;
});

// The CLI loads <project>/.env (or Config.setDotEnvLocation) into the render; so do we.
const readEnv = (projectDir, internals) => {
  const location = configValue(() => internals?.getDotEnvLocation?.()) ?? '.env';
  const file = path.resolve(projectDir, location);
  const env = {};
  if (!fs.existsSync(file)) {
    return env;
  }
  for (const raw of fs.readFileSync(file, 'utf8').split(/\r?\n/)) {
    const line = raw.trim();
    if (!line || line.startsWith('#')) {
      continue;
    }
    const m = /^(?:export\s+)?([\w.-]+)\s*=\s*(.*)$/.exec(line);
    if (!m) {
      continue;
    }
    let value = m[2].trim();
    if ((value.startsWith('"') && value.endsWith('"')) || (value.startsWith('\'') && value.endsWith('\''))) {
      value = value.slice(1, -1);
    }
    env[m[1]] = value;
  }
  return env;
};

const setup = async (opts) => {
  const projectDir = path.resolve(required(opts, 'project'));
  const rq = makeRequire(projectDir);
  const warnings = [];
  const remotionVersion = rq.version('remotion');
  emit({event: 'hello', helper: HELPER_VERSION, node: process.version, remotion: remotionVersion});
  const major = Number(String(remotionVersion ?? '0').split('.')[0]);
  if (remotionVersion && major < 4) {
    throw new HelperError('UNSUPPORTED', `Remotion ${remotionVersion} is too old: Zenvi needs Remotion 4 or newer`);
  }
  emit({event: 'progress', stage: 'config', progress: null, message: 'Reading the project configuration'});
  const config = await loadConfig(projectDir, rq, warnings);
  const internals = config.internals;
  const configEntry = configValue(() => internals?.getEntryPoint?.());
  const entryArg = typeof opts.entry === 'string' && opts.entry ? opts.entry : configEntry;
  const entry = entryArg ? path.resolve(projectDir, entryArg) : null;
  return {
    projectDir,
    rq,
    warnings,
    remotionVersion,
    entry,
    configFile: config.file,
    webpackOverride: configValue(() => internals?.getWebpackOverrideFn?.(), null),
    rspackOverride: configValue(() => internals?.getRspackOverrideFn?.(), null),
    bundlerOverride: configValue(() => internals?.getBundlerOverrideFn?.(), null),
    publicDir: optionValue(rq, 'publicDirOption'),
    rspack: Boolean(optionValue(rq, 'rspackOption')),
    envVariables: readEnv(projectDir, internals),
  };
};

const ensureEntry = (ctx) => {
  if (!ctx.entry) {
    throw new HelperError('NO_ENTRY', `${ctx.projectDir} has no entry point; pass --entry`);
  }
  if (!fs.existsSync(ctx.entry)) {
    throw new HelperError('NO_ENTRY', `the entry point ${ctx.entry} does not exist`);
  }
};

// A bundle is reused when bundle-dir already holds one (Zenvi keys bundle-dir by a fingerprint of
// the sources). A new one is built next to it and renamed into place, so a half-written bundle is
// never picked up.
const getServeUrl = async (ctx, bundleDir) => {
  ensureEntry(ctx);
  if (bundleDir && fs.existsSync(path.join(bundleDir, 'index.html'))) {
    emit({event: 'progress', stage: 'bundling', progress: 1, message: 'Using the cached bundle'});
    const now = new Date();
    try {
      fs.utimesSync(bundleDir, now, now);
    } catch {
      // only a hint for the cache cleanup
    }
    return bundleDir;
  }
  const {bundle} = ctx.rq.load('@remotion/bundler');
  const target = bundleDir ? `${bundleDir}.partial-${process.pid}-${Date.now()}` : null;
  if (target) {
    fs.mkdirSync(path.dirname(target), {recursive: true});
  }
  emit({event: 'progress', stage: 'bundling', progress: 0, message: 'Bundling the Remotion project'});
  let lastReported = -1;
  let out;
  try {
    const options = {
      entryPoint: ctx.entry,
      onProgress: (p) => {
        const value = Math.max(0, Math.min(1, Number(p) / 100));
        if (value - lastReported >= 0.02 || value >= 1) {
          lastReported = value;
          emit({event: 'progress', stage: 'bundling', progress: value, message: 'Bundling the Remotion project'});
        }
      },
      webpackOverride: ctx.webpackOverride ?? ((c) => c),
      outDir: target,
      enableCaching: true,
      rootDir: ctx.projectDir,
      publicDir: ctx.publicDir ?? null,
      ignoreRegisterRootWarning: false,
      onPublicDirCopyProgress: () => {},
      onSymlinkDetected: () => {},
      symlinkPublicDir: process.platform !== 'win32',
    };
    if (ctx.rspackOverride) {
      options.rspackOverride = ctx.rspackOverride;
    }
    if (ctx.bundlerOverride) {
      options.bundlerOverride = ctx.bundlerOverride;
    }
    if (ctx.rspack) {
      options.rspack = true;
    }
    out = await bundle(options);
  } catch (err) {
    if (target) {
      fs.rmSync(target, {recursive: true, force: true});
    }
    throw new HelperError('BUNDLE_FAILED', `bundling ${path.relative(ctx.projectDir, ctx.entry)} failed: ${err.message}`);
  }
  if (!bundleDir) {
    return out;
  }
  try {
    fs.renameSync(out, bundleDir);
  } catch (err) {
    fs.rmSync(out, {recursive: true, force: true});
    if (!fs.existsSync(path.join(bundleDir, 'index.html'))) {
      throw err;
    }
    // another render finished the same bundle first: use it
  }
  return bundleDir;
};

const ensureBrowser = async (ctx) => {
  const {ensureBrowser: ensure} = ctx.rq.load('@remotion/renderer');
  if (typeof ensure !== 'function') {
    return;
  }
  emit({event: 'progress', stage: 'browser', progress: null, message: 'Checking the headless browser'});
  let lastReported = -1;
  await ensure({
    logLevel: LOG_LEVEL,
    onBrowserDownload: () => ({
      version: null,
      onProgress: ({percent, downloadedBytes, totalSizeInBytes, alreadyAvailable}) => {
        if (alreadyAvailable) {
          return;
        }
        let value = Number(percent);
        if (!(value >= 0)) {
          value = totalSizeInBytes ? downloadedBytes / totalSizeInBytes : 0;
        }
        if (value > 1) {
          value /= 100;
        }
        if (value - lastReported >= 0.02 || value >= 1) {
          lastReported = value;
          const mb = (bytes) => (bytes / 1e6).toFixed(0);
          emit({
            event: 'progress',
            stage: 'browser',
            progress: Math.min(1, value),
            message: `Downloading the headless browser (${mb(downloadedBytes)} of ${mb(totalSizeInBytes)} MB)`,
          });
        }
      },
    }),
  });
};

const browserLogs = [];
const onBrowserLog = (log) => {
  if (log && (log.type === 'error') && browserLogs.length < 20) {
    browserLogs.push(String(log.text).slice(0, 400));
    emit({event: 'log', level: log.type, message: String(log.text).slice(0, 400)});
  }
};

const openBrowser = async (ctx) => {
  const {openBrowser: open} = ctx.rq.load('@remotion/renderer');
  return open('chrome', {logLevel: LOG_LEVEL});
};

const closeBrowser = async (browser) => {
  if (!browser) {
    return;
  }
  try {
    await browser.close({silent: true});
  } catch {
    // already gone
  }
};

const selectComposition = async (ctx, serveUrl, id, inputProps, browser) => {
  const {selectComposition: select} = ctx.rq.load('@remotion/renderer');
  try {
    return await select({
      serveUrl,
      id,
      inputProps,
      envVariables: ctx.envVariables,
      logLevel: LOG_LEVEL,
      puppeteerInstance: browser ?? undefined,
      onBrowserLog,
    });
  } catch (err) {
    if (/Could not find composition|No composition with the ID|not found/i.test(String(err.message))) {
      throw new HelperError('NOT_FOUND', String(err.message).split('\n')[0]);
    }
    throw err;
  }
};

const describeComposition = (c) => ({
  id: c.id,
  width: c.width,
  height: c.height,
  fps: c.fps,
  durationInFrames: c.durationInFrames,
  defaultProps: c.defaultProps ?? {},
  props: c.props ?? c.defaultProps ?? {},
  defaultCodec: c.defaultCodec ?? null,
});

// ---------------------------------------------------------------------------------------------
// Commands
// ---------------------------------------------------------------------------------------------

let cancelRender = null;
let cancelled = false;
let currentPartial = null;
const CANCEL_GRACE_MS = 1500;

// SIGTERM / SIGINT (Zenvi's Cancel): stop Remotion's render, then exit even if Remotion is still busy
// (bundling and still renders have no cancel signal). Exiting runs Remotion's own 'exit' handler,
// which kills its headless Chrome. A half-written output is removed; nothing is ever renamed into place.
const onSignal = () => {
  if (cancelled) {
    return;
  }
  cancelled = true;
  if (cancelRender) {
    cancelRender();
  }
  const timer = setTimeout(() => {
    if (currentPartial) {
      fs.rmSync(currentPartial, {force: true});
    }
    emit({event: 'error', code: 'CANCELLED', message: 'cancelled'});
    process.exit(130);
  }, CANCEL_GRACE_MS);
  timer.unref?.();
};

const commands = {
  async probe(opts) {
    const ctx = await setup(opts);
    const renderer = ctx.rq.version('@remotion/renderer');
    const bundler = ctx.rq.version('@remotion/bundler');
    return {
      remotion: ctx.remotionVersion,
      renderer,
      bundler,
      entry: ctx.entry ? path.relative(ctx.projectDir, ctx.entry).split(path.sep).join('/') : null,
      entryExists: Boolean(ctx.entry && fs.existsSync(ctx.entry)),
      configFile: ctx.configFile ? path.basename(ctx.configFile) : null,
      publicDir: ctx.publicDir,
      warnings: ctx.warnings,
    };
  },

  async compositions(opts) {
    const ctx = await setup(opts);
    const inputProps = readProps(opts.props);
    const serveUrl = await getServeUrl(ctx, typeof opts['bundle-dir'] === 'string' ? opts['bundle-dir'] : null);
    await ensureBrowser(ctx);
    emit({event: 'progress', stage: 'compositions', progress: null, message: 'Reading the compositions'});
    const {getCompositions} = ctx.rq.load('@remotion/renderer');
    const list = await getCompositions(serveUrl, {
      inputProps,
      envVariables: ctx.envVariables,
      logLevel: LOG_LEVEL,
      onBrowserLog,
    });
    return {compositions: list.map(describeComposition), warnings: ctx.warnings, serveUrl};
  },

  async render(opts) {
    const ctx = await setup(opts);
    const id = required(opts, 'composition');
    const codecName = required(opts, 'codec');
    const codec = CODECS[codecName];
    if (!codec) {
      throw new HelperError('INVALID_ARGUMENT', `--codec must be ${Object.keys(CODECS).join(' or ')}`);
    }
    const output = path.resolve(required(opts, 'output'));
    if (path.extname(output).toLowerCase() !== codec.extension) {
      throw new HelperError('INVALID_ARGUMENT', `--output must end in ${codec.extension} for ${codecName}`);
    }
    const inputProps = readProps(opts.props);
    const frameRange = parseFrameRange(opts.frames);
    const concurrency = opts.concurrency && opts.concurrency !== true ? Number(opts.concurrency) : null;
    const serveUrl = await getServeUrl(ctx, typeof opts['bundle-dir'] === 'string' ? opts['bundle-dir'] : null);
    await ensureBrowser(ctx);
    const browser = await openBrowser(ctx);
    const partial = output.slice(0, -codec.extension.length) + '.partial' + codec.extension;
    try {
      const composition = await selectComposition(ctx, serveUrl, id, inputProps, browser);
      const {renderMedia, makeCancelSignal} = ctx.rq.load('@remotion/renderer');
      const {cancelSignal, cancel} = makeCancelSignal();
      cancelRender = cancel;
      if (cancelled) {
        cancel();
      }
      let lastReported = -1;
      const options = {
        serveUrl,
        composition,
        codec: codec.codec,
        outputLocation: partial,
        inputProps,
        imageFormat: codec.imageFormat,
        pixelFormat: codec.pixelFormat,
        frameRange,
        overwrite: true,
        cancelSignal,
        puppeteerInstance: browser,
        envVariables: ctx.envVariables,
        logLevel: LOG_LEVEL,
        onBrowserLog,
        onProgress: ({progress, renderedFrames, encodedFrames, stitchStage}) => {
          const value = Math.max(0, Math.min(1, Number(progress) || 0));
          if (value - lastReported >= 0.01 || value >= 1) {
            lastReported = value;
            emit({
              event: 'progress',
              stage: stitchStage === 'muxing' ? 'encoding' : 'rendering',
              progress: value,
              message: `Rendered ${renderedFrames} frames, encoded ${encodedFrames}`,
              renderedFrames,
              encodedFrames,
            });
          }
        },
      };
      if (concurrency && concurrency > 0) {
        options.concurrency = concurrency;
      }
      for (const key of ['proResProfile', 'crf', 'jpegQuality', 'colorSpace']) {
        if (codec[key] !== undefined) {
          options[key] = codec[key];
        }
      }
      fs.mkdirSync(path.dirname(output), {recursive: true});
      currentPartial = partial;
      await renderMedia(options);
      if (cancelled) {
        throw new HelperError('CANCELLED', 'the render was cancelled');
      }
      fs.renameSync(partial, output);
      currentPartial = null;
      let frames = composition.durationInFrames;
      if (Array.isArray(frameRange)) {
        frames = (frameRange[1] ?? composition.durationInFrames - 1) - frameRange[0] + 1;
      } else if (typeof frameRange === 'number') {
        frames = 1;
      }
      return {
        output,
        codec: codecName,
        width: composition.width,
        height: composition.height,
        fps: composition.fps,
        durationInFrames: frames,
        compositionDurationInFrames: composition.durationInFrames,
        defaultProps: composition.defaultProps ?? {},
        props: composition.props ?? {},
        warnings: ctx.warnings,
      };
    } catch (err) {
      fs.rmSync(partial, {force: true});
      currentPartial = null;
      if (cancelled) {
        throw new HelperError('CANCELLED', 'the render was cancelled');
      }
      throw err;
    } finally {
      cancelRender = null;
      await closeBrowser(browser);
    }
  },

  async still(opts) {
    const ctx = await setup(opts);
    const id = required(opts, 'composition');
    const outDir = path.resolve(required(opts, 'out-dir'));
    const inputProps = readProps(opts.props);
    const wanted = String(opts.frames && opts.frames !== true ? opts.frames : 'middle')
      .split(',').map((s) => s.trim()).filter(Boolean);
    const serveUrl = await getServeUrl(ctx, typeof opts['bundle-dir'] === 'string' ? opts['bundle-dir'] : null);
    await ensureBrowser(ctx);
    const browser = await openBrowser(ctx);
    try {
      const composition = await selectComposition(ctx, serveUrl, id, inputProps, browser);
      const last = Math.max(0, composition.durationInFrames - 1);
      const frames = [];
      for (const w of wanted) {
        const f = w === 'first' ? 0 : w === 'middle' ? Math.floor(last / 2) : w === 'last' ? last : Number(w);
        if (!Number.isInteger(f) || f < 0 || f > last) {
          throw new HelperError('INVALID_ARGUMENT', `frame ${w} is outside 0-${last}`);
        }
        if (!frames.includes(f)) {
          frames.push(f);
        }
      }
      const {renderStill} = ctx.rq.load('@remotion/renderer');
      fs.mkdirSync(outDir, {recursive: true});
      const stills = [];
      for (let i = 0; i < frames.length; i++) {
        if (cancelled) {
          throw new HelperError('CANCELLED', 'cancelled');
        }
        const out = path.join(outDir, `still-${frames[i]}.png`);
        await renderStill({
          serveUrl,
          composition,
          output: out,
          frame: frames[i],
          imageFormat: 'png',
          inputProps,
          overwrite: true,
          puppeteerInstance: browser,
          envVariables: ctx.envVariables,
          logLevel: LOG_LEVEL,
          onBrowserLog,
        });
        stills.push({frame: frames[i], output: out});
        emit({event: 'progress', stage: 'stills', progress: (i + 1) / frames.length,
          message: `Rendered still ${i + 1} of ${frames.length}`});
      }
      return {
        stills,
        width: composition.width,
        height: composition.height,
        fps: composition.fps,
        durationInFrames: composition.durationInFrames,
        defaultProps: composition.defaultProps ?? {},
        props: composition.props ?? {},
        warnings: ctx.warnings,
      };
    } finally {
      await closeBrowser(browser);
    }
  },
};

const main = async () => {
  process.on('SIGTERM', onSignal);
  process.on('SIGINT', onSignal);
  let parsed;
  try {
    parsed = parseArgs(process.argv.slice(2));
    const run = commands[parsed.command];
    if (!run) {
      throw new HelperError('INVALID_ARGUMENT',
        `unknown command ${JSON.stringify(parsed.command)}; use ${Object.keys(commands).join(', ')}`);
    }
    const result = await run(parsed.opts);
    emit({event: 'result', ...result});
    process.exitCode = 0;
  } catch (err) {
    const code = cancelled ? 'CANCELLED' : (err && err.code && typeof err.code === 'string' ? err.code : 'FAILED');
    const message = err && err.message ? String(err.message) : String(err);
    emit({event: 'error', code, message, stack: err && err.stack ? String(err.stack).slice(0, 4000) : null,
      browserLogs});
    process.exitCode = code === 'CANCELLED' ? 130 : 2;
  }
};

const invokedDirectly = (() => {
  try {
    return path.resolve(process.argv[1] ?? '') === path.resolve(fileURLToPath(import.meta.url));
  } catch {
    return true;
  }
})();

// Remotion keeps handles open (its file server, the compositor) for reuse; exit once stdout is flushed.
const flushAndExit = (code) => {
  const timer = setTimeout(() => process.exit(code), 2000);
  process.stdout.write('', () => {
    clearTimeout(timer);
    process.exit(code);
  });
};

if (invokedDirectly) {
  await main();
  flushAndExit(process.exitCode ?? 0);
}
