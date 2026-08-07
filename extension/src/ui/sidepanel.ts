/**
 * Checkpoint UI: pick a school, grant access, list courses.
 *
 * The permission request MUST happen here rather than in the service worker.
 * Chrome only honours `chrome.permissions.request()` from a user gesture, so
 * a click is required — which is also the honest UX: the user is told which
 * site is about to be accessed, and agrees to it.
 *
 * Replaced by the chat panel once the Gemini loop lands.
 */

import { INSTITUTIONS, type Institution, originPattern } from "../institutions.js";
import {
  getCurrentInstitution,
  hasHostPermission,
  requestHostPermission,
  setInstitutionId,
} from "../settings.js";

const school = document.getElementById("school") as HTMLSelectElement;
const grantRow = document.getElementById("grant-row") as HTMLDivElement;
const grantHint = document.getElementById("grant-hint") as HTMLParagraphElement;
const grantBtn = document.getElementById("grant") as HTMLButtonElement;
const checkBtn = document.getElementById("check") as HTMLButtonElement;
const statusEl = document.getElementById("status") as HTMLDivElement;
const out = document.getElementById("out") as HTMLPreElement;

interface ToolOk {
  ok: true;
  result: { courses: Array<{ org_unit_id: number; name: string }>; count: number };
}
interface ToolErr {
  ok: false;
  error: string;
  message: string;
  next_step?: string;
}

function say(text: string, kind: "" | "ok" | "err" = ""): void {
  statusEl.className = kind;
  statusEl.textContent = text;
}

async function refreshGrantState(): Promise<Institution> {
  const inst = await getCurrentInstitution();
  const granted = await hasHostPermission(inst);

  grantRow.hidden = granted;
  checkBtn.disabled = !granted;

  if (!granted) {
    grantHint.textContent =
      `To read your courses, this extension needs access to ${inst.lmsName} ` +
      `(${originPattern(inst)}). Nothing is sent anywhere — the request happens ` +
      `in your browser, using the session you already have.`;
  }
  return inst;
}

// --- wire up ----------------------------------------------------------------

for (const inst of Object.values(INSTITUTIONS)) {
  const opt = document.createElement("option");
  opt.value = inst.id;
  opt.textContent = `${inst.lmsName} — ${inst.orgName}`;
  school.append(opt);
}

school.value = (await getCurrentInstitution()).id;
await refreshGrantState();

school.addEventListener("change", async () => {
  await setInstitutionId(school.value);
  out.textContent = "";
  say("");
  await refreshGrantState();
});

grantBtn.addEventListener("click", async () => {
  const inst = await getCurrentInstitution();
  const granted = await requestHostPermission(inst);
  if (!granted) {
    say(
      `Access to ${inst.lmsName} was not granted, so no course data can be read. ` +
        `You can grant it any time from this panel.`,
      "err",
    );
    return;
  }
  say("");
  await refreshGrantState();
});

checkBtn.addEventListener("click", async () => {
  const inst = await getCurrentInstitution();
  checkBtn.disabled = true;
  say(`Asking ${inst.lmsName}…`);
  out.textContent = "";

  try {
    const resp: ToolOk | ToolErr = await chrome.runtime.sendMessage({
      type: "tool",
      name: "list_courses",
      args: {},
    });

    if (resp.ok) {
      const { courses, count } = resp.result;
      say(
        `${count} active course${count === 1 ? "" : "s"} from ${inst.lmsName}.\n` +
          `Fetched with your existing session — the extension never read your cookie.`,
        "ok",
      );
      out.textContent = courses.map((c) => `${c.org_unit_id}  ${c.name}`).join("\n");
    } else {
      say([resp.message, resp.next_step].filter(Boolean).join("\n\n"), "err");
      // A revoked permission should put the grant button back.
      if (resp.error === "HostNotGranted") await refreshGrantState();
    }
  } catch (err) {
    say(`Could not reach the extension's service worker.\n\n${String(err)}`, "err");
  } finally {
    checkBtn.disabled = false;
    await refreshGrantState();
  }
});
