/**
 * Build to dist/ as a loadable unpacked extension.
 *
 * esbuild rather than a framework bundler: the extension is a service worker
 * plus one panel, and a heavier toolchain would add config to maintain without
 * doing anything this needs.
 *
 * NOTHING is fetched at runtime. Three sets of assets are copied in:
 *
 *   - pdf.worker.js — pdf.js parses in a worker, and MV3 will not load one
 *     from a CDN. Named .js, not .mjs: Chrome serves extension files by
 *     extension and rejects a module worker fetched from .mjs on MIME grounds.
 *   - onnxruntime-web's .wasm + .mjs — the plain CPU build only. src/rag/embed
 *     pins these by exact path, and this script fails if they drift apart.
 *   - The embedding model (~33MB), fetched into vendor-model/ by
 *     `npm run fetch-model` and kept out of git.
 */
import * as esbuild from "esbuild";
import { cp, mkdir, readFile, readdir, rm } from "node:fs/promises";
import { existsSync } from "node:fs";

const watch = process.argv.includes("--watch");
/** Must match MODEL_ID in src/rag/embed.ts — transformers.js resolves by path. */
const MODEL_DIR = "bge-small-en-v1.5";
const outdir = "dist";

await rm(outdir, { recursive: true, force: true });
await mkdir(outdir, { recursive: true });

const options = {
  entryPoints: {
    "background/worker": "src/background/worker.ts",
    "ui/sidepanel": "src/ui/main.tsx",
  },
  outdir,
  bundle: true,
  format: "esm",
  target: "chrome116",
  jsx: "automatic",
  jsxImportSource: "preact",
  sourcemap: watch ? "inline" : false,
  minify: !watch,
  logLevel: "info",
  // transformers.js reaches for Node built-ins that never run in the browser
  // path; without this the bundle fails to resolve them.
  external: ["node:fs", "node:path", "node:url", "onnxruntime-node", "sharp"],
};

async function copyStatic() {
  await cp("manifest.json", `${outdir}/manifest.json`);

  await mkdir(`${outdir}/ui`, { recursive: true });
  await cp("src/ui/sidepanel.html", `${outdir}/ui/sidepanel.html`);
  // theme.css is tokens only; app.css styles the components from them.
  await cp("src/ui/theme.css", `${outdir}/ui/theme.css`);
  await cp("src/ui/app.css", `${outdir}/ui/app.css`);

  await mkdir(`${outdir}/vendor`, { recursive: true });

  // Optional bundled typefaces. The design specifies Public Sans and JetBrains
  // Mono; an MV3 page may not fetch a remote font, and adding a font host would
  // break the two-host guarantee guarantees.test.ts pins. So drop
  // PublicSans.woff2 and JetBrainsMono.woff2 into vendor-fonts/ to get the
  // specified faces — theme.css falls through to the local stack without them,
  // which is a supported state, not a broken one.
  if (existsSync("vendor-fonts")) {
    await cp("vendor-fonts", `${outdir}/vendor/fonts`, { recursive: true });
  }

  // pdf.js worker
  // Copied as .js, NOT .mjs: Chrome serves extension files by extension, and a
  // module worker fetched from a .mjs URL is rejected on MIME grounds.
  await cp(
    "node_modules/pdfjs-dist/legacy/build/pdf.worker.min.mjs",
    `${outdir}/vendor/pdf.worker.js`,
  );

  // The embedding model itself (~33MB). Bundled rather than fetched: a remote
  // fetch needed a third host permission whose prompt is unreliable from a
  // side panel, and it made the first sync depend on a CDN.
  if (existsSync("vendor-model")) {
    await cp("vendor-model", `${outdir}/vendor/model/${MODEL_DIR}`, { recursive: true });
  }

  // onnxruntime WASM, kept local so nothing executable is fetched at runtime.
  //
  // ONLY the plain SIMD build (~12MB). onnxruntime-web also ships `jsep`
  // (+25MB, WebGPU), `jspi` and `asyncify` variants; copying all four made the
  // package 94MB for no benefit, since embeddings run single-threaded on CPU
  // here. If WebGPU is ever wanted, add jsep back deliberately and re-measure.
  const ortDist = "node_modules/onnxruntime-web/dist";
  const ORT_FILES = ["ort-wasm-simd-threaded.wasm", "ort-wasm-simd-threaded.mjs"];

  await mkdir(`${outdir}/vendor/ort`, { recursive: true });
  const available = existsSync(ortDist) ? await readdir(ortDist) : [];
  for (const file of ORT_FILES) {
    // FAIL THE BUILD on a missing runtime file. Skipping quietly produced a
    // package that installed fine and then failed every single file at sync
    // time with "no available backend found" — a build-time typo surfacing as
    // a runtime mystery.
    if (!available.includes(file)) {
      throw new Error(
        `onnxruntime-web is missing ${file}. Embeddings cannot work without it; ` +
          `run npm install, or update ORT_FILES if the upstream filenames changed.`,
      );
    }
    await cp(`${ortDist}/${file}`, `${outdir}/vendor/ort/${file}`);
  }

  // src/rag/embed.ts pins these by exact path, so a rename there without a
  // change here is a 404 at load time. Keep them in step.
  const embedSrc = await readFile("src/rag/embed.ts", "utf8");
  for (const file of ORT_FILES) {
    if (!embedSrc.includes(file)) {
      throw new Error(`src/rag/embed.ts no longer references ${file}; wasmPaths is out of step.`);
    }
  }
}

if (watch) {
  const ctx = await esbuild.context(options);
  await ctx.watch();
  await copyStatic();
  console.log("watching… load dist/ via chrome://extensions → Load unpacked");
} else {
  await esbuild.build(options);
  await copyStatic();
  console.log(`built → ${outdir}/`);
}
