/**
 * Ask — the questions that genuinely need reasoning, and the only view that
 * spends the model.
 *
 * Two things separate this from the old panel:
 *
 * 1. **Results render as components.** The tools already return structure —
 *    deadlines with local times and status, citations with file and page. The
 *    model flattening that into prose was throwing away the most useful part.
 *    So the wrapper below keeps the raw results of the calls the model makes
 *    and renders them under the answer.
 * 2. **Progress says what is happening.** "Thinking…" for ninety seconds during
 *    a first sync reads as hung. Naming the tool costs nothing and is the
 *    difference between waiting and reloading.
 */

import { useEffect, useRef, useState } from "preact/hooks";

import type { CallTool, Content, ToolTrace } from "../../llm/gemini.js";
import { DEFAULT_MODEL, ask } from "../../llm/gemini.js";
import { getModel } from "../../settings.js";
import { setSyncProgressSink } from "../../tools/materials.js";
import { CitationList, type SearchHit } from "../components/Citation.js";
import { DeadlineCard, type DeadlineItem } from "../components/Deadline.js";
import { callTool, invalidate } from "../data/tools.js";

/** Structure worth rendering, harvested from what the model called. */
type Artifact =
  | { kind: "deadlines"; items: DeadlineItem[] }
  | { kind: "citations"; hits: SearchHit[] };

type Message =
  | { role: "you"; text: string }
  | { role: "bot"; text: string; trace: ToolTrace[]; artifacts: Artifact[] }
  | { role: "err"; text: string };

/** Plain-English names for the progress line. */
const TOOL_LABELS: Record<string, string> = {
  list_courses: "Looking up your courses",
  get_upcoming_deadlines: "Checking deadlines",
  get_grades: "Reading grades",
  analyze_grade_summary: "Working out your standing",
  list_assignments: "Reading assignments",
  list_quizzes: "Reading quizzes",
  list_announcements: "Reading announcements",
  get_course_content: "Reading the content tree",
  search_course_materials: "Searching course files",
  sync_course_materials: "Indexing course files",
  get_page_image: "Rendering a page",
  get_whats_new: "Checking what changed",
  get_status: "Checking the connection",
  get_class_list: "Checking teaching staff",
};

/**
 * Compact argument summary for the trace.
 *
 * "search_course_materials — 0 items" is not diagnosable: it does not say
 * whether the model scoped to a course, or to WHICH course. A zero-result
 * search against an unindexed course is correct; the same line against an
 * indexed one is a bug, and the two were indistinguishable for long enough to
 * cost several wrong guesses.
 */
function summarizeArgs(args: Record<string, unknown>): string {
  const parts = Object.entries(args)
    .filter(([, v]) => v !== undefined && v !== null && v !== "")
    .map(([k, v]) => {
      const text = typeof v === "string" ? v : JSON.stringify(v);
      return `${k}: ${text.length > 32 ? `${text.slice(0, 32)}…` : text}`;
    });
  return parts.length ? `(${parts.join(", ")})` : "()";
}

/** Minimal formatting: paragraphs and bullets. No markdown renderer. */
function Prose({ text }: { text: string }) {
  return (
    <>
      {text.split(/\n{2,}/).map((block, i) => {
        const lines = block.split("\n").filter(Boolean);
        const bulleted = lines.length > 0 && lines.every((l) => /^\s*[-*•]\s+/.test(l));
        return bulleted ? (
          <ul key={i}>
            {lines.map((line, j) => (
              <li key={j}>{line.replace(/^\s*[-*•]\s+/, "")}</li>
            ))}
          </ul>
        ) : (
          <p key={i}>{lines.join(" ")}</p>
        );
      })}
    </>
  );
}

function harvest(name: string, result: unknown, into: Artifact[]): void {
  if (!result || typeof result !== "object") return;
  const r = result as Record<string, unknown>;

  if (name === "get_upcoming_deadlines" && Array.isArray(r["deadlines"])) {
    const items = r["deadlines"] as DeadlineItem[];
    if (items.length) into.push({ kind: "deadlines", items: items.slice(0, 8) });
  }
  if (name === "search_course_materials" && Array.isArray(r["results"])) {
    const hits = (r["results"] as SearchHit[]).filter((h) => h?.citation);
    if (hits.length) into.push({ kind: "citations", hits: hits.slice(0, 6) });
  }
}

const SUGGESTIONS = [
  "What are the assignment weights in my hardest course?",
  "Summarise this week's announcements",
  "What did I miss while I was away?",
];

export function Chat({ hasKey }: { hasKey: boolean }) {
  const [messages, setMessages] = useState<Message[]>([]);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [progress, setProgress] = useState<string | null>(null);
  const [model, setModel] = useState(DEFAULT_MODEL);
  const threadRef = useRef<HTMLDivElement>(null);
  // Question/answer pairs only — see the Content docstring in llm/gemini.ts.
  const history = useRef<Content[]>([]);

  useEffect(() => {
    void getModel(DEFAULT_MODEL).then(setModel);
    // A first sync takes minutes; a silent panel looks hung.
    setSyncProgressSink((p) => {
      const pct = p.total ? Math.round((p.done / p.total) * 100) : 0;
      setProgress(`${p.phase} — ${p.done}/${p.total} (${pct}%)`);
    });
    return () => setSyncProgressSink(null);
  }, []);

  useEffect(() => {
    const el = threadRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [messages, progress]);

  async function send(text: string): Promise<void> {
    const question = text.trim();
    if (!question || busy) return;

    setMessages((m) => [...m, { role: "you", text: question }]);
    setDraft("");
    setBusy(true);
    setProgress("Thinking…");

    const artifacts: Artifact[] = [];
    let mutated = false;

    const traced: CallTool = async (name, args) => {
      setProgress(`${TOOL_LABELS[name] ?? name}…`);
      const outcome = await callTool(name, args);
      if (outcome.ok) harvest(name, outcome.result, artifacts);
      if (name === "sync_course_materials") mutated = true;
      return outcome;
    };

    try {
      const result = await ask(question, traced, { history: history.current });
      history.current = [
        ...history.current,
        { role: "user", parts: [{ text: question }] },
        { role: "model", parts: [{ text: result.text }] },
      ];
      setMessages((m) => [
        ...m,
        { role: "bot", text: result.text, trace: result.trace, artifacts },
      ]);
    } catch (err) {
      setMessages((m) => [
        ...m,
        { role: "err", text: err instanceof Error ? err.message : String(err) },
      ]);
    } finally {
      // A sync changes what every other view would report.
      if (mutated) invalidate();
      setBusy(false);
      setProgress(null);
    }
  }

  return (
    <div class="chat">
      <div class="thread" ref={threadRef} aria-live="polite">
        {messages.length === 0 ? (
          <div class="chat-intro">
            <p>
              Home, Courses, and each course view read Brightspace directly and cost nothing.
              Ask here when the answer needs reading across them — or through your course
              files.
            </p>
            {hasKey ? (
              <div class="suggestions">
                {SUGGESTIONS.map((s) => (
                  <button key={s} type="button" class="suggestion" onClick={() => void send(s)}>
                    {s}
                  </button>
                ))}
              </div>
            ) : (
              <p class="hint">
                Add a Gemini API key in Setup to ask questions. The other views work without
                one.
              </p>
            )}
          </div>
        ) : null}

        {messages.map((msg, i) =>
          msg.role === "you" ? (
            <div key={i} class="msg you">
              <Prose text={msg.text} />
            </div>
          ) : msg.role === "err" ? (
            <div key={i} class="msg err" role="alert">
              {msg.text}
            </div>
          ) : (
            <div key={i} class="msg bot">
              {/* One line per tool the model invoked, before the answer — the
                  handoff's gold dot. It is also the audit trail: an answer
                  with no search line did not read your files. */}
              {msg.trace.map((t, j) => (
                <div key={j} class={`toolline${t.ok ? "" : " toolline-err"}`}>
                  <span class="toolline-dot" aria-hidden="true" />
                  {t.name} · {t.summary}
                </div>
              ))}

              <Prose text={msg.text} />

              {msg.artifacts.map((a, j) =>
                a.kind === "deadlines" ? (
                  <div key={j} class="stack msg-artifact">
                    {a.items.map((item, k) => (
                      <DeadlineCard key={k} item={item} />
                    ))}
                  </div>
                ) : (
                  <div key={j} class="msg-artifact">
                    <CitationList hits={a.hits} />
                  </div>
                ),
              )}

              {msg.trace.length ? (
                <details class="trace">
                  <summary>arguments</summary>
                  {msg.trace.map((t, j) => (
                    <div key={j} class={t.ok ? "trace-ok" : "trace-err"}>
                      {t.ok ? "✓" : "✕"} {t.name}
                      {summarizeArgs(t.args)}
                    </div>
                  ))}
                </details>
              ) : null}
            </div>
          ),
        )}

        {progress ? (
          <div class="toolline">
            <span class="toolline-dot" aria-hidden="true" />
            {progress}
          </div>
        ) : null}
      </div>

      <div class="composer-wrap">
        <form
          class="composer"
          onSubmit={(e) => {
            e.preventDefault();
            void send(draft);
          }}
        >
          <textarea
            rows={1}
            spellcheck={false}
            value={draft}
            placeholder="Ask about your courses…"
            onInput={(e) => setDraft((e.target as HTMLTextAreaElement).value)}
            onKeyDown={(e) => {
              // Enter sends, Shift+Enter makes a newline — the convention people expect.
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                void send(draft);
              }
            }}
          />
          <button type="submit" class="btn" disabled={busy || !draft.trim()}>
            {busy ? "…" : "Ask"}
          </button>
        </form>
        {/* Named on every screen rather than buried in Setup. Which model is
            running and whose quota it spends are the two facts a rate-limit
            error is about; they should already be on screen when it hits. */}
        <p class="composer-meta">{model} · uses your API key</p>
      </div>
    </div>
  );
}
