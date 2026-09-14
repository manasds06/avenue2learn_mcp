/**
 * One course, in tabs.
 *
 * Every tab reads a tool directly — no model, no tokens. The Grades tab is the
 * one that matters most: it is where a plausible wrong number does real damage,
 * so when the weights do not reconcile it shows the caveat INSTEAD of a
 * projection, at full size, rather than tucking it under an answer.
 */

import { useState } from "preact/hooks";

import { DeadlineCard, type DeadlineItem } from "../components/Deadline.js";
import { Caveat, NotIndexed, Unknown } from "../components/Honesty.js";
import {
  Badge,
  Card,
  Empty,
  Failure,
  SectionTitle,
  Skeleton,
  Stat,
  Tabs,
  WeightBar,
} from "../components/ui.js";
import { callTool, invalidate } from "../data/tools.js";
import { useTool } from "../data/useTool.js";
import { splitCourseTitle } from "../format.js";

type TabId = "work" | "grades" | "files" | "news" | "talk";

const TABS: Array<{ id: TabId; label: string }> = [
  { id: "work", label: "Work" },
  { id: "grades", label: "Grades" },
  { id: "files", label: "Files" },
  { id: "news", label: "News" },
  { id: "talk", label: "Discussions" },
];

export function Course({ orgUnitId, onBack }: { orgUnitId: number; onBack: () => void }) {
  const [tab, setTab] = useState<TabId>("work");

  // Resolved here rather than passed in, so opening a course from a deadline
  // card works without the caller holding a name map. If it cannot be
  // resolved the heading stays generic — inventing "Course 759806" from the id
  // is exactly the failure that put a placeholder name into every indexed
  // chunk for a whole session.
  const courses = useTool<{ courses: Array<{ org_unit_id: number; name: string }> }>(
    "list_courses",
  );
  const courseName =
    courses.data?.courses.find((c) => c.org_unit_id === orgUnitId)?.name ?? null;

  return (
    <div class="view">
      <div class="coursehead">
        <button type="button" class="btn btn-sm btn-quiet" onClick={onBack}>
          ← Courses
        </button>
        {courseName ? (
          <>
            {splitCourseTitle(courseName).code ? (
              <span class="coursecard-code">{splitCourseTitle(courseName).code}</span>
            ) : null}
            <h1 class="coursehead-name">{splitCourseTitle(courseName).title}</h1>
          </>
        ) : (
          <h1 class="coursehead-name">Course</h1>
        )}
      </div>

      <Tabs tabs={TABS} active={tab} onSelect={setTab} />

      {tab === "work" ? <WorkTab orgUnitId={orgUnitId} /> : null}
      {tab === "grades" ? <GradesTab orgUnitId={orgUnitId} /> : null}
      {tab === "files" ? (
        <FilesTab orgUnitId={orgUnitId} courseName={courseName ?? undefined} />
      ) : null}
      {tab === "news" ? <NewsTab orgUnitId={orgUnitId} /> : null}
      {tab === "talk" ? <DiscussionsTab orgUnitId={orgUnitId} /> : null}
    </div>
  );
}

// --- work -------------------------------------------------------------------

interface AssignmentsResult {
  assignments: DeadlineItem[];
  count: number;
  degraded: boolean;
  submission_status_available: boolean;
  note: string | null;
}

interface Quiz {
  quiz_id: number | null;
  name: string;
  due_date: { local?: string | null } | null;
  days_until_due: number | null;
  status: string;
  attempts_allowed: number | null;
  attempts_used: number | null;
}

interface QuizzesResult {
  quizzes: Quiz[];
  count: number;
  attempt_status_available: boolean;
  note: string | null;
}

function WorkTab({ orgUnitId }: { orgUnitId: number }) {
  const assignments = useTool<AssignmentsResult>("list_assignments", {
    org_unit_id: orgUnitId,
  });
  const quizzes = useTool<QuizzesResult>("list_quizzes", { org_unit_id: orgUnitId });

  return (
    <>
      <SectionTitle>Assignments</SectionTitle>
      {assignments.initial && assignments.loading ? (
        <Skeleton rows={3} />
      ) : assignments.failure ? (
        <Failure failure={assignments.failure} onRetry={() => assignments.reload()} />
      ) : assignments.data?.assignments.length ? (
        <>
          <div class="stack">
            {assignments.data.assignments.map((a, i) => (
              <DeadlineCard key={i} item={a} />
            ))}
          </div>
          {assignments.data.submission_status_available === false ? (
            <Caveat title="Submission status cannot be checked">
              {assignments.data.note ??
                "The route that reports what you have handed in is denied to student accounts here."}
            </Caveat>
          ) : null}
        </>
      ) : (
        <Empty title="No assignments" detail={assignments.data?.note ?? undefined} />
      )}

      <SectionTitle>Quizzes and tests</SectionTitle>
      {quizzes.initial && quizzes.loading ? (
        <Skeleton rows={2} />
      ) : quizzes.failure ? (
        <Failure failure={quizzes.failure} onRetry={() => quizzes.reload()} />
      ) : quizzes.data?.quizzes.length ? (
        <div class="stack">
          {quizzes.data.quizzes.map((q, i) => (
            <Card key={i}>
              <div class="deadline">
                <div class="deadline-head">
                  <span class="deadline-title">{q.name}</span>
                  {/* Never "not attempted" when the route is denied. */}
                  {q.status === "unknown" ? (
                    <Unknown
                      what="attempts"
                      why="Your school does not let student accounts read quiz attempts, so whether you have taken this cannot be checked here."
                    />
                  ) : (
                    <Badge tone={q.status === "attempted" ? "ok" : "neutral"}>
                      {q.status.replace("_", " ")}
                    </Badge>
                  )}
                </div>
                <div class="deadline-meta">
                  <span class="deadline-when">{q.due_date?.local ?? "No due date"}</span>
                  {q.attempts_allowed ? <span>· {q.attempts_allowed} attempts</span> : null}
                </div>
              </div>
            </Card>
          ))}
        </div>
      ) : (
        <Empty title="No quizzes" />
      )}
    </>
  );
}

// --- grades -----------------------------------------------------------------

interface GradeItem {
  name: string | null;
  points_earned: number | null;
  points_possible: number | null;
  percentage: number | null;
  weight: number | null;
  is_graded: boolean;
  displayed_grade: string | null;
  feedback_text: string | null;
}

interface GradesResult {
  items: GradeItem[];
  weights_available: boolean;
  notes: string[];
}

interface SummaryResult {
  current_weighted_average: number | null;
  points_average?: number;
  graded_weight: number | null;
  remaining_weight: number | null;
  total_weight_declared?: number;
  weights_available: boolean;
  target: { required_average_on_remaining: number; achievable: boolean } | null;
  caveats: string[];
}

function GradesTab({ orgUnitId }: { orgUnitId: number }) {
  const grades = useTool<GradesResult>("get_grades", { org_unit_id: orgUnitId });
  const summary = useTool<SummaryResult>("analyze_grade_summary", { org_unit_id: orgUnitId });

  if (grades.initial && grades.loading) return <Skeleton rows={4} />;
  if (grades.failure) return <Failure failure={grades.failure} onRetry={() => grades.reload()} />;

  const items = grades.data?.items ?? [];
  const graded = items.filter((i) => i.is_graded);

  return (
    <>
      {summary.data ? (
        <>
          <div class="stats">
            <Stat
              label="Current average"
              value={
                summary.data.current_weighted_average !== null
                  ? `${summary.data.current_weighted_average}%`
                  : summary.data.points_average !== undefined
                    ? `${summary.data.points_average}%`
                    : "—"
              }
            />
            <Stat
              label="Graded"
              value={
                summary.data.graded_weight !== null ? `${summary.data.graded_weight}%` : "—"
              }
            />
            <Stat
              label="Remaining"
              value={
                summary.data.remaining_weight !== null
                  ? `${summary.data.remaining_weight}%`
                  : "—"
              }
            />
          </div>

          {summary.data.weights_available && items.some((i) => i.weight) ? (
            <WeightBar
              segments={items
                .filter((i) => i.weight)
                .map((i) => ({
                  label: i.name ?? "",
                  weight: i.weight!,
                  tone: i.is_graded ? "accent" : "neutral",
                }))}
            />
          ) : null}

          {/* The refusal gets MORE room than the number it replaces. A
              confidently wrong "you need 74% on the final" is the most
              damaging output this app could produce. */}
          {summary.data.caveats.length ? (
            <Caveat title="What this cannot tell you">
              <ul>
                {summary.data.caveats.map((c, i) => (
                  <li key={i}>{c}</li>
                ))}
              </ul>
            </Caveat>
          ) : null}
        </>
      ) : null}

      <SectionTitle>Items</SectionTitle>
      {graded.length ? (
        <div class="stack">
          {items.map((item, i) => (
            <Card key={i}>
              <div class="graderow">
                <div class="graderow-head">
                  <span class="graderow-name">{item.name}</span>
                  <span class="graderow-score">
                    {item.percentage !== null
                      ? `${item.percentage}%`
                      : (item.displayed_grade ?? (item.is_graded ? "graded" : "—"))}
                  </span>
                </div>
                <div class="graderow-meta">
                  {item.weight !== null ? <span>{item.weight}% of final</span> : null}
                  {item.points_earned !== null && item.points_possible ? (
                    <span>
                      · {item.points_earned}/{item.points_possible}
                    </span>
                  ) : null}
                  {!item.is_graded ? <span>· not marked yet</span> : null}
                </div>
                {item.feedback_text ? (
                  <div class="graderow-feedback">{item.feedback_text}</div>
                ) : null}
              </div>
            </Card>
          ))}
        </div>
      ) : (
        <Empty title="Nothing graded yet" />
      )}
    </>
  );
}

// --- files ------------------------------------------------------------------

interface Topic {
  id: number;
  title: string;
  file_name: string | null;
  is_downloadable: boolean;
}

interface Module {
  id: number;
  title: string;
  topics: Topic[];
  modules: Module[];
}

interface ContentResult {
  modules: Module[];
  topics: Topic[];
  topic_count: number;
  complete: boolean;
  note: string | null;
}

interface StatusResult {
  index: { courses_indexed: Array<{ org_unit_id: number; files: number }> };
}

function FilesTab({ orgUnitId, courseName }: { orgUnitId: number; courseName?: string }) {
  const content = useTool<ContentResult>("get_course_content", { org_unit_id: orgUnitId });
  const status = useTool<StatusResult>("get_status");
  const [syncing, setSyncing] = useState(false);
  const [syncNote, setSyncNote] = useState<string | null>(null);

  const indexedFiles =
    status.data?.index.courses_indexed.find((c) => c.org_unit_id === orgUnitId)?.files ?? 0;

  async function sync(): Promise<void> {
    setSyncing(true);
    setSyncNote("Indexing… this takes a few minutes the first time.");
    const outcome = await callTool("sync_course_materials", { org_unit_id: orgUnitId });
    setSyncing(false);

    if (outcome.ok) {
      const r = outcome.result as {
        files_indexed: number;
        chunks_in_index: number;
        errors: unknown[];
        note: string | null;
      };
      // chunks_in_index is READ BACK from the store, unlike chunks_created
      // which only counted what we tried to write.
      setSyncNote(
        `${r.files_indexed} file(s) indexed · ${r.chunks_in_index} passages searchable` +
          (r.errors.length ? ` · ${r.errors.length} failed` : "") +
          (r.note ? ` — ${r.note}` : ""),
      );
      // get_status now reports different index figures everywhere.
      invalidate("get_status");
      status.reload();
    } else {
      setSyncNote(`${outcome.message} ${outcome.next_step ?? ""}`.trim());
    }
  }

  const files: Topic[] = [];
  const walk = (mods: Module[]): void => {
    for (const m of mods) {
      files.push(...m.topics.filter((t) => t.is_downloadable));
      walk(m.modules);
    }
  };
  if (content.data) {
    walk(content.data.modules);
    files.push(...content.data.topics.filter((t) => t.is_downloadable));
  }

  return (
    <>
      <SectionTitle
        action={
          <button
            type="button"
            class="btn btn-sm"
            onClick={() => void sync()}
            disabled={syncing}
          >
            {syncing ? "Indexing…" : indexedFiles ? "Re-index" : "Index course"}
          </button>
        }
      >
        Files
      </SectionTitle>

      {syncNote ? <p class="hint">{syncNote}</p> : null}

      {!indexedFiles && !syncing ? <NotIndexed courseName={courseName} /> : null}

      {content.initial && content.loading ? (
        <Skeleton rows={4} />
      ) : content.failure ? (
        <Failure failure={content.failure} onRetry={() => content.reload()} />
      ) : files.length ? (
        <>
          <div class="stack">
            {files.map((f) => (
              <Card key={f.id}>
                <div class="filerow">
                  <span class="filerow-name">{f.file_name ?? f.title}</span>
                </div>
              </Card>
            ))}
          </div>
          {content.data && !content.data.complete ? (
            <Caveat title="This file list is incomplete">
              Part of the content tree could not be read, so some files were never seen.
            </Caveat>
          ) : null}
        </>
      ) : (
        <Empty title="No downloadable files" />
      )}
    </>
  );
}

// --- discussions ------------------------------------------------------------

interface DiscussionThread {
  topic_id: number;
  name: string;
  post_count: number | null;
  last_post_at: { local?: string | null } | null;
}

interface Forum {
  forum_id: number;
  name: string;
  topics: DiscussionThread[];
}

interface DiscussionsResult {
  forums: Forum[];
  forum_count: number;
  topic_count: number;
}

interface Post {
  post_id: number | null;
  parent_post_id: number | null;
  author_role: string | null;
  posted_at: { local?: string | null } | null;
  body_text: string;
}

interface ThreadResult {
  posts: Post[];
  count: number;
  truncated: boolean;
  has_instructor_replies: boolean;
  note: string;
}

/**
 * Forums, threads, and posts — by ROLE, never by name.
 *
 * The tool omits author names deliberately and this must not put them back.
 * "Instructor" is the field that carries the signal anyway: on a thread full of
 * guesses, which reply is authoritative is the only thing worth knowing.
 */
function DiscussionsTab({ orgUnitId }: { orgUnitId: number }) {
  const [open, setOpen] = useState<{ forumId: number; topicId: number; name: string } | null>(
    null,
  );
  const forums = useTool<DiscussionsResult>("list_discussions", { org_unit_id: orgUnitId });

  const thread = useTool<ThreadResult>(
    "read_discussion_thread",
    open
      ? { org_unit_id: orgUnitId, forum_id: open.forumId, topic_id: open.topicId }
      : { org_unit_id: orgUnitId, forum_id: 0, topic_id: 0 },
    { enabled: open !== null },
  );

  if (open) {
    return (
      <>
        <SectionTitle
          action={
            <button type="button" class="btn btn-sm btn-quiet" onClick={() => setOpen(null)}>
              ← All threads
            </button>
          }
        >
          {open.name}
        </SectionTitle>

        {thread.loading ? (
          <Skeleton rows={3} />
        ) : thread.failure ? (
          <Failure failure={thread.failure} onRetry={() => thread.reload()} />
        ) : thread.data?.posts.length ? (
          <>
            {thread.data.has_instructor_replies ? (
              <Badge tone="ok">an instructor or TA replied in this thread</Badge>
            ) : (
              <Badge tone="neutral">no instructor reply in this thread</Badge>
            )}
            <div class="stack">
              {thread.data.posts.map((p, i) => (
                <Card key={i}>
                  <div class="newsrow">
                    <div class="newsrow-head">
                      <span class="newsrow-title">
                        {/* Role only. Names are withheld by the tool on purpose. */}
                        {p.author_role ?? "Participant"}
                        {p.parent_post_id ? " · reply" : ""}
                      </span>
                      <span class="newsrow-when">{p.posted_at?.local ?? ""}</span>
                    </div>
                    <div class="newsrow-body">{p.body_text}</div>
                  </div>
                </Card>
              ))}
            </div>
            {thread.data.truncated ? (
              <Caveat title="Only the first part of this thread is shown">
                Long threads are capped. Open it in Brightspace to read the rest.
              </Caveat>
            ) : null}
          </>
        ) : (
          <Empty title="No posts in this thread" />
        )}
      </>
    );
  }

  if (forums.initial && forums.loading) return <Skeleton rows={3} />;
  if (forums.failure) return <Failure failure={forums.failure} onRetry={() => forums.reload()} />;
  if (!forums.data?.forums.length) return <Empty title="No discussion forums" />;

  return (
    <>
      {forums.data.forums.map((forum) => (
        <div key={forum.forum_id}>
          <SectionTitle>{forum.name}</SectionTitle>
          {forum.topics.length ? (
            <div class="stack">
              {forum.topics.map((t) => (
                <Card
                  key={t.topic_id}
                  onClick={() =>
                    setOpen({ forumId: forum.forum_id, topicId: t.topic_id, name: t.name })
                  }
                >
                  <div class="courserow">
                    <div class="courserow-name">{t.name}</div>
                    <div class="deadline-meta">
                      {t.post_count !== null ? <span>{t.post_count} posts</span> : null}
                      {t.last_post_at?.local ? <span>· {t.last_post_at.local}</span> : null}
                    </div>
                  </div>
                </Card>
              ))}
            </div>
          ) : (
            <Empty title="No threads in this forum" />
          )}
        </div>
      ))}
    </>
  );
}

// --- news -------------------------------------------------------------------

interface Announcement {
  id: number | null;
  title: string;
  body_text: string;
  posted_at: { local?: string | null } | null;
  links: Array<{ text: string; url: string }>;
}

interface NewsResult {
  announcements: Announcement[];
  count: number;
}

function NewsTab({ orgUnitId }: { orgUnitId: number }) {
  const news = useTool<NewsResult>("list_announcements", { org_unit_id: orgUnitId, limit: 20 });

  if (news.initial && news.loading) return <Skeleton rows={3} />;
  if (news.failure) return <Failure failure={news.failure} onRetry={() => news.reload()} />;
  if (!news.data?.announcements.length) return <Empty title="No announcements" />;

  return (
    <div class="stack">
      {news.data.announcements.map((a, i) => (
        <Card key={i}>
          <div class="newsrow">
            <div class="newsrow-head">
              <span class="newsrow-title">{a.title}</span>
              <span class="newsrow-when">{a.posted_at?.local ?? ""}</span>
            </div>
            <div class="newsrow-body">{a.body_text}</div>
            {a.links.length ? (
              <div class="newsrow-links">
                {a.links.slice(0, 4).map((l, j) => (
                  <a key={j} href={l.url} target="_blank" rel="noreferrer">
                    {l.text}
                  </a>
                ))}
              </div>
            ) : null}
          </div>
        </Card>
      ))}
    </div>
  );
}
