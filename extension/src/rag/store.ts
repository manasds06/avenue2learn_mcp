/**
 * Local index: IndexedDB in place of the Python side's SQLite + FTS5 + a
 * NumPy vector file.
 *
 * Everything here stays in the user's browser profile. Course materials are
 * instructor intellectual property and the extracted text includes classmates'
 * forum posts, so this database is the reason the extension asks for
 * `unlimitedStorage` and the reason none of it is ever uploaded.
 *
 * Vectors are stored as Float32Array blobs alongside their chunk. At a few
 * thousand chunks a brute-force cosine scan is sub-millisecond, so there is no
 * ANN index to corrupt or tune — the same call the Python side made.
 */

const DB_NAME = "brightspace-assistant";
const DB_VERSION = 1;

export interface StoredDocument {
  /** `${orgUnitId}:${topicId}` — topic ids are only unique within a course. */
  key: string;
  orgUnitId: number;
  topicId: number;
  courseName: string;
  modulePath: string;
  title: string;
  fileName: string;
  mimeType: string;
  lastModified: string | null;
  contentHash: string;
  indexedAt: string;
  extractionQuality: "ok" | "poor";
  pageCount: number | null;
  /** Full extracted text, kept so re-chunking never needs a re-download. */
  text: string;
}

export interface StoredChunk {
  id?: number;
  docKey: string;
  orgUnitId: number;
  /** "p.3" / "slide 14" — what a citation points at. */
  position: string;
  /** Returned to the user. Excludes the context header. */
  text: string;
  /** What was embedded. Includes the header; see chunk.ts. */
  embedded: string;
  tokenCount: number;
  vector?: Float32Array;
}

export interface IndexMeta {
  key: string;
  value: string;
}

function open(): Promise<IDBDatabase> {
  return new Promise((resolve, reject) => {
    const req = indexedDB.open(DB_NAME, DB_VERSION);
    req.onupgradeneeded = () => {
      const db = req.result;

      if (!db.objectStoreNames.contains("documents")) {
        const docs = db.createObjectStore("documents", { keyPath: "key" });
        docs.createIndex("orgUnitId", "orgUnitId");
      }
      if (!db.objectStoreNames.contains("chunks")) {
        const chunks = db.createObjectStore("chunks", { keyPath: "id", autoIncrement: true });
        chunks.createIndex("docKey", "docKey");
        // Course scoping is a HARD filter at query time, so it needs an index.
        chunks.createIndex("orgUnitId", "orgUnitId");
      }
      if (!db.objectStoreNames.contains("meta")) {
        db.createObjectStore("meta", { keyPath: "key" });
      }
    };
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}

function tx<T>(
  storeNames: string | string[],
  mode: IDBTransactionMode,
  fn: (stores: IDBObjectStore[]) => IDBRequest<T> | Promise<T> | void,
): Promise<T> {
  return open().then(
    (db) =>
      new Promise<T>((resolve, reject) => {
        const names = Array.isArray(storeNames) ? storeNames : [storeNames];
        const transaction = db.transaction(names, mode);
        const stores = names.map((n) => transaction.objectStore(n));
        let result: T;

        Promise.resolve(fn(stores))
          .then((r) => {
            if (r && typeof (r as IDBRequest).addEventListener === "function") {
              const req = r as unknown as IDBRequest<T>;
              req.onsuccess = () => (result = req.result);
              req.onerror = () => reject(req.error);
            } else {
              result = r as T;
            }
          })
          .catch(reject);

        transaction.oncomplete = () => {
          db.close();
          resolve(result);
        };
        transaction.onerror = () => {
          db.close();
          reject(transaction.error);
        };
      }),
  );
}

const promisify = <T>(req: IDBRequest<T>): Promise<T> =>
  new Promise((resolve, reject) => {
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });

// --- documents --------------------------------------------------------------

export const docKey = (orgUnitId: number, topicId: number): string =>
  `${orgUnitId}:${topicId}`;

export async function getDocument(
  orgUnitId: number,
  topicId: number,
): Promise<StoredDocument | undefined> {
  return tx("documents", "readonly", ([docs]) =>
    promisify(docs!.get(docKey(orgUnitId, topicId)) as IDBRequest<StoredDocument | undefined>),
  );
}

export async function listDocuments(orgUnitId?: number): Promise<StoredDocument[]> {
  return tx("documents", "readonly", ([docs]) =>
    promisify(
      (orgUnitId === undefined
        ? docs!.getAll()
        : docs!.index("orgUnitId").getAll(orgUnitId)) as IDBRequest<StoredDocument[]>,
    ),
  );
}

/**
 * Replace a document and its chunks atomically.
 *
 * The ORDER matters and is load-bearing: the Python side had a bug where a
 * failed extraction still wrote the document row, so the course looked indexed
 * forever while search returned nothing. Callers must not call this until
 * extraction has actually produced text.
 */
export async function putDocument(doc: StoredDocument, chunks: StoredChunk[]): Promise<void> {
  await tx(["documents", "chunks"], "readwrite", async ([docs, chunkStore]) => {
    const existing = await promisify(
      chunkStore!.index("docKey").getAllKeys(doc.key) as IDBRequest<IDBValidKey[]>,
    );
    for (const key of existing) chunkStore!.delete(key);

    docs!.put(doc);
    for (const chunk of chunks) {
      const { id: _ignored, ...rest } = chunk;
      chunkStore!.add({ ...rest, docKey: doc.key, orgUnitId: doc.orgUnitId });
    }
    return undefined as unknown as void;
  });
}

export async function deleteCourse(orgUnitId: number): Promise<number> {
  return tx(["documents", "chunks"], "readwrite", async ([docs, chunkStore]) => {
    const docKeys = await promisify(
      docs!.index("orgUnitId").getAllKeys(orgUnitId) as IDBRequest<IDBValidKey[]>,
    );
    for (const key of docKeys) docs!.delete(key);

    const chunkKeys = await promisify(
      chunkStore!.index("orgUnitId").getAllKeys(orgUnitId) as IDBRequest<IDBValidKey[]>,
    );
    for (const key of chunkKeys) chunkStore!.delete(key);

    return docKeys.length;
  });
}

export async function clearAll(): Promise<void> {
  await tx(["documents", "chunks", "meta"], "readwrite", ([docs, chunks, meta]) => {
    docs!.clear();
    chunks!.clear();
    meta!.clear();
  });
}

// --- chunks -----------------------------------------------------------------

/** Loaded whole: a few thousand chunks is nothing, and it keeps search simple. */
export async function loadChunks(orgUnitId?: number): Promise<StoredChunk[]> {
  return tx("chunks", "readonly", ([chunks]) =>
    promisify(
      (orgUnitId === undefined
        ? chunks!.getAll()
        : chunks!.index("orgUnitId").getAll(orgUnitId)) as IDBRequest<StoredChunk[]>,
    ),
  );
}

export async function countChunks(orgUnitId?: number): Promise<number> {
  return tx("chunks", "readonly", ([chunks]) =>
    promisify(
      (orgUnitId === undefined
        ? chunks!.count()
        : chunks!.index("orgUnitId").count(orgUnitId)) as IDBRequest<number>,
    ),
  );
}

// --- meta -------------------------------------------------------------------

export async function getMeta(key: string): Promise<string | null> {
  const row = await tx("meta", "readonly", ([meta]) =>
    promisify(meta!.get(key) as IDBRequest<IndexMeta | undefined>),
  );
  return row?.value ?? null;
}

export async function setMeta(key: string, value: string): Promise<void> {
  await tx("meta", "readwrite", ([meta]) => {
    meta!.put({ key, value });
  });
}

/** SHA-256 of the raw bytes: catches a re-upload that resets the timestamp. */
export async function hashBytes(bytes: ArrayBuffer): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, "0")).join("");
}
