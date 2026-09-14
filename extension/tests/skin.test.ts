/**
 * The skin is the one visual thing with a correctness property rather than a
 * taste one: a Western student must not be looking at McMaster maroon. That is
 * the same category of error as calling their LMS "Avenue" — it belongs to a
 * school that is not theirs.
 *
 * So these tests are about the MECHANISM, not the palette: every school defines
 * a complete skin, nothing downstream hardcodes one school's colour, and the
 * gold-style highlight is always paired with the ink chosen to sit on it.
 */

import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

import { INSTITUTIONS, MCMASTER, WESTERN } from "../src/institutions.js";

const ROOT = new URL("..", import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, "$1");
const css = (name: string) => readFileSync(join(ROOT, "src/ui", name), "utf8");

/**
 * Comments stripped.
 *
 * Both sheets document the literals they refuse to use — "the handoff writes
 * rgba(122,0,60,.14), which is why we mix instead" — and a raw match would flag
 * the explanation as the violation.
 */
const rules = (name: string) => css(name).replace(/\/\*[\s\S]*?\*\//g, "");

const HEX = /^#[0-9a-f]{6}$/i;

describe("every school defines a complete skin", () => {
  it("has three valid colours", () => {
    for (const inst of Object.values(INSTITUTIONS)) {
      expect(inst.skin.accent, `${inst.id} accent`).toMatch(HEX);
      expect(inst.skin.accentSoft, `${inst.id} accentSoft`).toMatch(HEX);
      expect(inst.skin.accentInk, `${inst.id} accentInk`).toMatch(HEX);
    }
  });

  it("gives each school a distinct accent", () => {
    // Two schools sharing an accent means the panel does not visibly change on
    // switching, which is the whole feature.
    const accents = Object.values(INSTITUTIONS).map((i) => i.skin.accent.toLowerCase());
    expect(new Set(accents).size).toBe(accents.length);
  });

  it("keeps McMaster maroon and Western purple", () => {
    expect(MCMASTER.skin.accent).toBe("#7A003C");
    expect(WESTERN.skin.accent).toBe("#4F2683");
  });

  it("offers a host permission for every school in the registry", async () => {
    // A school with a skin and no granted origin is a school that renders
    // beautifully and cannot load anything.
    const manifest = JSON.parse(readFileSync(join(ROOT, "manifest.json"), "utf8"));
    const { allOriginPatterns } = await import("../src/institutions.js");
    const offered = new Set<string>(manifest.optional_host_permissions);
    for (const pattern of allOriginPatterns()) {
      expect(offered, `${pattern} is in the registry but not offered`).toContain(pattern);
    }
  });

  it("marks an unprobed school as unprobed", () => {
    // Western's colours are right; nobody has tested its routes. Claiming
    // otherwise would borrow McMaster's measurements for another instance.
    expect(WESTERN.probedAt).toBeNull();
    expect(Object.keys(WESTERN.capabilities)).toEqual([]);
  });
});

describe("nothing downstream hardcodes a school's colour", () => {
  const sheets = ["theme.css", "app.css"];

  it("keeps app.css free of literal hex", () => {
    // theme.css holds the defaults; app.css must be tokens only, or a school's
    // colour leaks into every other school's chrome.
    const literals = rules("app.css").match(/#[0-9a-f]{3,8}\b/gi) ?? [];
    expect(literals, `hardcoded colours in app.css: ${literals.join(", ")}`).toEqual([]);
  });

  it("keeps the maroon-tinted rgba() literals from the handoff out of both sheets", () => {
    // The handoff writes borders as rgba(122,0,60,.14) — maroon at 14%. Pasted
    // literally, every school's borders would be McMaster-tinted. They belong
    // in color-mix(… var(--accent) …) instead.
    for (const sheet of sheets) {
      expect(rules(sheet), sheet).not.toMatch(/rgba?\(\s*122\s*,\s*0\s*,\s*60/);
    }
  });

  it("derives borders and tints from the accent", () => {
    const theme = css("theme.css");
    for (const token of ["--line:", "--divider:", "--pill-line:", "--shadow:"]) {
      const line = theme.split("\n").find((l) => l.trim().startsWith(token));
      expect(line, `${token} should be mixed from --accent`).toMatch(/var\(--accent\)/);
    }
  });
});

describe("the highlight is only ever a background", () => {
  it("pairs --accent-soft with --accent-ink wherever it is a background", () => {
    // accentSoft is picked to be legible UNDER accentInk, not as a foreground
    // itself. A rule that sets it as a background without the paired ink is a
    // contrast bug waiting for the first school with a pale highlight.
    const blocks = rules("app.css").split("}");
    for (const block of blocks) {
      if (!/background:\s*var\(--accent-soft\)/.test(block)) continue;
      // The dot indicators are decorative shapes with no text in them.
      if (/toolline-dot|schoolstrip-dot/.test(block)) continue;
      expect(block, `--accent-soft background without --accent-ink:\n${block}`).toMatch(
        /color:\s*var\(--accent-ink\)/,
      );
    }
  });
});
