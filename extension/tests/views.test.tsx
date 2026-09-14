/**
 * @vitest-environment jsdom
 *
 * The views render tool output directly, which means they can re-introduce the
 * exact failure the tool layer works hardest to avoid: reporting silence as a
 * definite answer. "not submitted" when the route was denied, a projection when
 * the weights did not reconcile, "no materials" when nothing was ever indexed.
 *
 * These tests pin the rendering side of those rules. The tool-layer side is
 * already pinned by parity.test.ts — the point here is that a view cannot quietly
 * drop the honest part on its way to the screen.
 *
 * The tool dispatch is mocked rather than exercised: importing the real one
 * drags in pdf.js, transformers.js and the whole Brightspace client, none of
 * which this is testing.
 */

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/preact";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

/** Per-test fixtures, keyed by tool name. */
let RESULTS: Record<string, unknown> = {};

vi.mock("../src/ui/data/tools.js", () => ({
  callTool: vi.fn(async (name: string) =>
    name in RESULTS
      ? { ok: true, result: RESULTS[name] }
      : { ok: false, error: "NoFixture", message: `No fixture for ${name}.` },
  ),
  cachedTool: vi.fn(async (name: string) =>
    name in RESULTS
      ? { ok: true, result: RESULTS[name] }
      : { ok: false, error: "NoFixture", message: `No fixture for ${name}.` },
  ),
  invalidate: vi.fn(),
}));

const { Home } = await import("../src/ui/views/Home.js");
const { Courses } = await import("../src/ui/views/Courses.js");
const { Course } = await import("../src/ui/views/Course.js");
const { DeadlineCard } = await import("../src/ui/components/Deadline.js");

const EMPTY_STATUS = {
  index: { documents: 0, chunks: 0, courses_indexed: [] },
};

beforeEach(() => {
  RESULTS = {};
});
afterEach(cleanup);

describe("a denied route never renders as a fact about the student's work", () => {
  it('renders "unknown", not "not submitted", when submission status cannot be read', () => {
    render(
      <DeadlineCard
        item={{
          title: "Assignment 3",
          due_date: { utc: null, local: "Fri 27 Mar, 11:59 PM", local_date: null },
          days_until: 2,
          submission_status: "unknown",
        }}
      />,
    );

    expect(screen.getByText(/submission unknown/i)).toBeTruthy();
    expect(screen.queryByText(/not submitted/i)).toBeNull();
  });

  it("shows the local due time, never the UTC one", () => {
    // A Friday 11:59 PM Eastern deadline is Saturday in UTC. Rendering the UTC
    // field tells a student their Friday assignment is due Saturday.
    render(
      <DeadlineCard
        item={{
          title: "Lab 2",
          due_date: {
            utc: "2026-03-28T03:59:00.000Z",
            local: "Fri 27 Mar, 11:59 PM",
            local_date: "2026-03-27",
          },
          days_until: 1,
        }}
      />,
    );

    expect(screen.getByText("Fri 27 Mar, 11:59 PM")).toBeTruthy();
    expect(screen.queryByText(/2026-03-28/)).toBeNull();
  });

  it("carries the tool's submission caveat onto Home", async () => {
    RESULTS = {
      get_upcoming_deadlines: {
        deadlines: [
          {
            title: "Assignment 3",
            course_name: "SFWRENG 2FA3",
            org_unit_id: 1,
            due_date: { utc: null, local: "Fri 27 Mar, 11:59 PM", local_date: null },
            days_until: 2,
            submission_status: "unknown",
          },
        ],
        count: 1,
        window_days: 14,
        submission_status_available: false,
        note: null,
      },
      get_whats_new: { total_changes: 0, changes: {}, failures: [], complete: true, note: null },
    };

    render(<Home onOpenCourse={() => {}} />);

    await waitFor(() => expect(screen.getByText("Assignment 3")).toBeTruthy());
    expect(screen.getByText(/Submission status unavailable/i)).toBeTruthy();
  });

  it("says a digest is incomplete rather than presenting it as everything", async () => {
    RESULTS = {
      get_upcoming_deadlines: {
        deadlines: [],
        count: 0,
        window_days: 14,
        submission_status_available: true,
        note: null,
      },
      get_whats_new: {
        total_changes: 1,
        changes: {
          announcements: [
            { course_name: "2FA3", org_unit_id: 1, title: "Midterm moved", at: null },
          ],
        },
        failures: [{ course_name: "2DA4", category: "grades", message: "403" }],
        complete: false,
        note: null,
      },
    };

    render(<Home onOpenCourse={() => {}} />);

    await waitFor(() => expect(screen.getByText("Midterm moved")).toBeTruthy());
    expect(screen.getByText(/digest is incomplete/i)).toBeTruthy();
  });

  it("does not consume the what's-new watermark just by opening Home", async () => {
    RESULTS = {
      get_upcoming_deadlines: {
        deadlines: [],
        count: 0,
        window_days: 14,
        submission_status_available: true,
        note: null,
      },
      get_whats_new: { total_changes: 0, changes: {}, failures: [], complete: true, note: null },
    };

    const { cachedTool } = await import("../src/ui/data/tools.js");
    render(<Home onOpenCourse={() => {}} />);

    await waitFor(() =>
      expect(vi.mocked(cachedTool).mock.calls.some(([n]) => n === "get_whats_new")).toBe(true),
    );
    const call = vi.mocked(cachedTool).mock.calls.find(([n]) => n === "get_whats_new")!;
    expect(call[1]).toEqual({ mark_seen: false });
  });
});

describe("an empty result says WHY it is empty", () => {
  it("distinguishes an unindexed course from one with nothing to find", async () => {
    RESULTS = {
      list_courses: {
        courses: [
          {
            org_unit_id: 1,
            name: "SFWRENG 2FA3:Discrete Mathematics and Applications II",
            code: "2FA3",
            is_active: true,
            end_date: null,
          },
          {
            org_unit_id: 2,
            name: "SFWRENG 2DA4:Digital Systems and Interfacing",
            code: "2DA4",
            is_active: true,
            end_date: null,
          },
        ],
        count: 2,
      },
      get_status: {
        index: {
          documents: 26,
          chunks: 128,
          courses_indexed: [{ org_unit_id: 1, course_name: "SFWRENG 2FA3", files: 26 }],
        },
      },
    };

    render(<Courses onOpenCourse={() => {}} />);

    await waitFor(() => expect(screen.getByText("SFWRENG 2FA3")).toBeTruthy());
    expect(screen.getByText("indexed · 26 files")).toBeTruthy();
    expect(screen.getByText("not indexed")).toBeTruthy();
  });

  it("splits the course code off the title rather than running them together", async () => {
    // D2L hands back one field: "SFWRENG 2FA3:Discrete Mathematics…". Rendered
    // raw it reads as one long unbroken string, which is what the redesign
    // called out.
    RESULTS = {
      list_courses: {
        courses: [
          {
            org_unit_id: 1,
            name: "SFWRENG 2FA3:Discrete Mathematics and Applications II",
            code: "2FA3",
            is_active: true,
            end_date: null,
          },
          // No colon — must render the whole name as the title, with no code
          // line, rather than inventing a split.
          {
            org_unit_id: 2,
            name: "Engineering Co-Op Success Course",
            code: "",
            is_active: true,
            end_date: null,
          },
        ],
        count: 2,
      },
      get_status: EMPTY_STATUS,
    };

    render(<Courses onOpenCourse={() => {}} />);

    await waitFor(() => expect(screen.getByText("SFWRENG 2FA3")).toBeTruthy());
    expect(screen.getByText("Discrete Mathematics and Applications II")).toBeTruthy();
    expect(screen.getByText("Engineering Co-Op Success Course")).toBeTruthy();
    // The joined form must not survive anywhere.
    expect(screen.queryByText(/2FA3:Discrete/)).toBeNull();
  });

  it("explains what an unindexed course means on its Files tab", async () => {
    RESULTS = {
      list_courses: {
        courses: [
          { org_unit_id: 1, name: "SFWRENG 2DA4", code: "2DA4", is_active: true, end_date: null },
        ],
        count: 1,
      },
      get_status: EMPTY_STATUS,
      get_course_content: { modules: [], topics: [], topic_count: 0, complete: true, note: null },
    };

    render(<Course orgUnitId={1} onBack={() => {}} />);

    fireEvent.click(await screen.findByText("Files"));
    await waitFor(() => expect(screen.getByText(/has no indexed files/i)).toBeTruthy());
    // The distinction the whole component exists for.
    expect(screen.getByText(/different from the materials not mentioning/i)).toBeTruthy();
  });
});

describe("personal information stays withheld", () => {
  it("labels discussion posts by role, and renders no author name field", async () => {
    RESULTS = {
      list_courses: {
        courses: [
          { org_unit_id: 1, name: "SFWRENG 2FA3", code: "2FA3", is_active: true, end_date: null },
        ],
        count: 1,
      },
      list_discussions: {
        forums: [
          {
            forum_id: 9,
            name: "Q&A",
            topics: [
              { topic_id: 5, name: "Midterm scope", post_count: 3, last_post_at: null },
            ],
          },
        ],
        forum_count: 1,
        topic_count: 1,
      },
      read_discussion_thread: {
        posts: [
          {
            post_id: 1,
            parent_post_id: null,
            author_role: "Student",
            posted_at: null,
            body_text: "Is chapter 4 on the midterm?",
          },
          {
            post_id: 2,
            parent_post_id: 1,
            author_role: "Instructor",
            posted_at: null,
            body_text: "No, chapters 1 to 3 only.",
          },
        ],
        count: 2,
        truncated: false,
        has_instructor_replies: true,
        note: "Author names are intentionally omitted; only roles are reported.",
      },
    };

    render(<Course orgUnitId={1} onBack={() => {}} />);

    fireEvent.click(await screen.findByText("Discussions"));
    fireEvent.click(await screen.findByText("Midterm scope"));

    await waitFor(() => expect(screen.getByText("No, chapters 1 to 3 only.")).toBeTruthy());
    // The authoritative reply is the whole reason to read a thread.
    expect(screen.getByText(/an instructor or TA replied/i)).toBeTruthy();
    expect(screen.getByText(/^Instructor/)).toBeTruthy();
  });
});

describe("a withheld projection is shown, not omitted", () => {
  it("renders the caveats when the weights did not reconcile", async () => {
    RESULTS = {
      list_courses: {
        courses: [
          { org_unit_id: 1, name: "SFWRENG 2FA3", code: "2FA3", is_active: true, end_date: null },
        ],
        count: 1,
      },
      get_grades: {
        items: [
          {
            name: "Midterm",
            points_earned: 34,
            points_possible: 40,
            percentage: 85,
            weight: null,
            is_graded: true,
            displayed_grade: null,
            feedback_text: null,
          },
        ],
        weights_available: false,
        notes: [],
      },
      analyze_grade_summary: {
        current_weighted_average: null,
        graded_weight: null,
        remaining_weight: null,
        weights_available: false,
        target: null,
        caveats: ["Weights are not published for this course, so no projection is possible."],
      },
    };

    render(<Course orgUnitId={1} onBack={() => {}} />);

    fireEvent.click(await screen.findByText("Grades"));
    await waitFor(() => expect(screen.getByText(/What this cannot tell you/i)).toBeTruthy());
    expect(screen.getByText(/no projection is possible/i)).toBeTruthy();
  });
});

describe("a failure keeps its next step", () => {
  it("renders next_step rather than a generic error", async () => {
    RESULTS = {
      get_whats_new: { total_changes: 0, changes: {}, failures: [], complete: true, note: null },
    };
    // get_upcoming_deadlines has no fixture, so it fails.
    const { cachedTool } = await import("../src/ui/data/tools.js");
    vi.mocked(cachedTool).mockImplementationOnce(async () => ({
      ok: false,
      error: "PermissionDenied",
      message: "Deadlines could not be read.",
      next_step: "Open Brightspace and sign in, then try again.",
    }));

    render(<Home onOpenCourse={() => {}} />);

    await waitFor(() => expect(screen.getByText("Deadlines could not be read.")).toBeTruthy());
    expect(screen.getByText("Open Brightspace and sign in, then try again.")).toBeTruthy();
  });
});
