/**
 * A deadline, rendered from the structure the tool already returns.
 *
 * `get_upcoming_deadlines` gives course, title, type, a date block with both
 * UTC and a local rendering, days-until, and submission status. Prose throws
 * all of that away; a card keeps it scannable.
 *
 * ALWAYS the local time, never the UTC one. McMaster deadlines are Eastern and
 * land at 11:59 PM, which is 03:59 or 04:59 UTC THE NEXT DAY. Showing the UTC
 * date tells a student their Friday assignment is due Saturday.
 */

import { Badge, Card, type Tone } from "./ui.js";
import { Unknown } from "./Honesty.js";

export interface DateBlock {
  utc: string | null;
  local: string | null;
  local_date: string | null;
}

export interface DeadlineItem {
  title: string;
  course_name?: string;
  org_unit_id?: number;
  type?: string;
  due_date: DateBlock | null;
  days_until: number | null;
  submission_status?: string;
  points_possible?: number | null;
}

/** Urgency, but never alarm for something already handed in. */
function urgency(days: number | null, status?: string): Tone {
  if (status === "submitted") return "ok";
  if (days === null) return "neutral";
  if (days < 1) return "danger";
  if (days <= 3) return "warn";
  return "neutral";
}

function countdown(days: number | null): string {
  if (days === null) return "no date";
  if (days < 0) return "past due";
  if (days < 1) return "today";
  if (days < 2) return "tomorrow";
  return `${Math.floor(days)} days`;
}

export function DeadlineCard({
  item,
  onOpenCourse,
}: {
  item: DeadlineItem;
  onOpenCourse?: (orgUnitId: number) => void;
}) {
  const tone = urgency(item.days_until, item.submission_status);

  return (
    <Card
      onClick={
        onOpenCourse && item.org_unit_id ? () => onOpenCourse(item.org_unit_id!) : undefined
      }
    >
      <div class="deadline">
        <div class="deadline-head">
          <span class="deadline-title">{item.title}</span>
          <Badge tone={tone}>{countdown(item.days_until)}</Badge>
        </div>

        {item.course_name ? <div class="deadline-course">{item.course_name}</div> : null}

        <div class="deadline-meta">
          {/* The local rendering, deliberately. See the header note. */}
          <span class="deadline-when">{item.due_date?.local ?? "No due date"}</span>
          {item.points_possible ? <span>· {item.points_possible} pts</span> : null}
          {item.type && item.type !== "assignment" ? <span>· {item.type}</span> : null}
        </div>

        <div class="deadline-status">
          {item.submission_status === "submitted" ? (
            <Badge tone="ok">submitted</Badge>
          ) : item.submission_status === "unknown" ? (
            <Unknown
              what="submission"
              why="Your school does not let student accounts read submission status, so this cannot be checked here. Open Brightspace to confirm."
            />
          ) : item.submission_status === "not_submitted" ? (
            <Badge tone="neutral">not submitted</Badge>
          ) : null}
        </div>
      </div>
    </Card>
  );
}
