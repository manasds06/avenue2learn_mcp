/**
 * The side panel: chat, setup, and a direct tool runner for debugging.
 *
 * The Gemini loop runs HERE rather than in the service worker. MV3 kills a
 * worker after ~30s idle, and a multi-round tool conversation is exactly the
 * shape that trips over; a panel page lives as long as it is open. The panel
 * calls tools by messaging the worker, which keeps all Brightspace access in
 * one place.
 *
 * The permission request also has to live here: Chrome only honours
 * `chrome.permissions.request()` from a user gesture.
 */

import { AvenueError } from "../avenue/errors.js";
import { INSTITUTIONS, type Institution, originPattern } from "../institutions.js";
import {
  DEFAULT_MODEL,
  MODEL_CHOICES,
  ask,
  availableModels,
  type CallTool,
  type ToolTrace,
} from "../llm/gemini.js";
import {
  clearApiKey,
  getApiKey,
  getCurrentInstitution,
  hasHostPermission,
  requestHostPermission,
  getModel,
  setApiKey,
  setInstitutionId,
  setModel,
} from "../settings.js";
import { setSyncProgressSink } from "../tools/materials.js";
import { PANEL_TOOLS, TOOLS, TOOLS_BY_NAME } from "../tools/registry.js";

const $ = <T extends HTMLElement>(id: string): T => document.getElementById(id) as T;

const setupPanel = $<HTMLElement>("setup");
const toggleSetup = $<HTMLButtonElement>("toggle-setup");
const school = $<HTMLSelectElement>("school");
const grantRow = $<HTMLDivElement>("grant-row");
const grantHint = $<HTMLParagraphElement>("grant-hint");
const grantBtn = $<HTMLButtonElement>("grant");
const modelInput = $<HTMLInputElement>("model");
const modelOptions = $<HTMLDataListElement>("model-options");
const saveModelBtn = $<HTMLButtonElement>("save-model");
const modelHint = $<HTMLParagraphElement>("model-hint");
const indexHint = $<HTMLParagraphElement>("index-hint");
const apiKeyInput = $<HTMLInputElement>("apikey");
const saveKeyBtn = $<HTMLButtonElement>("save-key");
const keyHint = $<HTMLParagraphElement>("key-hint");
const toolSelect = $<HTMLSelectElement>("tool");
const toolDesc = $<HTMLParagraphElement>("tool-desc");
const argsBox = $<HTMLTextAreaElement>("args");
const runBtn = $<HTMLButtonElement>("run");
const thread = $<HTMLDivElement>("thread");
const composer = $<HTMLFormElement>("composer");
const question = $<HTMLTextAreaElement>("question");
const sendBtn = $<HTMLButtonElement>("send");

let lastOrgUnitId: number | null = null;
let busy = false;

/**
 * Dispatch a tool to wherever it can actually run.
 *
 * Live-data tools go to the service worker, which owns Brightspace access.
 * The file tools run HERE: they need DOMParser and a pdf.js worker, which a
 * service worker does not have, and a sync runs for minutes — longer than
 * MV3 will keep a worker alive.
 */
const callTool: CallTool = async (name, args) => {
  if (!PANEL_TOOLS.has(name)) {
    return chrome.runtime.sendMessage({ type: "tool", name, args });
  }

  const tool = TOOLS_BY_NAME[name];
  if (!tool) {
    return { ok: false, error: "UnknownTool", message: `No tool named ${name}.` };
  }
  try {
    return { ok: true, result: await tool.handler(args) };
  } catch (err) {
    if (err instanceof AvenueError) return { ok: false, ...err.toResult() };
    console.error("panel tool failed", name, err);
    return {
      ok: false,
      error: "Unexpected",
      message: err instanceof Error ? err.message : String(err),
      next_step: "Check the side panel console for a traceback.",
    };
  }
};

// --- thread rendering -------------------------------------------------------

function bubble(kind: "you" | "bot" | "err" | "info"): HTMLDivElement {
  const el = document.createElement("div");
  el.className = `msg ${kind}`;
  thread.append(el);
  thread.scrollTop = thread.scrollHeight;
  return el;
}

/** Minimal formatting: paragraphs and bullets, no markdown renderer. */
function renderText(el: HTMLElement, text: string): void {
  el.replaceChildren();
  for (const block of text.split(/\n{2,}/)) {
    const lines = block.split("\n").filter(Boolean);
    const bulleted = lines.length > 0 && lines.every((l) => /^\s*[-*•]\s+/.test(l));
    if (bulleted) {
      const ul = document.createElement("ul");
      for (const line of lines) {
        const li = document.createElement("li");
        li.textContent = line.replace(/^\s*[-*•]\s+/, "");
        ul.append(li);
      }
      el.append(ul);
    } else {
      const p = document.createElement("p");
      p.textContent = lines.join(" ");
      el.append(p);
    }
  }
}

function renderTrace(el: HTMLElement, trace: ToolTrace[]): void {
  if (!trace.length) return;
  const details = document.createElement("details");
  details.className = "trace";
  const summary = document.createElement("summary");
  summary.textContent = `${trace.length} tool call${trace.length === 1 ? "" : "s"}`;
  details.append(summary);
  for (const t of trace) {
    const line = document.createElement("div");
    line.className = t.ok ? "trace-ok" : "trace-err";
    line.textContent = `${t.ok ? "✓" : "✕"} ${t.name} — ${t.summary}`;
    details.append(line);
  }
  el.append(details);
}

// --- asking -----------------------------------------------------------------

async function submitQuestion(text: string): Promise<void> {
  if (busy || !text.trim()) return;
  busy = true;
  sendBtn.disabled = true;

  renderText(bubble("you"), text);
  question.value = "";

  const pending = bubble("info");
  pending.textContent = "Thinking…";

  try {
    const result = await ask(text, callTool);
    pending.remove();
    const answer = bubble("bot");
    renderText(answer, result.text);
    renderTrace(answer, result.trace);
  } catch (err) {
    pending.remove();
    const failed = bubble("err");
    failed.textContent = err instanceof Error ? err.message : String(err);
    // A missing grant is recoverable right here.
    await refreshGrantState();
  } finally {
    progressBubble?.remove();
    progressBubble = null;
    void refreshIndexState();
    busy = false;
    sendBtn.disabled = false;
    question.focus();
  }
}

composer.addEventListener("submit", (e) => {
  e.preventDefault();
  void submitQuestion(question.value);
});

question.addEventListener("keydown", (e) => {
  // Enter sends, Shift+Enter makes a newline — the convention people expect.
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    void submitQuestion(question.value);
  }
});

// --- setup ------------------------------------------------------------------

toggleSetup.addEventListener("click", () => {
  setupPanel.hidden = !setupPanel.hidden;
});

async function refreshGrantState(): Promise<Institution> {
  const inst = await getCurrentInstitution();
  const granted = await hasHostPermission(inst);

  grantRow.hidden = granted;
  if (!granted) {
    setupPanel.hidden = false;
    grantHint.textContent =
      `To read your courses, this extension needs access to ${inst.lmsName} ` +
      `(${originPattern(inst)}). The request happens in your browser, using the ` +
      `session you already have.`;
  }
  return inst;
}

async function refreshKeyState(): Promise<void> {
  const key = await getApiKey();
  if (key) {
    apiKeyInput.value = "";
    apiKeyInput.placeholder = `saved (${key.slice(0, 4)}…${key.slice(-4)})`;
    saveKeyBtn.textContent = "Replace";
    keyHint.textContent = "Stored in this browser only. Clear the field and press Replace to remove.";
  } else {
    apiKeyInput.placeholder = "AIza…";
    saveKeyBtn.textContent = "Save";
    keyHint.innerHTML =
      "Needed to ask questions. Free key at " +
      '<a href="https://aistudio.google.com/apikey" target="_blank" rel="noreferrer">' +
      "aistudio.google.com/apikey</a>. Stored in this browser only.";
    setupPanel.hidden = false;
  }
}

saveKeyBtn.addEventListener("click", async () => {
  const entered = apiKeyInput.value.trim();
  if (!entered) {
    await clearApiKey();
  } else if (!/^AIza[\w-]{10,}$/.test(entered)) {
    // Catch obvious paste mistakes here rather than as an opaque 400 later.
    bubble("err").textContent = 'That does not look like a Gemini API key — they start with "AIza".';
    return;
  } else {
    await setApiKey(entered);
    bubble("info").textContent = "API key saved to this browser.";
  }
  apiKeyInput.value = "";
  await refreshKeyState();
});

apiKeyInput.addEventListener("keydown", (e) => {
  if (e.key === "Enter") saveKeyBtn.click();
});

grantBtn.addEventListener("click", async () => {
  const inst = await getCurrentInstitution();
  if (!(await requestHostPermission(inst))) {
    bubble("err").textContent = `Access to ${inst.lmsName} was not granted, so no course data can be read.`;
    return;
  }
  await refreshGrantState();
});

/**
 * Show what is already indexed.
 *
 * The index PERSISTS in IndexedDB across restarts, and sync is incremental —
 * but nothing said so, which makes re-syncing look mandatory.
 */
async function refreshIndexState(): Promise<void> {
  const resp = await callTool("get_status", {});
  if (!resp.ok) return;
  const index = (resp.result as { index?: { documents: number; chunks: number } }).index;
  indexHint.textContent = index?.documents
    ? `${index.documents} file(s) indexed, ${index.chunks} passages. Kept between sessions — re-sync only when new material is posted.`
    : "No course files indexed yet. Ask to sync a course to enable file search.";
}

/**
 * A first sync takes minutes. Reporting progress in one reusable bubble beats
 * a silent panel that looks hung.
 */
let progressBubble: HTMLDivElement | null = null;
setSyncProgressSink((p) => {
  if (!progressBubble) progressBubble = bubble("info");
  const pct = p.total ? Math.round((p.done / p.total) * 100) : 0;
  progressBubble.textContent = `${p.phase} — ${p.done}/${p.total} (${pct}%)`;
  thread.scrollTop = thread.scrollHeight;
});

// --- direct tool runner (debugging) -----------------------------------------

function templateFor(name: string): string {
  const tool = TOOLS_BY_NAME[name];
  if (!tool) return "{}";
  const args: Record<string, unknown> = {};
  for (const key of tool.parameters.required ?? []) {
    args[key] = key === "org_unit_id" ? (lastOrgUnitId ?? 0) : key.endsWith("_id") ? 0 : "";
  }
  return JSON.stringify(args);
}

function syncToolUi(): void {
  toolDesc.textContent = TOOLS_BY_NAME[toolSelect.value]?.description ?? "";
  argsBox.value = templateFor(toolSelect.value);
}

runBtn.addEventListener("click", async () => {
  let args: Record<string, unknown>;
  try {
    args = argsBox.value.trim() ? JSON.parse(argsBox.value) : {};
  } catch (err) {
    bubble("err").textContent = `Arguments are not valid JSON. ${String(err)}`;
    return;
  }

  const name = toolSelect.value;
  const out = bubble("info");
  out.textContent = `Running ${name}…`;

  const resp = await callTool(name, args);
  const pre = document.createElement("pre");
  if (resp.ok) {
    out.textContent = `${name} ✓`;
    pre.textContent = JSON.stringify(resp.result, null, 2);
    const r = resp.result as { courses?: Array<{ org_unit_id: number }> };
    if (r?.courses?.length) lastOrgUnitId = r.courses[0]!.org_unit_id;
  } else {
    // A typed failure is a RESULT: for several tools a PermissionDenied is the
    // correct answer on this instance.
    out.textContent = `${name} — ${resp.error}`;
    pre.textContent = [resp.message, resp.next_step].filter(Boolean).join("\n\n");
  }
  out.append(pre);
  thread.scrollTop = thread.scrollHeight;
});

toolSelect.addEventListener("change", syncToolUi);

// --- init -------------------------------------------------------------------

for (const inst of Object.values(INSTITUTIONS)) {
  const opt = document.createElement("option");
  opt.value = inst.id;
  opt.textContent = `${inst.lmsName} — ${inst.orgName}`;
  school.append(opt);
}
for (const t of TOOLS) {
  const opt = document.createElement("option");
  opt.value = t.name;
  opt.textContent = t.name;
  toolSelect.append(opt);
}

school.value = (await getCurrentInstitution()).id;
school.addEventListener("change", async () => {
  await setInstitutionId(school.value);
  lastOrgUnitId = null;
  await refreshGrantState();
});

// A free-text field with suggestions, not a fixed list: Google adds and
// retires model names faster than this extension ships, and being stuck on a
// rate-limited one with no way out is exactly the problem this solves.
function setSuggestions(ids: Array<{ value: string; label?: string }>): void {
  modelOptions.replaceChildren();
  for (const m of ids) {
    const opt = document.createElement("option");
    opt.value = m.value;
    if (m.label) opt.label = m.label;
    modelOptions.append(opt);
  }
}

setSuggestions(MODEL_CHOICES.map((m) => ({ value: m.id, label: m.label })));

/**
 * Replace the guesses with what the key can actually call.
 *
 * Suggesting a model the account does not have turns "I am rate-limited" into
 * "I am rate-limited AND the suggested fix errors", which is what happened.
 */
async function refreshModelSuggestions(): Promise<void> {
  const models = await availableModels();
  if (!models.length) return;
  setSuggestions(models.map((value) => ({ value })));
  modelHint.textContent =
    `${models.length} models available to this key. Free limits are per-model, ` +
    `so switching is the fastest fix when one is exhausted.`;
}

async function applyModel(): Promise<void> {
  const chosen = modelInput.value.trim();
  if (!chosen) {
    modelInput.value = await getModel(DEFAULT_MODEL);
    return;
  }
  await setModel(chosen);
  modelHint.textContent = `Using ${chosen}. Any Gemini model id works; an unknown one reports which are available for your key.`;
  bubble("info").textContent = `Now using ${chosen}.`;
}

modelInput.value = await getModel(DEFAULT_MODEL);
modelHint.textContent =
  "Type any Gemini model id, or pick a suggestion. Free-tier limits are " +
  "per-model, so switching is the fastest fix when one is rate-limited.";
saveModelBtn.addEventListener("click", () => void applyModel());
modelInput.addEventListener("focus", () => void refreshModelSuggestions(), { once: true });
modelInput.addEventListener("change", () => void applyModel());
modelInput.addEventListener("keydown", (e) => {
  if (e.key === "Enter") {
    e.preventDefault();
    void applyModel();
  }
});

toolSelect.value = "list_courses";
syncToolUi();
await refreshGrantState();
await refreshKeyState();
void refreshIndexState();
void refreshModelSuggestions();
question.focus();
