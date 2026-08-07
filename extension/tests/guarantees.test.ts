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

describe("we only ever talk to two hosts", () => {
  const manifest = JSON.parse(readFileSync(join(ROOT, "manifest.json"), "utf8"));

  it("declares exactly Avenue and the Gemini API", () => {
    expect(new Set(manifest.host_permissions)).toEqual(
      new Set([
        "https://avenue.cllmcmaster.ca/*",
        "https://generativelanguage.googleapis.com/*",
      ]),
    );
  });

  it("has no server of ours in the manifest", () => {
    // If a backend is ever added, this test should be updated DELIBERATELY,
    // with a decision about what that server would see (docs/07).
    const hosts: string[] = manifest.host_permissions;
    expect(hosts.some((h) => /avenue-?assistant|herokuapp|fly\.dev|vercel|onrender/.test(h))).toBe(
      false,
    );
  });
});

describe("the Avenue host is the Brightspace one", () => {
  it("is avenue.cllmcmaster.ca, not avenue.mcmaster.ca", async () => {
    const client = readFileSync(join(ROOT, "src/avenue/client.ts"), "utf8");
    // avenue.mcmaster.ca is a static Apache landing page where every /d2l/*
    // path 404s. Getting this wrong breaks every tool on first use (docs/08).
    expect(client).toContain("https://avenue.cllmcmaster.ca");
    expect(code(join(ROOT, "src/avenue/client.ts"))).not.toMatch(
      /https:\/\/avenue\.mcmaster\.ca/,
    );
  });
});
