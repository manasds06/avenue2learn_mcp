/**
 * Local embeddings via transformers.js.
 *
 * Same model family the Python side uses (`bge-small-en-v1.5`, 384-dim), run
 * as ONNX in WASM inside the browser. Course content never reaches an
 * embedding API — which is the whole reason a local model was chosen over a
 * hosted one.
 *
 * TWO THINGS ARE FETCHED, AND THEY ARE DIFFERENT:
 *
 *   - The WASM runtime is BUNDLED in this package. Executable code is not
 *     pulled from a CDN at runtime.
 *   - The model WEIGHTS (~30MB, once, then cached by the browser) come from
 *     HuggingFace. Bundling them would add 30MB to every extension update, so
 *     that host is an OPTIONAL permission requested at first sync — the user
 *     agrees to it as part of an action they started.
 *
 * MODEL IDENTITY IS RECORDED WITH THE INDEX. Vectors from different models are
 * not comparable, and silently mixing them produces garbage rankings that look
 * like working search.
 */

import { env, pipeline, type FeatureExtractionPipeline } from "@huggingface/transformers";

import { getMeta, setMeta } from "./store.js";

export const MODEL_ID = "Xenova/bge-small-en-v1.5";
export const EMBED_DIM = 384;

const MODEL_META_KEY = "embed_model";

/** Host permissions needed before weights can be fetched. */
export const MODEL_ORIGINS = [
  "https://huggingface.co/*",
  "https://cdn-lfs.huggingface.co/*",
  "https://cdn-lfs-us-1.huggingface.co/*",
];

let extractor: Promise<FeatureExtractionPipeline> | null = null;

function configure(): void {
  // Point ONNX Runtime at the WASM we shipped, so nothing executable is
  // fetched at runtime and the CSP only needs 'wasm-unsafe-eval'.
  // The typings mark this readonly, but assigning it is the documented way
  // to point ORT at locally bundled WASM.
  const wasm = (env.backends.onnx as { wasm?: Record<string, unknown> }).wasm ?? {};
  wasm["wasmPaths"] = chrome.runtime.getURL("vendor/ort/");
  // Single-threaded: SharedArrayBuffer needs cross-origin isolation headers
  // that an extension page does not have.
  wasm["numThreads"] = 1;
  // Only the plain SIMD build is shipped (see build.mjs), so do not let ORT
  // probe for the jsep/WebGPU variant we deliberately left out.
  wasm["simd"] = true;
  wasm["proxy"] = false;
  (env.backends.onnx as { wasm?: unknown }).wasm = wasm;

  env.allowLocalModels = false;
  env.useBrowserCache = true;
}

export async function hasModelPermission(): Promise<boolean> {
  return chrome.permissions.contains({ origins: MODEL_ORIGINS });
}

/** Must be called from a user gesture — see settings.ts. */
export async function requestModelPermission(): Promise<boolean> {
  return chrome.permissions.request({ origins: MODEL_ORIGINS });
}

export class EmbedError extends Error {}

async function getExtractor(
  onProgress?: (msg: string) => void,
): Promise<FeatureExtractionPipeline> {
  if (!extractor) {
    if (!(await hasModelPermission())) {
      throw new EmbedError(
        "Downloading the search model needs one-time access to huggingface.co. " +
          "Open Setup and allow it, then sync again.",
      );
    }
    configure();
    onProgress?.("Downloading the search model (~30 MB, once)…");

    extractor = pipeline("feature-extraction", MODEL_ID, {
      dtype: "q8",
      progress_callback: (p: { status?: string; progress?: number }) => {
        if (p.status === "progress" && typeof p.progress === "number") {
          onProgress?.(`Downloading the search model… ${Math.round(p.progress)}%`);
        }
      },
    }).catch((err) => {
      extractor = null; // let a later attempt retry rather than caching failure
      throw new EmbedError(`Could not load the search model: ${String(err)}`);
    }) as Promise<FeatureExtractionPipeline>;
  }
  return extractor;
}

/**
 * Embed a batch. Returns unit-normalized vectors, so a dot product IS cosine
 * similarity and retrieval does not have to normalize per query.
 */
export async function embed(
  texts: string[],
  onProgress?: (msg: string) => void,
): Promise<Float32Array[]> {
  if (!texts.length) return [];
  const pipe = await getExtractor(onProgress);

  const out: Float32Array[] = [];
  const BATCH = 16;

  for (let i = 0; i < texts.length; i += BATCH) {
    const batch = texts.slice(i, i + BATCH);
    const result = await pipe(batch, { pooling: "mean", normalize: true });
    const data = result.data as Float32Array;
    const dim = result.dims[result.dims.length - 1] ?? EMBED_DIM;

    for (let j = 0; j < batch.length; j++) {
      out.push(new Float32Array(data.slice(j * dim, (j + 1) * dim)));
    }
    onProgress?.(`Embedding ${Math.min(i + BATCH, texts.length)}/${texts.length}…`);
  }
  return out;
}

export async function embedOne(text: string): Promise<Float32Array> {
  const [vector] = await embed([text]);
  if (!vector) throw new EmbedError("Embedding produced no vector.");
  return vector;
}

/**
 * Guard against a silently mixed index.
 *
 * Changing the model invalidates everything already stored. Reporting that is
 * far better than serving rankings computed across incomparable vectors, which
 * look like working search right up until the answers are wrong.
 */
export async function assertModelMatches(): Promise<void> {
  const stored = await getMeta(MODEL_META_KEY);
  if (stored && stored !== MODEL_ID) {
    throw new EmbedError(
      `The index was built with ${stored}, but this version uses ${MODEL_ID}. ` +
        `Vectors from different models are not comparable — clear the index and re-sync.`,
    );
  }
}

export async function recordModel(): Promise<void> {
  await setMeta(MODEL_META_KEY, MODEL_ID);
}

export async function indexedModel(): Promise<string | null> {
  return getMeta(MODEL_META_KEY);
}
