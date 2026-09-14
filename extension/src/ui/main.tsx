/**
 * The side panel shell: a status strip, five views, and a tab bar.
 *
 * The arrangement is the whole point of the redesign. Home, Courses, and Course
 * read tools directly — instant, no tokens, and they keep working when the API
 * key is missing or its quota is spent, which happened twice in one afternoon
 * of testing. Ask is one tab among five rather than the only surface.
 *
 * No router library. Five views in a side panel is a `useState`, and a
 * history-based router in a panel with no address bar would be ceremony around
 * nothing.
 */

import { render } from "preact";
import { useCallback, useEffect, useRef, useState } from "preact/hooks";

import { type Institution } from "../institutions.js";
import { getCurrentInstitution, setInstitutionId } from "../settings.js";
import { invalidate } from "./data/tools.js";
import { useTool } from "./data/useTool.js";
import { applyTheme, watchTheme } from "./theme.js";
import { Chat } from "./views/Chat.js";
import { Course } from "./views/Course.js";
import { Courses } from "./views/Courses.js";
import { Home } from "./views/Home.js";
import { Setup } from "./views/Setup.js";

type Route =
  | { view: "home" }
  | { view: "courses" }
  | { view: "course"; orgUnitId: number }
  | { view: "chat" }
  | { view: "setup" };

const TABS = [
  { id: "home", label: "Home" },
  { id: "courses", label: "Courses" },
  { id: "chat", label: "Ask" },
  { id: "setup", label: "Setup" },
] as const;

interface StatusResult {
  school: { lms_name: string; access_granted: boolean };
  session: { signed_in: boolean | null };
  api_key_set: boolean;
  index: { documents: number; chunks: number };
}

/**
 * One line saying whether this thing can see anything at all.
 *
 * It exists because every empty view has two possible causes — nothing to show,
 * or nothing readable — and a student cannot tell them apart from the view
 * itself. When access or the session is the problem, that fact belongs at the
 * top of the window, not buried in whichever view happened to fail first.
 */
function StatusStrip({
  status,
  onFix,
}: {
  status: StatusResult | null;
  onFix: () => void;
}) {
  if (!status) return null;

  const problem = !status.school.access_granted
    ? `No access to ${status.school.lms_name} yet`
    : status.session.signed_in === false
      ? `Not signed in to ${status.school.lms_name}`
      : null;

  if (!problem) return null;

  return (
    <button type="button" class="strip strip-warn" onClick={onFix}>
      <span>{problem}</span>
      <span class="strip-action">Fix →</span>
    </button>
  );
}

function App() {
  const [route, setRoute] = useState<Route>({ view: "home" });
  const [institution, setInstitution] = useState<Institution | null>(null);
  const instRef = useRef<Institution | null>(null);

  const status = useTool<StatusResult>("get_status");
  const reloadStatus = status.reload;

  useEffect(() => {
    void getCurrentInstitution().then((inst) => {
      instRef.current = inst;
      setInstitution(inst);
      applyTheme(inst);
    });
    watchTheme(() => instRef.current);
  }, []);

  const changeSchool = useCallback(async (id: string) => {
    await setInstitutionId(id);
    const inst = await getCurrentInstitution();
    instRef.current = inst;
    setInstitution(inst);
    applyTheme(inst);
    // Everything cached belongs to the previous school.
    invalidate();
    reloadStatus();
  }, [reloadStatus]);

  const openCourse = useCallback(
    (orgUnitId: number) => setRoute({ view: "course", orgUnitId }),
    [],
  );

  const activeTab = route.view === "course" ? "courses" : route.view;

  return (
    <div class="app">
      {/* The "B" tile is a text glyph, per the handoff — no logo, no wordmark.
          The skin is colour only, which is what keeps a school's palette in a
          student's own tool clear of its trademarks. */}
      <header class="titlebar">
        <span class="titlebar-mark" aria-hidden="true">
          B
        </span>
        <h1 class="titlebar-name">Brightspace Assistant</h1>
      </header>

      {institution ? (
        <div class="schoolstrip">
          <span class="schoolstrip-dot" aria-hidden="true" />
          {institution.lmsName} — {institution.orgName}
        </div>
      ) : null}

      <StatusStrip status={status.data} onFix={() => setRoute({ view: "setup" })} />

      <main class="content">
        {route.view === "home" ? <Home onOpenCourse={openCourse} /> : null}
        {route.view === "courses" ? <Courses onOpenCourse={openCourse} /> : null}
        {route.view === "course" ? (
          <Course orgUnitId={route.orgUnitId} onBack={() => setRoute({ view: "courses" })} />
        ) : null}
        {/* Kept mounted so a long answer is not thrown away by tabbing to
            Courses to check something mid-conversation. */}
        <div hidden={route.view !== "chat"} class="chat-host">
          <Chat hasKey={status.data?.api_key_set ?? false} />
        </div>
        {route.view === "setup" && institution ? (
          <Setup
            institution={institution}
            onInstitutionChange={(id) => void changeSchool(id)}
            onKeyChange={reloadStatus}
          />
        ) : null}
      </main>

      <nav class="tabbar">
        {TABS.map((t) => (
          <button
            key={t.id}
            type="button"
            class={`tabbar-btn${activeTab === t.id ? " tabbar-active" : ""}`}
            aria-current={activeTab === t.id ? "page" : undefined}
            onClick={() => setRoute({ view: t.id } as Route)}
          >
            {t.label}
          </button>
        ))}
      </nav>
    </div>
  );
}

render(<App />, document.getElementById("root")!);
