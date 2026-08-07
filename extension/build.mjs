/**
 * Build to dist/ as a loadable unpacked extension.
 *
 * esbuild rather than a framework bundler: the extension is a service worker
 * plus one panel, and a heavier toolchain would add config to maintain without
 * doing anything this needs.
 */
import * as esbuild from "esbuild";
import { cp, mkdir, rm } from "node:fs/promises";

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
};

async function copyStatic() {
  await cp("manifest.json", `${outdir}/manifest.json`);
  await mkdir(`${outdir}/ui`, { recursive: true });
  await cp("src/ui/sidepanel.html", `${outdir}/ui/sidepanel.html`);
  await cp("src/ui/sidepanel.css", `${outdir}/ui/sidepanel.css`);
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
