/**
 * The promises this extension makes to its users, as tests.
 *
 * "We store nothing" and "we never see your session" are the entire reason
 * this is an extension rather than a hosted web app. They are structural
 * properties, so they should fail a build rather than a code review.
 */

import { readFileSync, readdirSync, statSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

const ROOT = new URL("..", import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, "$1");

function sourceFiles(dir: string, acc: string[] = []): string[] {
  for (const name of readdirSync(dir)) {
    const full = join(dir, name);
    if (statSync(full).isDirectory()) sourceFiles(full, acc);
    // .tsx too: once the UI moved to components, most of the code that could
    // plausibly touch a cookie or a remote host stopped being .ts.
    else if (full.endsWith(".ts") || full.endsWith(".tsx")) acc.push(full);
  }
  return acc;
}

/** Strip comments so a doc comment mentioning an API isn't a false positive. */
function code(path: string): string {
  return readFileSync(path, "utf8")
    .replace(/\/\*[\s\S]*?\*\//g, "")
    .replace(/^\s*\/\/.*$/gm, "");
}

describe("we never touch the session cookie", () => {
  const files = sourceFiles(join(ROOT, "src"));

  it("finds source files to check", () => {
    expect(files.length).toBeGreaterThan(0);
  });

  it("never calls chrome.cookies or document.cookie", () => {
    const offenders = files.filter((f) => /chrome\.cookies|document\.cookie/.test(code(f)));
    expect(offenders, `cookie access in: ${offenders.join(", ")}`).toEqual([]);
  });

  it("does not request the cookies permission", () => {
    const manifest = JSON.parse(readFileSync(join(ROOT, "manifest.json"), "utf8"));
    expect(manifest.permissions).not.toContain("cookies");
    // Requesting it would also fail Web Store review scrutiny for no benefit:
    // the browser attaches the cookie for us, so we never need to read it.
  });
});

/**
 * The load-bearing claim of the multi-view UI: glancing at your deadlines costs
 * nothing and works with no API key. It stops being true the moment one view
 * reaches for the model "just for this bit", and nothing about that change
 * would look wrong in review — so it is pinned here instead.
 */
describe("the data views cost no tokens", () => {
  const VIEWS = join(ROOT, "src/ui/views");

  it("only the chat view talks to the LLM", () => {
    const offenders = readdirSync(VIEWS)
      .filter((f) => f !== "Chat.tsx" && f !== "Setup.tsx")
      .filter((f) => /llm\/gemini/.test(code(join(VIEWS, f))));
    expect(
      offenders,
      `these views import the LLM, so they would need an API key: ${offenders.join(", ")}`,
    ).toEqual([]);
  });

  it("Setup imports the LLM only to list models, never to generate", () => {
    // Setup does need it — that is how "which models does this key have?" gets
    // answered — but calling ask() there would put a paid round trip behind
    // opening a settings tab.
    expect(code(join(VIEWS, "Setup.tsx"))).not.toMatch(/\bask\(/);
  });

  it("views read data through the shared tool layer, not the network", () => {
    // A view that fetched Brightspace directly could drift from the rules the
    // tools enforce, and "the honest answer" would depend on which surface you
    // happened to look at.
    const offenders = readdirSync(VIEWS).filter((f) => /\bfetch\(/.test(code(join(VIEWS, f))));
    expect(offenders, `direct fetch in: ${offenders.join(", ")}`).toEqual([]);
  });
});

describe("hosts are declared narrowly and granted per school", () => {
  const manifest = JSON.parse(readFileSync(join(ROOT, "manifest.json"), "utf8"));

  it("requires only the LLM host up front", () => {
    // School hosts are OPTIONAL: a McMaster student should never be asked for
    // access to Carleton's site, and a minimal required set is what Web Store
    // review should see.
    expect(new Set(manifest.host_permissions)).toEqual(
      new Set(["https://generativelanguage.googleapis.com/*"]),
    );
  });

  it("offers every school host the registry knows about", async () => {
    const { allOriginPatterns } = await import("../src/institutions.js");
    const offered = new Set<string>(manifest.optional_host_permissions);
    for (const pattern of allOriginPatterns()) {
      expect(offered, `${pattern} is in the registry but not offered`).toContain(pattern);
    }
  });

  it("offers exactly one non-school host, for the search model", async () => {
    const { allOriginPatterns } = await import("../src/institutions.js");
    const { MODEL_ORIGINS } = await import("../src/rag/embed.js");

    const schools = new Set(allOriginPatterns());
    const extra = (manifest.optional_host_permissions as string[]).filter(
      (h) => !schools.has(h),
    );

    // HuggingFace serves the embedding model's weights (~30MB, once). This is
    // a DELIBERATE exception to "we only talk to your school and Google":
    // bundling the weights would add 30MB to every extension update, so they
    // are fetched instead — and only after the user allows it, as part of a
    // sync they started. Nothing is UPLOADED there; the model comes to the
    // files, not the other way round.
    expect(new Set(extra)).toEqual(new Set(MODEL_ORIGINS));
  });

  it("keeps the model host optional, never required", () => {
    // A student who never indexes a course should never be asked for it.
    const required: string[] = manifest.host_permissions ?? [];
    expect(required.some((h) => h.includes("huggingface"))).toBe(false);
  });

  it("has no server of ours in either list", () => {
    // If a backend is ever added — the shared-key proxy, say — this test
    // should fail and be updated DELIBERATELY, with a decision about what
    // that server would see (docs/07).
    const hosts: string[] = [
      ...(manifest.host_permissions ?? []),
      ...(manifest.optional_host_permissions ?? []),
    ];
    expect(hosts.some((h) => /assistant|herokuapp|fly\.dev|vercel|onrender/.test(h))).toBe(false);
  });
});

describe("institution profiles are honest", () => {
  it("McMaster points at the Brightspace host, not the landing page", async () => {
    const { MCMASTER } = await import("../src/institutions.js");
    // avenue.mcmaster.ca is a static Apache landing page where every /d2l/*
    // path 404s. Login STARTS there and ENDS on the Brightspace host.
    expect(MCMASTER.baseUrl).toBe("https://avenue.cllmcmaster.ca");
    expect(MCMASTER.loginUrl).toContain("avenue.mcmaster.ca");
  });

  it("no school hardcodes another school's brand", async () => {
    const { INSTITUTIONS } = await import("../src/institutions.js");
    // "Avenue to Learn" is McMaster's name alone. Calling Carleton's LMS
    // "Avenue" in text a student reads is simply wrong.
    for (const inst of Object.values(INSTITUTIONS)) {
      if (inst.id !== "mcmaster") expect(inst.lmsName).not.toMatch(/avenue/i);
    }
  });

  it("unprobed capabilities report unverified, never a guess", async () => {
    const { capability, CARLETON } = await import("../src/institutions.js");
    expect(capability(CARLETON, "course_details")).not.toBe("unverified");
    // A key nobody has measured must not inherit another school's answer.
    expect(capability({ ...CARLETON, capabilities: {} }, "classlist")).toBe("unverified");
  });

  it("the client resolves the host at runtime rather than hardcoding one", () => {
    const client = code(join(ROOT, "src/avenue/client.ts"));
    expect(client).not.toMatch(/const BASE_URL\s*=/);
    expect(client).toContain("getCurrentInstitution");
  });
});

describe("the API key stays on the user's machine", () => {
  const files = sourceFiles(join(ROOT, "src"));

  it("is only ever written to chrome.storage.local", () => {
    // storage.sync would push the key to Google's servers and to every other
    // browser the user is signed into. local is the whole point.
    const offenders = files.filter((f) => /chrome\.storage\.sync/.test(code(f)));
    expect(offenders, `storage.sync used in: ${offenders.join(", ")}`).toEqual([]);
  });

  it("is never logged", () => {
    const offenders = files.filter((f) => {
      const src = code(f);
      return /console\.(log|info|warn|error)\([^)]*(apiKey|api_key|getApiKey)/i.test(src);
    });
    expect(offenders, `key reaches a log in: ${offenders.join(", ")}`).toEqual([]);
  });

  it("is not sent anywhere except the declared LLM host", async () => {
    const manifest = JSON.parse(readFileSync(join(ROOT, "manifest.json"), "utf8"));
    const required: string[] = manifest.host_permissions ?? [];
    // The key can only physically reach hosts the manifest allows, so keeping
    // that list to the LLM API is what bounds the exposure.
    expect(required).toEqual(["https://generativelanguage.googleapis.com/*"]);
  });
});

describe("every registered tool is callable", () => {
  it("has a handler, a description, and a schema", async () => {
    const { TOOLS } = await import("../src/tools/registry.js");
    expect(TOOLS.length).toBeGreaterThanOrEqual(12);
    for (const t of TOOLS) {
      expect(typeof t.handler, `${t.name} handler`).toBe("function");
      expect(t.description.length, `${t.name} description`).toBeGreaterThan(80);
      expect(t.parameters.type, `${t.name} schema`).toBe("object");
    }
  });

  it("tells the model when NOT to assert on missing data", async () => {
    const { TOOLS_BY_NAME } = await import("../src/tools/registry.js");
    // These three carry the honesty rules that cost the most to discover.
    expect(TOOLS_BY_NAME["list_assignments"]!.description.toLowerCase()).toContain("unknown");
    expect(TOOLS_BY_NAME["list_quizzes"]!.description.toLowerCase()).toContain("unknown");
    expect(TOOLS_BY_NAME["analyze_grade_summary"]!.description.toLowerCase()).toContain(
      "do not estimate",
    );
  });

  it("does not advertise a student roster", async () => {
    const { TOOLS_BY_NAME } = await import("../src/tools/registry.js");
    const desc = TOOLS_BY_NAME["get_class_list"]!.description.toLowerCase();
    expect(desc).toContain("does not return a student roster");
  });
});

describe("the package stays a reasonable download", () => {
  it("ships only the ONNX runtime variant it actually uses", async () => {
    const { readFileSync } = await import("node:fs");
    const { join } = await import("node:path");
    const root = new URL("..", import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, "$1");
    const build = readFileSync(join(root, "build.mjs"), "utf8");

    // Copying every variant made the package 94MB. The jsep (WebGPU) build
    // alone is 25MB and unused, since embeddings run single-threaded on CPU.
    expect(build).not.toMatch(/endsWith\("\.wasm"\)/);
    expect(build).toContain("ort-wasm-simd-threaded.wasm");
    expect(build).not.toContain("jsep.wasm");
  });
});

describe("the search model ships with the extension", () => {
  const manifest = JSON.parse(readFileSync(join(ROOT, "manifest.json"), "utf8"));

  it("needs no third-party host to index files", () => {
    const hosts: string[] = [
      ...(manifest.host_permissions ?? []),
      ...(manifest.optional_host_permissions ?? []),
    ];
    // Fetching weights needed a permission prompt that is unreliable from a
    // side panel — the button silently did nothing — and made a first sync
    // depend on a CDN. Bundling costs package size and buys back the
    // two-host guarantee plus offline indexing.
    expect(hosts.some((h) => h.includes("huggingface"))).toBe(false);
  });

  it("loads the model from the package, never the network", () => {
    const embed = code(join(ROOT, "src/rag/embed.ts"));
    expect(embed).toContain("env.allowRemoteModels = false");
    expect(embed).toContain("localModelPath");
  });

  it("build copies the model under the id the loader resolves", () => {
    const build = readFileSync(join(ROOT, "build.mjs"), "utf8");
    const embed = readFileSync(join(ROOT, "src/rag/embed.ts"), "utf8");
    const dir = build.match(/const MODEL_DIR = "([^"]+)"/)?.[1];
    const id = embed.match(/MODEL_ID = "([^"]+)"/)?.[1];
    // transformers.js resolves by path, so a mismatch is a silent 404 at
    // load time rather than a build error.
    expect(dir, "MODEL_DIR must equal MODEL_ID").toBe(id);
  });
});
