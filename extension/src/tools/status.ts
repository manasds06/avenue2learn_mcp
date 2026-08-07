/**
 * get_status — why did that just fail?
 *
 * Ported in spirit from src/avenue_mcp/tools/status.py, minus the index
 * reporting (there is no local index in this version).
 *
 * The point is to separate three failures that a model otherwise conflates:
 * not signed in, host not granted, and a route that is simply restricted for
 * students. Each needs a different sentence to the user, and telling someone
 * to sign in again when the real answer is "instructors only" sends them at a
 * wall that will never move.
 */

import { avenue } from "../avenue/client.js";
import { AvenueError } from "../avenue/errors.js";
import { CAPABILITY_KEYS, capability, originPattern } from "../institutions.js";
import { getApiKey, getCurrentInstitution, hasHostPermission } from "../settings.js";
import { indexStatus } from "./materials.js";

export async function getStatus() {
  const inst = await getCurrentInstitution();
  const granted = await hasHostPermission(inst);

  let signedIn: boolean | null = null;
  let signInDetail: string | null = null;

  if (granted) {
    try {
      await avenue.get("lp", "users/whoami");
      signedIn = true;
    } catch (err) {
      signedIn = false;
      signInDetail =
        err instanceof AvenueError ? `${err.message} ${err.nextStep}`.trim() : String(err);
    }
  }

  const index = await indexStatus();

  const advice: string[] = [];
  if (!granted) {
    advice.push(
      `This extension has not been granted access to ${inst.lmsName}. Open the side panel and choose your school — Chrome will ask you to allow it.`,
    );
  } else if (signedIn === false) {
    advice.push(
      `You are not signed in to ${inst.lmsName}. Open ${inst.loginUrl} and sign in with your ${inst.credentialBrand}.`,
    );
  }
  if (!(await getApiKey())) {
    advice.push("No API key is set, so questions cannot be answered yet. Add one in the side panel.");
  }
  if (!index.documents) {
    advice.push(
      "No course files are indexed, so search_course_materials has nothing to search. Run sync_course_materials for a course.",
    );
  }
  if (!advice.length) advice.push("Everything is connected.");

  return {
    school: {
      id: inst.id,
      name: inst.orgName,
      lms_name: inst.lmsName,
      host: originPattern(inst),
      access_granted: granted,
    },
    session: { signed_in: signedIn, detail: signInDetail },
    api_key_set: (await getApiKey()) !== null,
    index,
    // What a probe MEASURED on this instance — "unverified" where nobody has
    // looked. Never generalize one school's results to another.
    known_restrictions: Object.fromEntries(
      CAPABILITY_KEYS.map((k) => [k, capability(inst, k)]).filter(
        ([, v]) => v !== "permitted",
      ),
    ),
    probed_at: inst.probedAt,
    advice,
  };
}
