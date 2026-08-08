/**
 * Fetch the embedding model into vendor-model/, which the build copies into
 * the extension package.
 *
 * The weights are NOT in git. A 33MB binary in history is paid for by every
 * clone, forever, and it changes only when the model does. This script is the
 * trade: one command after checkout, nothing in the repo.
 *
 *     npm run fetch-model
 *
 * Same model the Python side uses (bge-small-en-v1.5, 384-dim), quantized to
 * int8 so it loads fast in WASM.
 */
import { mkdir, stat, writeFile } from "node:fs/promises";
import { existsSync } from "node:fs";
import path from "node:path";

const REPO = "Xenova/bge-small-en-v1.5";
const BASE = `https://huggingface.co/${REPO}/resolve/main`;
const OUT = "vendor-model";

const FILES = [
  "onnx/model_quantized.onnx",
  "tokenizer.json",
  "tokenizer_config.json",
  "config.json",
  "special_tokens_map.json",
];

const force = process.argv.includes("--force");
let downloaded = 0;

for (const rel of FILES) {
  const dest = path.join(OUT, rel);
  await mkdir(path.dirname(dest), { recursive: true });

  if (!force && existsSync(dest) && (await stat(dest)).size > 0) {
    console.log(`have    ${rel}`);
    continue;
  }

  process.stdout.write(`fetch   ${rel} … `);
  const resp = await fetch(`${BASE}/${rel}`, { redirect: "follow" });
  if (!resp.ok) {
    console.log("FAILED");
    console.error(`\n${rel}: HTTP ${resp.status} from ${BASE}/${rel}`);
    process.exit(1);
  }
  const bytes = Buffer.from(await resp.arrayBuffer());
  await writeFile(dest, bytes);
  downloaded++;
  console.log(`${(bytes.length / 1048576).toFixed(1)} MB`);
}

console.log(
  downloaded
    ? `\nFetched ${downloaded} file(s) into ${OUT}/. Run \`npm run build\` next.`
    : `\nAlready present in ${OUT}/. Use --force to re-download.`,
);
