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

  it("offers exactly the school hosts the registry knows about", async () => {
    const { allOriginPatterns } = await import("../src/institutions.js");
    expect(new Set(manifest.optional_host_permissions)).toEqual(new Set(allOriginPatterns()));
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
