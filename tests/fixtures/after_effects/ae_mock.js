#!/usr/bin/env node
/*
 * Run a Zenvi -> After Effects export against the mock DOM in ae_dom.js and print JSON:
 * {"result": <the script's return value>, "error": <uncaught error or null>, "dump": <what it built>}.
 *
 *   node ae_mock.js script.jsx [--media media.json] [--fonts "Family:Style:PostScript,..."]
 *                   [--no-keylight] [--no-fonts-api] [--no-char-reset] [--project-file x.aep]
 *                   [--zenvi-link]   (define the ZenviLink global, as inside the Zenvi Link panel)
 *
 * The script runs in a realm without the library features ExtendScript (ES3) lacks -- JSON,
 * Array.prototype.indexOf/forEach/map/..., String.prototype.trim, Object.keys, Function.prototype.bind --
 * so a runtime that relied on them fails here as it would in After Effects. #target / #include
 * preprocessor lines are commented out (they are not JavaScript).
 */
"use strict";
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const argv = process.argv.slice(2);
const options = {keylight: true, fontsApi: true, charStyleReset: true, projectFile: null, scriptPath: null,
  zenviLink: false};
let media = {};
let fonts = [];
let script = null;
for (let i = 0; i < argv.length; i++) {
  const a = argv[i];
  if (a === "--media") media = JSON.parse(fs.readFileSync(argv[++i], "utf8"));
  else if (a === "--fonts") fonts = argv[++i].split(",").filter(Boolean).map((s) => s.split(":"));
  else if (a === "--no-keylight") options.keylight = false;
  else if (a === "--no-fonts-api") options.fontsApi = false;
  else if (a === "--no-char-reset") options.charStyleReset = false;
  else if (a === "--project-file") options.projectFile = argv[++i];
  else if (a === "--zenvi-link") options.zenviLink = true;
  else script = a;
}
if (!script) {
  process.stderr.write("usage: node ae_mock.js script.jsx [options]\n");
  process.exit(2);
}
options.scriptPath = path.resolve(script);

const STILL = new Set([".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".gif", ".psd", ".webp", ".svg"]);
const AUDIO = new Set([".mp3", ".wav", ".aac", ".m4a", ".aif", ".aiff", ".flac", ".ogg"]);

const host = {
  options,
  fonts,
  fontInstalled: (ps) => fonts.some((f) => f[2] === ps),
  isFile: (p) => { try { return fs.statSync(p).isFile(); } catch (e) { return false; } },
  isDir: (p) => { try { return fs.statSync(p).isDirectory(); } catch (e) { return false; } },
  resolve: (p) => path.resolve(p),
  dirname: (p) => path.dirname(p),
  basename: (p) => path.basename(p),
  mediaInfo: (p, sequence) => {
    const ext = path.extname(p).toLowerCase();
    const m = media[p] || {};
    const still = !sequence && (m.still !== undefined ? m.still : STILL.has(ext));
    const audio = AUDIO.has(ext);
    return {
      width: m.width || (audio ? 0 : 1920), height: m.height || (audio ? 0 : 1080),
      frameRate: m.fps || 30, duration: still ? 0 : (m.duration !== undefined ? m.duration : 60),
      hasVideo: m.hasVideo !== undefined ? m.hasVideo : !audio, hasAudio: m.hasAudio !== undefined ? m.hasAudio : audio,
      still, alpha: !!m.alpha,
    };
  },
};

const context = vm.createContext({__host: host});
vm.runInContext(fs.readFileSync(path.join(__dirname, "ae_dom.js"), "utf8"), context, {filename: "ae_dom.js"});
vm.runInContext(`(function () {
  var a = ["indexOf", "lastIndexOf", "forEach", "map", "filter", "reduce", "reduceRight", "some", "every",
           "find", "findIndex", "includes", "fill", "flat", "flatMap"], i;
  for (i = 0; i < a.length; i++) { delete Array.prototype[a[i]]; }
  var s = ["trim", "trimStart", "trimEnd", "startsWith", "endsWith", "includes", "padStart", "padEnd", "repeat"];
  for (i = 0; i < s.length; i++) { delete String.prototype[s[i]]; }
  delete Object.keys; delete Object.values; delete Object.entries; delete Object.assign;
  delete Array.isArray; delete Function.prototype.bind; delete Date.now;
  delete Number.isInteger; delete Number.isFinite;
  delete this.JSON;
}());`, context);

if (options.zenviLink) vm.runInContext("var ZenviLink = {CATALOG: {}};", context);

const source = fs.readFileSync(script, "utf8");
const code = source.split("\n").map((line) => (line.startsWith("#") ? "//" + line : line)).join("\n");
let result = null;
let error = null;
try {
  result = vm.runInContext(code, context, {filename: path.basename(script)});
} catch (e) {
  error = String((e && e.stack) || e);
}
const dump = JSON.parse(vm.runInContext("__dump()", context));
process.stdout.write(JSON.stringify({result: typeof result === "string" ? result : null, error, dump}));
