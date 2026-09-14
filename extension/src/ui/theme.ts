/**
 * Write the selected school's skin onto the CSS tokens.
 *
 * theme.css ships McMaster's three colours as defaults, which is only correct
 * for McMaster. This overwrites them at boot and whenever the school changes.
 * Everything else — borders, dividers, tints, shadows — is a `color-mix` of
 * these in CSS, so a school's chrome re-tints wholesale from three values.
 *
 * The same reason `lmsName` exists: a Western student should be looking at
 * purple, not another university's maroon.
 */

import type { Institution } from "../institutions.js";

export function applyTheme(inst: Institution): void {
  const root = document.documentElement;
  root.style.setProperty("--accent", inst.skin.accent);
  root.style.setProperty("--accent-soft", inst.skin.accentSoft);
  root.style.setProperty("--accent-ink", inst.skin.accentInk);

  // The deep brand colours are unreadable as text on a dark ground. Chrome
  // keeps the true colour (white text carries it there); foreground text gets
  // a lifted variant. Recomputed here rather than in CSS because the mix has
  // to follow the school, not a fixed default.
  const dark = window.matchMedia("(prefers-color-scheme: dark)").matches;
  root.style.setProperty(
    "--accent-text",
    dark ? `color-mix(in srgb, ${inst.skin.accent} 55%, #ffffff)` : inst.skin.accent,
  );
}

/** Re-apply on an OS theme switch, so accent text does not stay unreadable. */
export function watchTheme(current: () => Institution | null): void {
  window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => {
    const inst = current();
    if (inst) applyTheme(inst);
  });
}
