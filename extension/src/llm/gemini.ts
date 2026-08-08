/**
 * The Gemini function-calling loop.
 *
 * Ask -> model requests tools -> we run them -> feed results back -> repeat
 * until it answers in prose. Everything runs in the browser: the key comes
 * from chrome.storage.local and the call goes straight to Google. There is no
 * server of ours in this path.
 *
 * WHAT LEAVES THE MACHINE: the user's question, the tool results, and this
 * system prompt — so course names, deadlines, grades, and announcement text
 * reach Google under the user's own API key. That is the same exposure as
 * pasting the material into any chat, and it is worth being plain about
 * because "nothing leaves your machine" is true of the Brightspace half only.
 */

import { TOOLS } from "../tools/registry.js";
import { getApiKey, getCurrentInstitution, getModel } from "../settings.js";
import { toFunctionDeclarations } from "./schema.js";

/**
 * How this loop reaches the tools.
 *
 * Deliberately injected rather than imported from background/worker.ts. The
 * loop runs in the SIDE PANEL, and importing the worker module there would
 * execute its top-level `chrome.runtime.onMessage.addListener`, registering a
 * second listener in the wrong context.
 *
 * The panel is also the right place to run it: an MV3 service worker is killed
 * after ~30s idle, and a multi-round tool conversation is exactly the shape
 * that trips over. A panel page lives as long as it is open.
 */
export type CallTool = (
  name: string,
  args: Record<string, unknown>,
) => Promise<
  | { ok: true; result: unknown }
  | { ok: false; error: string; message: string; next_step?: string }
>;

const API_ROOT = "https://generativelanguage.googleapis.com/v1beta";

/**
 * Configurable because model names get retired. If this one 404s, listModels()
 * below turns that into a message naming what IS available rather than an
 * opaque failure.
 */
export const DEFAULT_MODEL = "gemini-2.5-flash";

/**
 * Fallback suggestions, used only before the real list has been fetched.
 *
 * A hardcoded list is a guess about someone else's account: the first version
 * offered gemini-2.5-flash-lite, which this key does not have, so the
 * suggestion sent the user straight into a "not available" error while looking
 * for a way out of a quota one. availableModels() asks the key itself.
 */
export const MODEL_CHOICES = [
  { id: "gemini-2.5-flash", label: "2.5 Flash — balanced" },
  { id: "gemini-2.0-flash", label: "2.0 Flash — separate quota" },
  { id: "gemini-2.5-pro", label: "2.5 Pro — best reasoning, lowest limits" },
];

/**
 * What this key can actually call, newest-looking first.
 *
 * Quotas are per-model, so when one is exhausted the fastest fix is switching
 * to another the SAME key already has. Guessing at that list is what made the
 * previous attempt worse instead of better.
 */
export async function availableModels(): Promise<string[]> {
  const key = await getApiKey();
  if (!key) return [];
  const all = await listModels(key).catch(() => []);
  return all
    .filter((m) => m.startsWith("gemini-") && !/embedding|aqa|imagen|veo|tts/i.test(m))
    .sort((a, b) => b.localeCompare(a));
}

/** Stops a tool loop from running away. Real answers need two or three. */
const MAX_TURNS = 8;

interface Part {
  text?: string;
  functionCall?: { name: string; args?: Record<string, unknown> };
  functionResponse?: { name: string; response: Record<string, unknown> };
  inlineData?: { mimeType: string; data: string };
}

/**
 * Images cost roughly 350k tokens per megabyte of base64 and, because the
 * whole conversation is resent each turn, an image sticks around for the rest
 * of the exchange. Keeping only the most recent one bounds that: a follow-up
 * question about a different page should not still be paying for the first.
 */
function dropStaleImages(contents: Content[]): void {
  let seen = false;
  for (let i = contents.length - 1; i >= 0; i--) {
    const parts = contents[i]!.parts;
    for (let j = parts.length - 1; j >= 0; j--) {
      if (!parts[j]!.inlineData) continue;
      if (seen) {
        parts[j] = { text: "[earlier page image omitted to save quota]" };
      } else {
        seen = true;
      }
    }
  }
}
interface Content {
  role: "user" | "model";
  parts: Part[];
}

export interface ToolTrace {
  name: string;
  args: Record<string, unknown>;
  ok: boolean;
  summary: string;
}

export interface AskResult {
  text: string;
  trace: ToolTrace[];
  turns: number;
}

function systemPrompt(lmsName: string, credentialBrand: string): string {
  return [
    `You help a university student with their ${lmsName} courses. Every tool is`,
    `read-only: you can read their courses, but you cannot submit, post, or change anything.`,
    ``,
    `HOW TO WORK:`,
    `- Start with list_courses to get an org_unit_id; almost everything else needs one.`,
    `- For "what's due", use get_upcoming_deadlines — it covers every course at once.`,
    `- When a tool fails, call get_status before telling the user to sign in. It`,
    `  distinguishes "not signed in" from "this route is restricted for students",`,
    `  which need completely different advice.`,
    ``,
    `HOW TO ANSWER:`,
    `- Quote deadline times using the "local" field, NEVER the "utc" one. These are`,
    `  Eastern-time deadlines and the UTC date is often the following day.`,
    `- If submission_status is "unknown", the route was denied — do NOT tell the user`,
    `  whether they have or have not handed something in. Say it cannot be checked.`,
    `- Same for quiz status "unknown": do not say whether they have taken it.`,
    `- If analyze_grade_summary returns no "target" block, the weights did not`,
    `  reconcile. Report the caveats and do NOT compute a projection yourself.`,
    `- Never invent a letter grade; cutoffs vary by faculty.`,
    `- get_class_list deliberately withholds the student roster. That is by design,`,
    `  not a failure, and you should say so if asked.`,
    `- Read the "note" field on a result before answering — it usually says what is`,
    `  missing and why.`,
    ``,
    `Their credentials are called ${credentialBrand}. Be concise and concrete.`,
  ].join("\n");
}

async function callGemini(
  key: string,
  model: string,
  body: unknown,
): Promise<Record<string, unknown>> {
  let resp: Response;
  try {
    resp = await fetch(`${API_ROOT}/models/${model}:generateContent`, {
      method: "POST",
      headers: { "content-type": "application/json", "x-goog-api-key": key },
      body: JSON.stringify(body),
    });
  } catch (err) {
    throw new Error(`Could not reach the Gemini API. Check your connection. (${String(err)})`);
  }

  const payload = (await resp.json().catch(() => ({}))) as Record<string, unknown>;

  if (!resp.ok) {
    const detail =
      ((payload["error"] as { message?: string } | undefined)?.message ?? "").trim() ||
      `HTTP ${resp.status}`;

    if (resp.status === 400 && /API key not valid/i.test(detail)) {
      throw new Error("That Gemini API key was rejected. Check it in Setup above.");
    }
    if (resp.status === 403) {
      throw new Error(`Gemini refused the request: ${detail}`);
    }
    if (resp.status === 404) {
      const available = await listModels(key).catch(() => []);
      const hint = available.length
        ? ` Models available to this key: ${available.slice(0, 6).join(", ")}.`
        : "";
      throw new Error(`The model "${model}" is not available.${hint}`);
    }
    if (resp.status === 429) {
      throw new Error(
        `${model} has hit its quota on this key. Free tiers are per-model, so ` +
          `switching model in Setup usually works immediately — 2.5 Flash Lite has ` +
          `the highest free limits. Otherwise the quota resets daily.`,
      );
    }
    throw new Error(`Gemini error: ${detail}`);
  }

  return payload;
}

/** Used only to make a 404 actionable. */
async function listModels(key: string): Promise<string[]> {
  const resp = await fetch(`${API_ROOT}/models`, { headers: { "x-goog-api-key": key } });
  if (!resp.ok) return [];
  const data = (await resp.json()) as { models?: Array<{ name?: string; supportedGenerationMethods?: string[] }> };
  return (data.models ?? [])
    .filter((m) => m.supportedGenerationMethods?.includes("generateContent"))
    .map((m) => (m.name ?? "").replace(/^models\//, ""))
    .filter(Boolean);
}

function summarize(result: unknown): string {
  if (result && typeof result === "object") {
    const r = result as Record<string, unknown>;
    for (const k of ["count", "topic_count", "courses_checked"]) {
      if (typeof r[k] === "number") return `${r[k]} ${k === "count" ? "item(s)" : k}`;
    }
    if (typeof r["message"] === "string") return r["message"];
  }
  return "ok";
}

export async function ask(
  question: string,
  callTool: CallTool,
  opts: { model?: string; history?: Content[] } = {},
): Promise<AskResult> {
  const key = await getApiKey();
  if (!key) {
    throw new Error("No Gemini API key set. Add one under Setup, then ask again.");
  }

  const model = opts.model ?? (await getModel(DEFAULT_MODEL));
  const inst = await getCurrentInstitution();
  const declarations = toFunctionDeclarations(TOOLS);

  const contents: Content[] = [
    ...(opts.history ?? []),
    { role: "user", parts: [{ text: question }] },
  ];
  const trace: ToolTrace[] = [];

  for (let turn = 1; turn <= MAX_TURNS; turn++) {
    const payload = await callGemini(key, model, {
      contents,
      tools: [{ functionDeclarations: declarations }],
      systemInstruction: {
        parts: [{ text: systemPrompt(inst.lmsName, inst.credentialBrand) }],
      },
    });

    const candidate = (payload["candidates"] as Array<Record<string, unknown>> | undefined)?.[0];
    const content = candidate?.["content"] as Content | undefined;
    const parts = content?.parts ?? [];

    const calls = parts.filter((p) => p.functionCall).map((p) => p.functionCall!);

    if (calls.length === 0) {
      const text = parts
        .map((p) => p.text ?? "")
        .join("")
        .trim();
      if (text) return { text, trace, turns: turn };

      // No text and no call. finishReason explains why, and "I got nothing"
      // is a useless thing to show a user.
      const reason = String(candidate?.["finishReason"] ?? "unknown");
      if (reason === "MAX_TOKENS") {
        throw new Error("Gemini hit its output limit before answering. Try a narrower question.");
      }
      if (reason === "SAFETY" || reason === "PROHIBITED_CONTENT") {
        throw new Error("Gemini declined to answer that.");
      }
      throw new Error(`Gemini returned no answer (finishReason: ${reason}).`);
    }

    // Echo the model's own turn back before the results, or it loses track of
    // what it asked for.
    contents.push({ role: "model", parts });

    // Gemini can request several tools at once; run them together.
    const nested: Part[][] = await Promise.all(
      calls.map(async (call) => {
        const args = call.args ?? {};
        const outcome = await callTool(call.name, args);
        trace.push({
          name: call.name,
          args,
          ok: outcome.ok,
          summary: outcome.ok ? summarize(outcome.result) : outcome.message,
        });

        if (!outcome.ok) {
          // A failed tool is DATA, not an exception. The typed error carries a
          // next step and the model needs to relay it — throwing here would
          // replace an actionable answer with a dead end.
          return [
            {
              functionResponse: {
                name: call.name,
                response: {
                  error: outcome.error,
                  message: outcome.message,
                  next_step: outcome.next_step ?? null,
                },
              },
            },
          ];
        }

        // An image must travel as inlineData, NOT inside the response JSON.
        // Base64 in a functionResponse is charged as text — 100k+ tokens for
        // one page — and gets resent on every later turn.
        const result = outcome.result as Record<string, unknown> | null;
        const image = result?.["_inlineImage"] as
          | { mimeType: string; data: string }
          | undefined;
        const payload = image ? { ...result } : result;
        if (image) delete (payload as Record<string, unknown>)["_inlineImage"];

        const parts: Part[] = [
          { functionResponse: { name: call.name, response: { result: payload } } },
        ];
        if (image) parts.push({ inlineData: image });
        return parts;
      }),
    );

    contents.push({ role: "user", parts: nested.flat() });
    dropStaleImages(contents);
  }

  throw new Error(
    `Stopped after ${MAX_TURNS} tool rounds without a final answer. ` +
      `Try asking something more specific.`,
  );
}
