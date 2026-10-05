"""Fakes for the Remotion handoff tests (classes.handoff.remotion).

* :func:`make_project` writes a small Remotion project (package.json, entry,
  Root.tsx, a config) and, with ``installed=True``, the package.json files
  detection looks for in node_modules.
* :func:`write_fake_remotion` writes a fake Remotion into node_modules --
  CommonJS stand-ins for ``@remotion/bundler``, ``@remotion/renderer`` and
  ``@remotion/cli/config`` -- so ``helper.mjs`` runs under real Node without
  Remotion or Chrome. They log every call (JSON lines) to
  ``$FAKE_REMOTION_LOG`` and read their compositions from
  ``$FAKE_REMOTION_COMPS``.
* :class:`FakeHelper` stands in for ``helper.run_helper`` (no Node at all):
  it answers ``compositions`` / ``still`` / ``render`` and writes the files.
"""

from __future__ import annotations

import json
import os
import shutil
from typing import Any, Dict, List, Optional

COMPOSITIONS = [
    {"id": "TitleCard", "width": 1920, "height": 1080, "fps": 30, "durationInFrames": 90,
     "defaultProps": {"title": "Hello Zenvi", "subtitle": "From Remotion", "accent": "#FF5A36"}},
    {"id": "Scene", "width": 1280, "height": 720, "fps": 30, "durationInFrames": 60, "defaultProps": {}},
]

ROOT_TSX = """import React from 'react';
import {Composition, Folder} from 'remotion';
import {TitleCard} from './TitleCard';
import {Scene} from './Scene';

export const RemotionRoot: React.FC = () => (
  <>
    <Folder name="Graphics">
      <Composition id="TitleCard" component={TitleCard} durationInFrames={90} fps={30} width={1920} height={1080}
        defaultProps={{title: 'Hello Zenvi', subtitle: 'From Remotion', accent: '#FF5A36'}} />
    </Folder>
    <Composition id="Scene" component={Scene} durationInFrames={60} fps={30} width={1280} height={720} />
  </>
);
"""


def make_project(root: str, *, installed: bool = True, version: str = "4.0.532", config: Optional[str] = None,
                 scripts: Optional[dict] = None, extra_deps: Optional[dict] = None) -> str:
    """A Remotion project at *root* (created). Returns root."""
    os.makedirs(os.path.join(root, "src"), exist_ok=True)
    os.makedirs(os.path.join(root, "public"), exist_ok=True)
    deps = {"remotion": "^" + version, "@remotion/cli": "^" + version, "react": "19.1.0", "react-dom": "19.1.0"}
    deps.update(extra_deps or {})
    with open(os.path.join(root, "package.json"), "w") as fh:
        json.dump({"name": "fake-remotion", "private": True, "scripts": scripts or {"studio": "remotion studio"},
                   "dependencies": deps}, fh)
    with open(os.path.join(root, "src", "index.ts"), "w") as fh:
        fh.write("import {registerRoot} from 'remotion';\nimport {RemotionRoot} from './Root';\n"
                 "registerRoot(RemotionRoot);\n")
    with open(os.path.join(root, "src", "Root.tsx"), "w") as fh:
        fh.write(ROOT_TSX)
    with open(os.path.join(root, "src", "TitleCard.tsx"), "w") as fh:
        fh.write("import React from 'react';\n\nexport const TitleCard = ({title}: {title: string}) => "
                 "<div>{title}</div>;\n")
    with open(os.path.join(root, "src", "Scene.tsx"), "w") as fh:
        fh.write("import React from 'react';\n\nexport function Scene() {\n  return <div />;\n}\n")
    if config is not None:
        with open(os.path.join(root, "remotion.config.ts"), "w") as fh:
            fh.write(config)
    if installed:
        for name in ("remotion", "@remotion/renderer", "@remotion/bundler", "@remotion/cli"):
            folder = os.path.join(root, "node_modules", *name.split("/"))
            os.makedirs(folder, exist_ok=True)
            with open(os.path.join(folder, "package.json"), "w") as fh:
                json.dump({"name": name, "version": version}, fh)
    return root


# ---------------------------------------------------------------------------
# A fake Remotion for helper.mjs under real Node
# ---------------------------------------------------------------------------

_LOG_JS = """const fs = require('fs');
const log = (o) => { if (process.env.FAKE_REMOTION_LOG) fs.appendFileSync(process.env.FAKE_REMOTION_LOG, JSON.stringify(o) + '\\n'); };
module.exports = log;
"""

_CLI_CONFIG_JS = """const client = require('@remotion/renderer/client');
let entry = null;
let override = (c) => c;
exports.Config = {
  setEntryPoint: (e) => { entry = e; },
  overrideWebpackConfig: (fn) => { override = fn; },
  setVideoImageFormat: () => {},
  setOverwriteOutput: () => {},
  // render settings live in the renderer's options, like the real @remotion/cli/config
  setDelayRenderTimeoutInMilliseconds: (v) => client.__set('delayRenderTimeoutInMillisecondsOption', v),
  setChromiumOpenGlRenderer: (v) => client.__set('glOption', v),
  setBrowserExecutable: (v) => client.__set('browserExecutableOption', v),
  setChromeMode: (v) => client.__set('chromeModeOption', v),
  setOffthreadVideoCacheSizeInBytes: (v) => client.__set('offthreadVideoCacheSizeInBytesOption', v),
  setChromiumIgnoreCertificateErrors: (v) => client.__set('ignoreCertificateErrorsOption', v),
};
exports.ConfigInternals = {
  getEntryPoint: () => entry,
  getWebpackOverrideFn: () => override,
  getDotEnvLocation: () => null,
};
"""

_BUNDLER_JS = """const fs = require('fs');
const path = require('path');
const os = require('os');
const log = require('./log');
exports.BundlerInternals = {
  esbuild: {
    // the fake config files are plain CommonJS: "transpiling" returns them as they are
    build: async ({entryPoints}) => ({outputFiles: [{contents: Buffer.from(fs.readFileSync(entryPoints[0], 'utf8'))}]}),
  },
};
exports.bundle = async (opts) => {
  const cfg = opts.webpackOverride({});
  log({call: 'bundle', entryPoint: opts.entryPoint, outDir: opts.outDir, publicDir: opts.publicDir,
       marker: cfg.zenviMarker || null, rootDir: opts.rootDir});
  if (process.env.FAKE_REMOTION_BUNDLE_FAIL) throw new Error(process.env.FAKE_REMOTION_BUNDLE_FAIL);
  opts.onProgress(50);
  opts.onProgress(100);
  const out = opts.outDir || fs.mkdtempSync(path.join(os.tmpdir(), 'fake-bundle-'));
  fs.mkdirSync(out, {recursive: true});
  fs.writeFileSync(path.join(out, 'index.html'), '<html></html>');
  return out;
};
"""

_RENDERER_JS = """const fs = require('fs');
const log = require('./log');
const {NoReactInternals} = require('remotion/no-react');
// like the real getCompositions: props come back from the browser with Remotion's special types revived
const comps = () => NoReactInternals.deserializeJSONWithSpecialTypes(process.env.FAKE_REMOTION_COMPS || '[]');
const withProps = (c, inputProps) => ({...c, props: {...(c.defaultProps || {}), ...(inputProps || {})}});
const types = (props) => Object.fromEntries(Object.entries(props || {}).map(
  ([k, v]) => [k, v instanceof Date ? 'date' : typeof v]));
const settings = (o) => ({timeoutInMilliseconds: o.timeoutInMilliseconds ?? null,
  chromiumOptions: o.chromiumOptions ?? null, browserExecutable: o.browserExecutable ?? null,
  chromeMode: o.chromeMode ?? null, offthreadVideoCacheSizeInBytes: o.offthreadVideoCacheSizeInBytes ?? null});
exports.ensureBrowser = async (o) => {
  log({call: 'ensureBrowser', browserExecutable: o.browserExecutable ?? null, chromeMode: o.chromeMode ?? null});
  const d = o.onBrowserDownload({chromeMode: 'headless-shell'});
  d.onProgress({alreadyAvailable: false, percent: 0.5, downloadedBytes: 50e6, totalSizeInBytes: 100e6});
  d.onProgress({alreadyAvailable: false, percent: 1, downloadedBytes: 100e6, totalSizeInBytes: 100e6});
  return {type: 'local-puppeteer-browser'};
};
exports.openBrowser = async (browser, o) => {
  log({call: 'openBrowser', ...settings(o || {})});
  return {close: async () => log({call: 'closeBrowser'})};
};
exports.getCompositions = async (serveUrl, opts) => {
  log({call: 'getCompositions', serveUrl, inputProps: opts.inputProps, env: opts.envVariables,
       propTypes: types(opts.inputProps), ...settings(opts)});
  return comps().map((c) => withProps(c, opts.inputProps));
};
exports.selectComposition = async (o) => {
  const {id, inputProps} = o;
  log({call: 'selectComposition', id, propTypes: types(inputProps), ...settings(o)});
  const c = comps().find((x) => x.id === id);
  if (!c) {
    throw new Error(`Could not find composition with ID ${id}. The following compositions are available: ${comps().map((x) => x.id).join(', ')}`);
  }
  return withProps(c, inputProps);
};
exports.makeCancelSignal = () => {
  const callbacks = [];
  return {cancelSignal: (f) => callbacks.push(f), cancel: () => callbacks.forEach((f) => f())};
};
exports.renderMedia = async (o) => {
  log({call: 'renderMedia', codec: o.codec, proResProfile: o.proResProfile || null, pixelFormat: o.pixelFormat,
       imageFormat: o.imageFormat, crf: o.crf || null, frameRange: o.frameRange, concurrency: o.concurrency || null,
       outputLocation: o.outputLocation, inputProps: o.inputProps, colorSpace: o.colorSpace || null,
       env: o.envVariables, propTypes: types(o.inputProps), ...settings(o)});
  if (process.env.FAKE_REMOTION_FAIL) throw new Error(process.env.FAKE_REMOTION_FAIL);
  for (let i = 1; i <= 4; i++) {
    o.onProgress({progress: i / 4, renderedFrames: i, encodedFrames: i, stitchStage: 'encoding'});
    if (process.env.FAKE_REMOTION_SLOW) {
      fs.writeFileSync(o.outputLocation, 'partial');
      await new Promise((r) => setTimeout(r, 400));
    }
  }
  fs.writeFileSync(o.outputLocation, 'movie');
};
exports.renderStill = async (o) => {
  log({call: 'renderStill', frame: o.frame, output: o.output, imageFormat: o.imageFormat,
       propTypes: types(o.inputProps), ...settings(o)});
  fs.writeFileSync(o.output, 'png');
};
"""

_CLIENT_JS = """const values = {publicDirOption: null, rspackOption: false, delayRenderTimeoutInMillisecondsOption: 30000,
  chromeModeOption: 'headless-shell', headlessOption: true};
const option = (name) => ({getValue: () => ({value: values[name], source: 'config'}),
  setConfig: (v) => { values[name] = v; }});
exports.BrowserSafeApis = {options: new Proxy({}, {get: (_target, name) => option(String(name))})};
exports.__set = (name, v) => { values[name] = v; };
"""

# remotion/no-react: Remotion's special-type JSON (Dates as "remotion-date:<ISO>"; a staticFile token
# needs the browser's window, so deserializing one in Node throws, as in the real package)
_NO_REACT_JS = """const DATE_TOKEN = 'remotion-date:';
const FILE_TOKEN = 'remotion-file:';
exports.NoReactInternals = {
  serializeJSONWithSpecialTypes: ({data, indent, staticBase}) => {
    let customDateUsed = false;
    const serializedString = JSON.stringify(data, function (key, value) {
      const item = this[key];
      if (item instanceof Date) {
        customDateUsed = true;
        return DATE_TOKEN + item.toISOString();
      }
      return value;
    }, indent);
    return {serializedString, customDateUsed, customFileUsed: false, mapUsed: false, setUsed: false};
  },
  deserializeJSONWithSpecialTypes: (text) => JSON.parse(text, (_key, value) => {
    if (typeof value === 'string' && value.startsWith(DATE_TOKEN)) return new Date(value.replace(DATE_TOKEN, ''));
    if (typeof value === 'string' && value.startsWith(FILE_TOKEN)) {
      return `${window.remotion_staticBase}/${value.replace(FILE_TOKEN, '')}`;
    }
    return value;
  }),
};
"""


def write_fake_remotion(root: str, version: str = "4.0.532") -> None:
    """Fake @remotion/* + remotion packages in *root*/node_modules (CommonJS, no Chrome)."""
    nm = os.path.join(root, "node_modules")

    def pkg(name: str, files: Dict[str, str], exports: Optional[dict] = None) -> None:
        folder = os.path.join(nm, *name.split("/"))
        os.makedirs(folder, exist_ok=True)
        meta: Dict[str, Any] = {"name": name, "version": version, "main": "index.js"}
        if exports:
            meta["exports"] = exports
        with open(os.path.join(folder, "package.json"), "w") as fh:
            json.dump(meta, fh)
        for fname, body in files.items():
            with open(os.path.join(folder, fname), "w") as fh:
                fh.write(body)

    pkg("remotion", {"index.js": "module.exports = {};\n", "no-react.js": _NO_REACT_JS},
        exports={".": "./index.js", "./no-react": "./no-react.js", "./package.json": "./package.json"})
    pkg("@remotion/cli", {"index.js": "module.exports = {};\n", "config.js": _CLI_CONFIG_JS},
        exports={".": "./index.js", "./config": "./config.js", "./package.json": "./package.json"})
    pkg("@remotion/bundler", {"index.js": _BUNDLER_JS, "log.js": _LOG_JS})
    pkg("@remotion/renderer", {"index.js": _RENDERER_JS, "client.js": _CLIENT_JS, "log.js": _LOG_JS},
        exports={".": "./index.js", "./client": "./client.js", "./package.json": "./package.json"})


def read_log(path: str) -> List[dict]:
    if not os.path.exists(path):
        return []
    with open(path) as fh:
        return [json.loads(line) for line in fh if line.strip()]


def node_runtime_or_none():
    """A NodeRuntime >= 18 when one is installed (tests that need real Node skip otherwise)."""
    if not shutil.which("node"):
        return None
    try:
        from classes.handoff import node_runtime
        return node_runtime.find_node(18)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# helper.run_helper without Node
# ---------------------------------------------------------------------------

class FakeHelper:
    """Stands in for ``helper.run_helper``: records calls, writes stills/renders, answers like helper.mjs."""

    def __init__(self, compositions: Optional[List[dict]] = None):
        self.compositions = [dict(c) for c in (compositions or COMPOSITIONS)]
        self.calls: List[dict] = []
        self.fail: Optional[Exception] = None          # raised by render
        self.fail_on: str = "render"
        self.during: Optional[Any] = None              # callback(command, kwargs) inside each call

    def _comp(self, cid):
        from classes.handoff.remotion.helper import HelperError
        for c in self.compositions:
            if c["id"] == cid:
                return c
        raise HelperError(f"Could not find composition with ID {cid}", "NOT_FOUND")

    def __call__(self, command, *, project_dir, entry=None, props=None, options=None, on_progress=None,
                 should_cancel=None, timeout=None, runtime=None):
        from classes.handoff.remotion.helper import HelperRun
        opts = dict(options or {})
        self.calls.append({"command": command, "project_dir": project_dir, "entry": entry, "props": props,
                           "options": opts})
        if self.during is not None:
            self.during(command, opts)
        if should_cancel is not None and should_cancel():
            from classes.handoff.jobs import JobCancelled
            raise JobCancelled("cancelled")
        if self.fail is not None and command == self.fail_on:
            raise self.fail
        if on_progress is not None:
            on_progress(0.5, command)
        if command == "compositions":
            return HelperRun(result={"compositions": [dict(c, props=dict(c.get("defaultProps") or {}, **(props or {})))
                                                      for c in self.compositions]}, remotion_version="4.0.532")
        comp = self._comp(opts["composition"])
        if command == "still":
            out_dir = opts["out_dir"]
            os.makedirs(out_dir, exist_ok=True)
            last = max(0, comp["durationInFrames"] - 1)
            stills = []
            for f in sorted({0, last // 2, last}):
                path = os.path.join(out_dir, f"still-{f}.png")
                with open(path, "wb") as fh:
                    fh.write(b"png")
                stills.append({"frame": f, "output": path})
            return HelperRun(result={"stills": stills, "width": comp["width"], "height": comp["height"],
                                     "fps": comp["fps"], "durationInFrames": comp["durationInFrames"],
                                     "defaultProps": comp.get("defaultProps") or {}}, remotion_version="4.0.532")
        if command == "render":
            out = opts["output"]
            with open(out, "wb") as fh:
                fh.write(("render %s %s %s" % (comp["id"], opts["codec"], json.dumps(props, sort_keys=True))).encode())
            return HelperRun(result={"output": out, "codec": opts["codec"], "width": comp["width"],
                                     "height": comp["height"], "fps": comp["fps"],
                                     "durationInFrames": comp["durationInFrames"],
                                     "defaultProps": comp.get("defaultProps") or {}}, remotion_version="4.0.532")
        raise AssertionError(command)

    def commands(self) -> List[str]:
        return [c["command"] for c in self.calls]


__all__ = ["make_project", "write_fake_remotion", "read_log", "node_runtime_or_none", "FakeHelper", "COMPOSITIONS",
           "ROOT_TSX"]
