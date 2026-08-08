/**
 * Local embeddings via transformers.js.
 *
 * Same model family the Python side uses (`bge-small-en-v1.5`, 384-dim), run
 * as ONNX in WASM inside the browser. Course content never reaches an
 * embedding API — which is the whole reason a local model was chosen over a
 * hosted one.
 *
 * NOTHING IS FETCHED AT RUNTIME. Both the WASM runtime and the model weights
 * (~33MB) ship inside the package.
 *
 * An earlier version pulled the weights from HuggingFace behind an optional
 * permission. That was worse on every axis: chrome.permissions.request() is
 * unreliable from a side panel and the button silently did nothing, it added a
 * third host to a manifest whose whole selling point is two, and it made the
 * first sync depend on a CDN being reachable. Bundling costs package size and
 * buys back the two-host guarantee, offline indexing, and one fewer prompt.
 *
 * MODEL IDENTITY IS RECORDED WITH THE INDEX. Vectors from different models are
 * not comparable, and silently mixing them produces garbage rankings that look
 * like working search.
 */

import { env, pipeline, type FeatureExtractionPipeline } from "@huggingface/transformers";

import { getMeta, setMeta } from "./store.js";

/** Directory name under vendor/model/, not a HuggingFace repo id. */
export const MODEL_ID = "bge-small-en-v1.5";
export const EMBED_DIM = 384;

const MODEL_META_KEY = "embed_model";

/**
 * Kept as an empty list so the guarantee tests stay meaningful: if anyone
 * reintroduces a remote model source, they have to add the host here AND to
 * the manifest, and the tests will notice.
 */
export const MODEL_ORIGINS: string[] = [];

let extractor: Promise<FeatureExtractionPipeline> | null = null;

function configure(): void {
  // Point ONNX Runtime at the WASM we shipped, so nothing executable is
  // fetched at runtime and the CSP only needs 'wasm-unsafe-eval'.
  // The typings mark this readonly, but assigning it is the documented way
  // to point ORT at locally bundled WASM.
  const wasm = (env.backends.onnx as { wasm?: Record<string, unknown> }).wasm ?? {};

  // EXPLICIT FILE PATHS, not a directory prefix.
  //
  // With a prefix, ORT picks a variant itself and asked for
  // `ort-wasm-simd-threaded.asyncify.mjs` — the WebGPU build — which is not
  // shipped, so every file failed with "no available backend found". The
  // object form pins the plain CPU build we actually bundle, and a mismatch
  // now surfaces at build time rather than as a 404 mid-sync.
  wasm["wasmPaths"] = {
    wasm: chrome.runtime.getURL("vendor/ort/ort-wasm-simd-threaded.wasm"),
    mjs: chrome.runtime.getURL("vendor/ort/ort-wasm-simd-threaded.mjs"),
  };

  // Single-threaded: SharedArrayBuffer needs cross-origin isolation headers
  // that an extension page does not have.
  wasm["numThreads"] = 1;
  wasm["proxy"] = false;
  (env.backends.onnx as { wasm?: unknown }).wasm = wasm;

  // Load from the package, never the network.
  env.allowLocalModels = true;
  env.allowRemoteModels = false;
  env.localModelPath = chrome.runtime.getURL("vendor/model/");
  env.useBrowserCache = false;
}

/** The model ships with the extension, so there is nothing to grant. */
export async function hasModelPermission(): Promise<boolean> {
  return true;
}

export class EmbedError extends Error {}

async function getExtractor(
  onProgress?: (msg: string) => void,
): Promise<FeatureExtractionPipeline> {
  if (!extractor) {
    configure();
    onProgress?.("Loading the search model…");

    extractor = pipeline("feature-extraction", MODEL_ID, {
      // CPU/WASM explicitly. Left on "auto", transformers.js asks for WebGPU,
      // which is what made ORT reach for the asyncify build in the first
      // place. An extension side panel is not the place to fight over GPU
      // adapters, and a 384-dim model over a few hundred chunks is fast
      // enough on CPU.
      device: "wasm",
      dtype: "q8",
      local_files_only: true,
      progress_callback: (p: { status?: string; progress?: number }) => {
        if (p.status === "progress" && typeof p.progress === "number") {
          onProgress?.(`Loading the search model… ${Math.round(p.progress)}%`);
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
