/**
 * Build to dist/ as a loadable unpacked extension.
 *
 * esbuild rather than a framework bundler: the extension is a service worker
 * plus one panel, and a heavier toolchain would add config to maintain without
 * doing anything this needs.
 *
 * Two sets of assets are copied rather than bundled:
 *
 *   - pdf.worker.mjs — pdf.js parses in a worker, and MV3 will not let us load
 *     that worker from a CDN. It has to sit inside the package.
 *   - onnxruntime-web's .wasm — same reason. Fetching WASM from a remote host
 *     would need both a CSP exemption and another host permission; shipping
 *     the ~10MB locally avoids both.
 *
 * Model WEIGHTS are the one thing still fetched at runtime (from HuggingFace),
 * because bundling them would add ~30MB to every extension update.
 */
import * as esbuild from "esbuild";
import { cp, mkdir, readdir, rm } from "node:fs/promises";
import { existsSync } from "node:fs";

const watch = process.argv.includes("--watch");
const outdir = "dist";

await rm(outdir, { recursive: true, force: true });
await mkdir(outdir, { recursive: true });

const options = {
  entryPoints: {
    "background/worker": "src/background/worker.ts",
    "ui/sidepanel": "src/ui/sidepanel.ts",
  },
  outdir,
  bundle: true,
  format: "esm",
  target: "chrome116",
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
  await cp("src/ui/sidepanel.css", `${outdir}/ui/sidepanel.css`);

  // pdf.js worker
  await mkdir(`${outdir}/vendor`, { recursive: true });
  await cp("node_modules/pdfjs-dist/build/pdf.worker.min.mjs", `${outdir}/vendor/pdf.worker.mjs`);

  // onnxruntime WASM, kept local so nothing executable is fetched at runtime.
  //
  // ONLY the plain SIMD build (~12MB). onnxruntime-web also ships `jsep`
  // (+25MB, WebGPU), `jspi` and `asyncify` variants; copying all four made the
  // package 94MB for no benefit, since embeddings run single-threaded on CPU
  // here. If WebGPU is ever wanted, add jsep back deliberately and re-measure.
  const ortDist = "node_modules/onnxruntime-web/dist";
  const ORT_FILES = ["ort-wasm-simd-threaded.wasm", "ort-wasm-simd-threaded.mjs"];
  if (existsSync(ortDist)) {
    await mkdir(`${outdir}/vendor/ort`, { recursive: true });
    const available = await readdir(ortDist);
    for (const file of ORT_FILES) {
      if (available.includes(file)) {
        await cp(`${ortDist}/${file}`, `${outdir}/vendor/ort/${file}`);
      }
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
