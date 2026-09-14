/**
 * Setup — school, access, model, key, and a direct tool runner.
 *
 * The tool runner stays. It is not a debug leftover: it is how the
 * search-returns-0 question was finally settled, by running the tool with the
 * same arguments the model used and reading the raw result instead of guessing
 * from a summary. Twice I guessed wrong before doing that.
 */

import { useEffect, useState } from "preact/hooks";

import {
  INSTITUTIONS,
  type Institution,
  originPattern,
} from "../../institutions.js";
import { DEFAULT_MODEL, MODEL_CHOICES, availableModels } from "../../llm/gemini.js";
import {
  clearApiKey,
  getApiKey,
  getModel,
  hasHostPermission,
  requestHostPermission,
  setApiKey,
  setInstitutionId,
  setModel,
} from "../../settings.js";
import { TOOLS, TOOLS_BY_NAME } from "../../tools/registry.js";
import { Caveat } from "../components/Honesty.js";
import { SectionTitle } from "../components/ui.js";
import { callTool, invalidate } from "../data/tools.js";
import { useTool } from "../data/useTool.js";

interface StatusResult {
  school: { lms_name: string; access_granted: boolean };
  session: { signed_in: boolean | null; detail: string | null };
  api_key_set: boolean;
  index: { documents: number; chunks: number; embed_model: string | null };
  cache: { entries: number; next_expiry_seconds: number | null };
  advice: string[];
}

export function Setup({
  institution,
  onInstitutionChange,
  onKeyChange,
}: {
  institution: Institution;
  onInstitutionChange: (id: string) => void;
  onKeyChange: () => void;
}) {
  const status = useTool<StatusResult>("get_status");

  return (
    <div class="view setup">
      <SchoolSection
        institution={institution}
        onChange={onInstitutionChange}
        onGranted={() => {
          invalidate();
          status.reload();
        }}
      />

      <KeySection
        onChange={() => {
          onKeyChange();
          status.reload();
        }}
      />

      <ModelSection />

      <CacheSection
        cache={status.data?.cache ?? null}
        onCleared={() => {
          invalidate();
          status.reload();
        }}
      />

      <SectionTitle>Indexed files</SectionTitle>
      <p class="hint">
        {status.data?.index.documents
          ? `${status.data.index.documents} file(s), ${status.data.index.chunks} passages. ` +
            `Kept between sessions — re-index a course only when new material is posted.`
          : "No course files indexed yet. Open a course and use Index course to enable file search."}
      </p>

      {status.data && status.data.session.signed_in === false ? (
        <Caveat title={`Not signed in to ${status.data.school.lms_name}`}>
          {status.data.session.detail ??
            "Open your school's Brightspace in a tab and sign in; this extension uses that session."}
        </Caveat>
      ) : null}

      <ToolRunner />

      <SectionTitle>What leaves this machine</SectionTitle>
      <p class="hint">
        Course data is read in this browser using the session you already have — none of it
        goes to any server of ours, because there isn't one. When you <b>ask a question</b>,
        the question and the tool results it needs are sent to Google under your own API key.
        The other views never send anything anywhere.
      </p>
    </div>
  );
}

// --- cache ------------------------------------------------------------------

/**
 * The cache, stated plainly rather than left to be discovered.
 *
 * Anyone who suspects a number is stale needs two things: to know caching
 * happens at all, and a way to force fresh. Both are here, along with the one
 * rule worth knowing — grades are never among the cached things.
 */
function CacheSection({
  cache,
  onCleared,
}: {
  cache: { entries: number; next_expiry_seconds: number | null } | null;
  onCleared: () => void;
}) {
  const [clearing, setClearing] = useState(false);
  const [note, setNote] = useState<string | null>(null);

  async function clear(): Promise<void> {
    setClearing(true);
    // Sent to the worker: it holds the copy that serves every Brightspace route.
    await chrome.runtime.sendMessage({ type: "clear-cache" }).catch(() => undefined);
    setClearing(false);
    setNote("Cleared. The next screen you open will fetch fresh.");
    onCleared();
  }

  return (
    <>
      <SectionTitle
        action={
          <button
            type="button"
            class="btn btn-sm btn-quiet"
            onClick={() => void clear()}
            disabled={clearing}
          >
            {clearing ? "Clearing…" : "Refresh all data"}
          </button>
        }
      >
        Cached data
      </SectionTitle>
      <p class="hint">
        {note ??
          `Course lists, calendars, file lists and announcements are kept for 10–45 minutes so
           moving around the app is instant. ${cache ? `${cache.entries} responses cached.` : ""}`}
      </p>
      <p class="hint">
        <b>Grades, quiz attempts and submissions are never cached to disk</b>, and everything
        volatile is dropped each time you open the panel — a mark you see here is one that was
        just fetched.
      </p>
    </>
  );
}

// --- school -----------------------------------------------------------------

function SchoolSection({
  institution,
  onChange,
  onGranted,
}: {
  institution: Institution;
  onChange: (id: string) => void;
  onGranted: () => void;
}) {
  const [granted, setGranted] = useState<boolean | null>(null);
  const [refused, setRefused] = useState(false);

  useEffect(() => {
    void hasHostPermission(institution).then(setGranted);
  }, [institution]);

  async function grant(): Promise<void> {
    // MUST run from the click itself — Chrome rejects a permission request that
    // is not attached to a user gesture.
    const ok = await requestHostPermission(institution);
    setGranted(ok);
    setRefused(!ok);
    if (ok) onGranted();
  }

  return (
    <>
      <SectionTitle>Your school</SectionTitle>
      <div class="schoolpicker">
        {/* Two swatches previewing the skin the choice applies. The colours are
            the only branding the panel carries — no logos, no wordmarks. */}
        <span class="swatch" aria-hidden="true">
          <span class="swatch-a" style={{ background: institution.skin.accent }} />
          <span class="swatch-b" style={{ background: institution.skin.accentSoft }} />
        </span>
        <select
          class="schoolpicker-select"
          value={institution.id}
          onChange={(e) => {
            setRefused(false);
            onChange((e.target as HTMLSelectElement).value);
          }}
        >
          {Object.values(INSTITUTIONS).map((inst) => (
            <option key={inst.id} value={inst.id}>
              {inst.lmsName} — {inst.orgName}
            </option>
          ))}
        </select>
      </div>
      <p class="hint">Colours follow your school. Changing this re-skins the panel.</p>

      {/* An unprobed school must say so. Its routes may work exactly like
          McMaster's or not at all, and asserting either would be borrowing one
          school's measurements for another. */}
      {institution.probedAt === null ? (
        <p class="hint hint-warn">
          Nobody has tested {institution.lmsName} against this extension yet, so some
          features may not work and errors here are not necessarily bugs.
        </p>
      ) : null}

      {granted === false ? (
        <div class="grant">
          <p class="hint">
            To read your courses this extension needs access to {institution.lmsName} (
            {originPattern(institution)}). The request happens in your browser, using the
            session you already have.
          </p>
          <button type="button" class="btn" onClick={() => void grant()}>
            Allow access
          </button>
          {refused ? (
            <p class="hint hint-warn">
              Access was not granted, so no course data can be read.
            </p>
          ) : null}
        </div>
      ) : null}
    </>
  );
}

// --- api key ----------------------------------------------------------------

function KeySection({ onChange }: { onChange: () => void }) {
  const [entered, setEntered] = useState("");
  const [saved, setSaved] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);

  useEffect(() => {
    void getApiKey().then(setSaved);
  }, []);

  async function save(): Promise<void> {
    const value = entered.trim();
    if (!value) {
      await clearApiKey();
      setSaved(null);
      setNote("Key removed.");
    } else if (!/^AIza[\w-]{10,}$/.test(value)) {
      // Catch an obvious paste mistake here rather than as an opaque 400 later.
      setNote('That does not look like a Gemini API key — they start with "AIza".');
      return;
    } else {
      await setApiKey(value);
      setSaved(value);
      setNote("Saved to this browser only.");
    }
    setEntered("");
    onChange();
  }

  return (
    <>
      <SectionTitle>Gemini API key</SectionTitle>
      <div class="row">
        <input
          class="input"
          type="password"
          autocomplete="off"
          spellcheck={false}
          value={entered}
          placeholder={saved ? `saved (${saved.slice(0, 4)}…${saved.slice(-4)})` : "AIza…"}
          onInput={(e) => setEntered((e.target as HTMLInputElement).value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") void save();
          }}
        />
        <button type="button" class="btn btn-quiet" onClick={() => void save()}>
          {saved ? "Replace" : "Save"}
        </button>
      </div>
      <p class="hint">
        {note ??
          (saved ? (
            "Stored in this browser only. Clear the field and press Replace to remove it."
          ) : (
            <>
              Needed only for the Ask tab. Free key at{" "}
              <a href="https://aistudio.google.com/apikey" target="_blank" rel="noreferrer">
                aistudio.google.com/apikey
              </a>
              . Stored in this browser only.
            </>
          ))}
      </p>
    </>
  );
}

// --- model ------------------------------------------------------------------

function ModelSection() {
  const [model, setModelState] = useState("");
  const [options, setOptions] = useState<string[]>(MODEL_CHOICES.map((m) => m.id));
  const [note, setNote] = useState(
    "Free-tier limits are per-model, so switching is the fastest fix when one is rate-limited.",
  );

  useEffect(() => {
    void getModel(DEFAULT_MODEL).then(setModelState);
  }, []);

  /**
   * Replace the guesses with what the key can actually call.
   *
   * A hardcoded suggestion list is a guess about someone else's account: the
   * first version offered a model this key does not have, which sent the user
   * from a quota error straight into a "not available" one.
   */
  async function loadReal(): Promise<void> {
    const models = await availableModels();
    if (!models.length) return;
    setOptions(models);
    setNote(`${models.length} models available to this key. Limits are per-model.`);
  }

  async function apply(value: string): Promise<void> {
    const chosen = value.trim();
    if (!chosen) return;
    await setModel(chosen);
    setModelState(chosen);
    setNote(`Using ${chosen}.`);
  }

  return (
    <>
      <SectionTitle>Model</SectionTitle>
      <div class="row">
        <input
          class="input"
          list="model-options"
          autocomplete="off"
          spellcheck={false}
          value={model}
          placeholder={DEFAULT_MODEL}
          onFocus={() => void loadReal()}
          onInput={(e) => setModelState((e.target as HTMLInputElement).value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              e.preventDefault();
              void apply((e.target as HTMLInputElement).value);
            }
          }}
        />
        <button type="button" class="btn btn-quiet" onClick={() => void apply(model)}>
          Use
        </button>
      </div>
      <datalist id="model-options">
        {options.map((id) => (
          <option key={id} value={id} />
        ))}
      </datalist>
      <p class="hint">{note}</p>
    </>
  );
}

// --- direct tool runner -----------------------------------------------------

function ToolRunner() {
  const [name, setName] = useState("list_courses");
  const [args, setArgs] = useState("{}");
  const [output, setOutput] = useState<string | null>(null);
  const [running, setRunning] = useState(false);

  function pick(next: string): void {
    setName(next);
    const tool = TOOLS_BY_NAME[next];
    const template: Record<string, unknown> = {};
    for (const key of tool?.parameters.required ?? []) {
      template[key] = key.endsWith("_id") ? 0 : "";
    }
    setArgs(JSON.stringify(template));
    setOutput(null);
  }

  async function run(): Promise<void> {
    let parsed: Record<string, unknown>;
    try {
      parsed = args.trim() ? JSON.parse(args) : {};
    } catch (err) {
      setOutput(`Arguments are not valid JSON.\n${String(err)}`);
      return;
    }

    setRunning(true);
    setOutput(`Running ${name}…`);
    const outcome = await callTool(name, parsed);
    setRunning(false);

    // A typed failure is a RESULT, not a crash: for several tools a
    // PermissionDenied is the correct answer on this instance.
    setOutput(
      outcome.ok
        ? `${name} ✓\n\n${JSON.stringify(outcome.result, null, 2)}`
        : `${name} — ${outcome.error}\n\n${[outcome.message, outcome.next_step]
            .filter(Boolean)
            .join("\n\n")}`,
    );
  }

  return (
    <details class="runner">
      <summary>Run a tool directly</summary>
      <select class="input" value={name} onChange={(e) => pick((e.target as HTMLSelectElement).value)}>
        {TOOLS.map((t) => (
          <option key={t.name} value={t.name}>
            {t.name}
          </option>
        ))}
      </select>
      <p class="hint">{TOOLS_BY_NAME[name]?.description}</p>
      <textarea
        class="input"
        rows={3}
        spellcheck={false}
        value={args}
        onInput={(e) => setArgs((e.target as HTMLTextAreaElement).value)}
      />
      <button type="button" class="btn btn-quiet" onClick={() => void run()} disabled={running}>
        Run tool
      </button>
      {output ? <pre class="runner-out">{output}</pre> : null}
    </details>
  );
}
