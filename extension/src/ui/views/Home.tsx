/**
 * Home — what is due, and what changed.
 *
 * This view is the argument for the whole redesign: it answers the two most
 * common questions with NO model call at all. Instant, free, and it still works
 * when the API key is missing or its quota is spent — which happened twice in
 * one afternoon of testing.
 */

import { useState } from "preact/hooks";

import { DeadlineCard, type DeadlineItem } from "../components/Deadline.js";
import { Caveat } from "../components/Honesty.js";
import { Empty, Failure, SectionTitle, Skeleton } from "../components/ui.js";
import { callTool } from "../data/tools.js";
import { useTool } from "../data/useTool.js";
import { splitCourseTitle } from "../format.js";

interface DeadlinesResult {
  deadlines: DeadlineItem[];
  count: number;
  window_days: number;
  submission_status_available: boolean;
  note: string | null;
}

interface Change {
  course_name: string;
  org_unit_id: number;
  title: string;
  at: { local?: string | null } | null;
  detail?: string | null;
}

interface WhatsNewResult {
  total_changes: number;
  changes: Record<string, Change[]>;
  failures: Array<{ course_name: string; category: string; message: string }>;
  complete: boolean;
  note: string | null;
}

const CHANGE_LABELS: Record<string, string> = {
  announcements: "Announcements",
  new_files: "New files",
  new_grades: "Grades",
  upcoming: "Coming up",
};

export function Home({ onOpenCourse }: { onOpenCourse: (orgUnitId: number) => void }) {
  const deadlines = useTool<DeadlinesResult>("get_upcoming_deadlines", { days_ahead: 14 });

  // mark_seen: FALSE. Opening the app must not consume the watermark — a
  // student who glances at Home and then asks "what did I miss?" should get
  // the same answer, not an empty one.
  const whatsNew = useTool<WhatsNewResult>("get_whats_new", { mark_seen: false });
  const [marking, setMarking] = useState(false);

  async function markAllRead(): Promise<void> {
    setMarking(true);
    await callTool("get_whats_new", { mark_seen: true });
    setMarking(false);
    whatsNew.reload();
  }

  return (
    <div class="view">
      <section>
        <SectionTitle
          action={
            <button
              type="button"
              class="btn btn-sm btn-quiet"
              onClick={() => deadlines.reload()}
              disabled={deadlines.loading}
            >
              Refresh
            </button>
          }
        >
          Due soon
        </SectionTitle>

        {deadlines.initial && deadlines.loading ? (
          <Skeleton rows={3} />
        ) : deadlines.failure ? (
          <Failure failure={deadlines.failure} onRetry={() => deadlines.reload()} />
        ) : deadlines.data && deadlines.data.deadlines.length ? (
          <>
            <div class="stack">
              {deadlines.data.deadlines.map((item, i) => (
                <DeadlineCard key={i} item={item} onOpenCourse={onOpenCourse} />
              ))}
            </div>
            {/* The tool tells us when it cannot know; say so rather than
                letting an absent badge imply "not submitted". */}
            {deadlines.data.submission_status_available === false ? (
              <Caveat title="Submission status unavailable">
                Your school does not let student accounts read what you have handed in, so
                work you have already submitted may still be listed here.
              </Caveat>
            ) : null}
          </>
        ) : (
          <Empty
            title="Nothing due in the next two weeks"
            detail={
              deadlines.data?.note ??
              "This reflects your course calendars and assignment folders."
            }
          />
        )}
      </section>

      <section>
        <SectionTitle
          action={
            whatsNew.data && whatsNew.data.total_changes > 0 ? (
              <button
                type="button"
                class="btn btn-sm btn-quiet"
                onClick={() => void markAllRead()}
                disabled={marking}
              >
                {marking ? "Marking…" : "Mark all read"}
              </button>
            ) : undefined
          }
        >
          Since you last looked
        </SectionTitle>

        {whatsNew.initial && whatsNew.loading ? (
          <Skeleton rows={2} />
        ) : whatsNew.failure ? (
          <Failure failure={whatsNew.failure} onRetry={() => whatsNew.reload()} />
        ) : whatsNew.data && whatsNew.data.total_changes > 0 ? (
          <>
            {Object.entries(whatsNew.data.changes)
              .filter(([, items]) => items.length)
              .map(([key, items]) => (
                <div key={key} class="changegroup">
                  <div class="changegroup-title">
                    <span>{CHANGE_LABELS[key] ?? key}</span>
                    <span class="countbadge">{items.length}</span>
                  </div>
                  <div class="listcard">
                    {items.slice(0, 5).map((c, i) => (
                      <button
                        key={i}
                        type="button"
                        class="changerow"
                        onClick={() => onOpenCourse(c.org_unit_id)}
                      >
                        <span class="changerow-main">
                          <span class="changerow-title">{c.title}</span>
                          <span class="changerow-course">
                            {splitCourseTitle(c.course_name).code ?? c.course_name}
                          </span>
                        </span>
                        {/* Right-aligned mono, per the handoff: a column of
                            percentages is scannable, the same values inline
                            are not. */}
                        {c.detail ? <span class="changerow-value">{c.detail}</span> : null}
                      </button>
                    ))}
                    {items.length > 5 ? (
                      <div class="changerow-more">+{items.length - 5} more</div>
                    ) : null}
                  </div>
                </div>
              ))}

            {/* A partial digest must never read as a complete one. */}
            {!whatsNew.data.complete ? (
              <Caveat title="This digest is incomplete">
                {whatsNew.data.failures.length} source(s) could not be read, so something may
                be missing. Nothing was skipped permanently — those courses will report again
                next time.
              </Caveat>
            ) : null}
          </>
        ) : (
          <Empty title="Nothing new" detail="You are caught up across every active course." />
        )}
      </section>
    </div>
  );
}
