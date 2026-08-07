/**
 * Checkpoint UI: set up a school and key, then run any tool by hand.
 *
 * This exists to exercise all twelve tools before the Gemini loop can call
 * them, and to keep a way of reproducing a bad answer afterwards. The chat
 * panel replaces the runner below; the setup block stays.
 *
 * The permission request MUST happen here rather than in the service worker.
 * Chrome only honours `chrome.permissions.request()` from a user gesture, so a
 * click is required — which is also the honest UX: the user is told which site
 * is about to be accessed, and agrees to it.
 */

import { INSTITUTIONS, type Institution, originPattern } from "../institutions.js";
import {
  clearApiKey,
  getApiKey,
  getCurrentInstitution,
  hasHostPermission,
  requestHostPermission,
  setApiKey,
  setInstitutionId,
} from "../settings.js";
import { TOOLS, TOOLS_BY_NAME } from "../tools/registry.js";

const school = document.getElementById("school") as HTMLSelectElement;
const grantRow = document.getElementById("grant-row") as HTMLDivElement;
const grantHint = document.getElementById("grant-hint") as HTMLParagraphElement;
const grantBtn = document.getElementById("grant") as HTMLButtonElement;
const apiKeyInput = document.getElementById("apikey") as HTMLInputElement;
const saveKeyBtn = document.getElementById("save-key") as HTMLButtonElement;
const keyHint = document.getElementById("key-hint") as HTMLParagraphElement;
const toolSelect = document.getElementById("tool") as HTMLSelectElement;
const toolDesc = document.getElementById("tool-desc") as HTMLParagraphElement;
const argsBox = document.getElementById("args") as HTMLTextAreaElement;
const runBtn = document.getElementById("run") as HTMLButtonElement;
const statusEl = document.getElementById("status") as HTMLDivElement;
const out = document.getElementById("out") as HTMLPreElement;

type ToolResponse =
  | { ok: true; result: unknown }
  | { ok: false; error: string; message: string; next_step?: string };

/** Remembered from the last list_courses so course-scoped tools prefill. */
let lastOrgUnitId: number | null = null;

function say(text: string, kind: "" | "ok" | "err" = ""): void {
  statusEl.className = kind;
  statusEl.textContent = text;
}

// --- setup ------------------------------------------------------------------

async function refreshGrantState(): Promise<Institution> {
  const inst = await getCurrentInstitution();
  const granted = await hasHostPermission(inst);

  grantRow.hidden = granted;
  runBtn.disabled = !granted;

  if (!granted) {
    grantHint.textContent =
      `To read your courses, this extension needs access to ${inst.lmsName} ` +
      `(${originPattern(inst)}). Nothing is sent anywhere — the request happens ` +
      `in your browser, using the session you already have.`;
  }
  return inst;
}

/**
 * The key never leaves this machine: chrome.storage.local, read only when
 * calling Gemini directly. Never echoed back once saved — redisplaying a
 * stored secret invites shoulder-surfing for no benefit.
 */
async function refreshKeyState(): Promise<void> {
  const key = await getApiKey();
  if (key) {
    apiKeyInput.value = "";
    apiKeyInput.placeholder = `saved (${key.slice(0, 4)}…${key.slice(-4)})`;
    saveKeyBtn.textContent = "Replace";
    keyHint.textContent =
      "Stored in this browser only. Clear the field and press Replace to remove it.";
  } else {
    apiKeyInput.placeholder = "AIza…";
    saveKeyBtn.textContent = "Save";
    keyHint.innerHTML =
      "Not needed to run tools below — only for the chat loop. Free key at " +
      '<a href="https://aistudio.google.com/apikey" target="_blank" rel="noreferrer">' +
      "aistudio.google.com/apikey</a>.";
  }
}

saveKeyBtn.addEventListener("click", async () => {
  const entered = apiKeyInput.value.trim();
  if (!entered) {
    await clearApiKey();
    say("API key removed.", "ok");
  } else if (!/^AIza[\w-]{10,}$/.test(entered)) {
    // Catch obvious paste mistakes here rather than as an opaque 400 later.
    say('That does not look like a Gemini API key — they start with "AIza".', "err");
    return;
  } else {
    await setApiKey(entered);
    say("API key saved to this browser.", "ok");
  }
  apiKeyInput.value = "";
  await refreshKeyState();
});

apiKeyInput.addEventListener("keydown", (e) => {
  if (e.key === "Enter") saveKeyBtn.click();
});

// --- tool runner ------------------------------------------------------------

/** A starting point built from the schema, not a guess at what you want. */
function templateFor(name: string): string {
  const tool = TOOLS_BY_NAME[name];
  if (!tool) return "{}";
  const args: Record<string, unknown> = {};
  for (const key of tool.parameters.required ?? []) {
    args[key] =
      key === "org_unit_id" ? (lastOrgUnitId ?? 0) : key.endsWith("_id") ? 0 : "";
  }
  return JSON.stringify(args, null, 2);
}

function syncToolUi(): void {
  const tool = TOOLS_BY_NAME[toolSelect.value];
  toolDesc.textContent = tool?.description ?? "";
  argsBox.value = templateFor(toolSelect.value);
  const optional = Object.keys(tool?.parameters.properties ?? {}).filter(
    (k) => !(tool?.parameters.required ?? []).includes(k),
  );
  if (optional.length) {
    argsBox.placeholder = `optional: ${optional.join(", ")}`;
  }
}

for (const t of TOOLS) {
  const opt = document.createElement("option");
  opt.value = t.name;
  opt.textContent = t.name;
  toolSelect.append(opt);
}

runBtn.addEventListener("click", async () => {
  let args: Record<string, unknown>;
  try {
    args = argsBox.value.trim() ? JSON.parse(argsBox.value) : {};
  } catch (err) {
    say(`Arguments are not valid JSON.\n\n${String(err)}`, "err");
    return;
  }

  const name = toolSelect.value;
  runBtn.disabled = true;
  say(`Running ${name}…`);
  out.textContent = "";
  const started = performance.now();

  try {
    const resp: ToolResponse = await chrome.runtime.sendMessage({ type: "tool", name, args });
    const ms = Math.round(performance.now() - started);

    if (resp.ok) {
      say(`${name} succeeded in ${ms} ms.`, "ok");
      out.textContent = JSON.stringify(resp.result, null, 2);

      // Remember a course id so the course-scoped tools prefill usefully.
      const result = resp.result as { courses?: Array<{ org_unit_id: number }> };
      if (result?.courses?.length) lastOrgUnitId = result.courses[0]!.org_unit_id;
    } else {
      // A typed failure is a RESULT, not a crash — several of these are the
      // correct answer (a restricted route, a signed-out session).
      say([`${name}: ${resp.error}`, resp.message, resp.next_step].filter(Boolean).join("\n\n"), "err");
      if (resp.error === "HostNotGranted") await refreshGrantState();
    }
  } catch (err) {
    say(`Could not reach the service worker.\n\n${String(err)}`, "err");
  } finally {
    runBtn.disabled = false;
  }
});

toolSelect.addEventListener("change", syncToolUi);

// --- init -------------------------------------------------------------------

for (const inst of Object.values(INSTITUTIONS)) {
  const opt = document.createElement("option");
  opt.value = inst.id;
  opt.textContent = `${inst.lmsName} — ${inst.orgName}`;
  school.append(opt);
}

school.value = (await getCurrentInstitution()).id;
school.addEventListener("change", async () => {
  await setInstitutionId(school.value);
  lastOrgUnitId = null;
  out.textContent = "";
  say("");
  await refreshGrantState();
});

grantBtn.addEventListener("click", async () => {
  const inst = await getCurrentInstitution();
  if (!(await requestHostPermission(inst))) {
    say(`Access to ${inst.lmsName} was not granted, so no course data can be read.`, "err");
    return;
  }
  say("");
  await refreshGrantState();
});

toolSelect.value = "list_courses";
syncToolUi();
await refreshGrantState();
await refreshKeyState();
