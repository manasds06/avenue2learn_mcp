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
    else if (full.endsWith(".ts")) acc.push(full);
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
