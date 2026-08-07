/**
 * User settings: which school, which API key.
 *
 * Everything here lives in chrome.storage.local — on the user's machine, in
 * their browser profile. Nothing is synced and nothing leaves the device.
 *
 * THE CONSTRAINT PYTHON DOESN'T HAVE: an extension may only fetch hosts its
 * manifest declares. Listing every school as a *required* permission would
 * mean asking a McMaster student for access to Carleton's site, which is both
 * a poor prompt and a worse Web Store review. So school hosts live in
 * `optional_host_permissions` and are requested when the user picks their
 * school — each person grants exactly their own.
 */

import {
  DEFAULT_INSTITUTION_ID,
  type Institution,
  getInstitution,
  originPattern,
} from "./institutions.js";

const KEY_INSTITUTION = "institution_id";
const KEY_API_KEY = "gemini_api_key";

export async function getInstitutionId(): Promise<string> {
  const stored = await chrome.storage.local.get(KEY_INSTITUTION);
  const id = stored[KEY_INSTITUTION];
  return typeof id === "string" && id ? id : DEFAULT_INSTITUTION_ID;
}

export async function getCurrentInstitution(): Promise<Institution> {
  try {
    return getInstitution(await getInstitutionId());
  } catch {
    // A stored id that no longer exists (profile removed in an update) must
    // not brick the extension.
    return getInstitution(DEFAULT_INSTITUTION_ID);
  }
}

export async function setInstitutionId(id: string): Promise<void> {
  getInstitution(id); // throws on unknown, before anything is written
  await chrome.storage.local.set({ [KEY_INSTITUTION]: id });
}

// --- host permissions -------------------------------------------------------

export async function hasHostPermission(inst: Institution): Promise<boolean> {
  return chrome.permissions.contains({ origins: [originPattern(inst)] });
}

/**
 * Ask for access to this school's Brightspace.
 *
 * MUST be called from a user gesture (a click), or Chrome rejects it. That is
 * why the side panel triggers this and the service worker only ever checks.
 */
export async function requestHostPermission(inst: Institution): Promise<boolean> {
  return chrome.permissions.request({ origins: [originPattern(inst)] });
}

// --- API key ----------------------------------------------------------------

export async function getApiKey(): Promise<string | null> {
  const stored = await chrome.storage.local.get(KEY_API_KEY);
  const key = stored[KEY_API_KEY];
  return typeof key === "string" && key.trim() ? key.trim() : null;
}

export async function setApiKey(key: string): Promise<void> {
  const trimmed = key.trim();
  if (!trimmed) {
    await chrome.storage.local.remove(KEY_API_KEY);
    return;
  }
  await chrome.storage.local.set({ [KEY_API_KEY]: trimmed });
}

export async function clearApiKey(): Promise<void> {
  await chrome.storage.local.remove(KEY_API_KEY);
}
