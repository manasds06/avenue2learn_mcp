/**
 * The tool catalog: name -> handler, description, and parameter schema.
 *
 * DESCRIPTIONS ARE THE MOST IMPORTANT TEXT HERE. The model picks a tool almost
 * entirely from its description, so each says WHEN to use it and when NOT to —
 * the second most common failure is reaching for the wrong tool among near
 * neighbours. These are carried over near-verbatim from the Python server,
 * where they were written and tested against a model.
 *
 * Schemas are plain JSON Schema. llm/schema.ts narrows them to the subset
 * Gemini's FunctionDeclaration accepts.
 */

import { listAnnouncements } from "./announcements.js";
import { getUpcomingDeadlines, listAssignments } from "./assignments.js";
import { getClassList } from "./classlist.js";
import { getCourseContent } from "./content.js";
import { listCourses } from "./courses.js";
import { listDiscussions, readDiscussionThread } from "./discussions.js";
import { analyzeGradeSummary, getGrades } from "./grades.js";
import { listQuizzes } from "./quizzes.js";
import { getStatus } from "./status.js";

export interface JsonSchema {
  type: "object";
  properties: Record<string, { type: string; description: string; nullable?: boolean }>;
  required?: string[];
}

export interface ToolDef {
  name: string;
  description: string;
  parameters: JsonSchema;
  handler: (args: Record<string, unknown>) => Promise<unknown>;
}

const ORG_UNIT: JsonSchema["properties"] = {
  org_unit_id: {
    type: "integer",
    description: "The course's org_unit_id, from list_courses.",
  },
};

export const TOOLS: ToolDef[] = [
  {
    name: "list_courses",
    description:
      "List the user's current Brightspace courses with IDs, names, codes, and term dates. Read-only. " +
      "USE THIS FIRST in almost any task — every other course-scoped tool needs an org_unit_id, and this is where you get one. " +
      "Also answers 'what am I taking this term?'. By default returns only active course offerings.",
    parameters: {
      type: "object",
      properties: {
        include_inactive: {
          type: "boolean",
          description: "Also include completed/past terms. Defaults to false.",
        },
      },
    },
    handler: (a) => listCourses(a as never),
  },
  {
    name: "get_upcoming_deadlines",
    description:
      "Return dated items due within a window ACROSS ALL the user's active courses, soonest first. Read-only. " +
      "This is the tool for 'what's due this week?', 'what's coming up?', or 'am I forgetting anything?'. " +
      "It needs no org_unit_id — it covers every active course itself. " +
      "Quote the `local` time to the user, never the UTC one: deadlines are Eastern and the UTC date is often a day later. " +
      "If submission_status_available is false, do NOT tell the user whether they have handed something in.",
    parameters: {
      type: "object",
      properties: {
        days_ahead: { type: "integer", description: "Window size in days. Defaults to 14." },
        include_submitted: {
          type: "boolean",
          description: "Include work already handed in. Defaults to false.",
        },
      },
    },
    handler: (a) => getUpcomingDeadlines(a as never),
  },
  {
    name: "list_assignments",
    description:
      "List one course's assignments with due dates, point values, instructions, and submission status. Read-only. " +
      "Use for 'what are the assignments in this course?'. For a deadline view across ALL courses use get_upcoming_deadlines instead. " +
      "If `degraded` is true or submission_status is 'unknown', read the `note` and tell the user what is missing rather than inventing it.",
    parameters: {
      type: "object",
      properties: {
        ...ORG_UNIT,
        include_submitted: {
          type: "boolean",
          description: "Include already-submitted work. Defaults to true.",
        },
      },
      required: ["org_unit_id"],
    },
    handler: (a) => listAssignments(a as never),
  },
  {
    name: "get_grades",
    description:
      "Return the user's grades for one course — each item's score, points possible, weight, and instructor feedback. Read-only. " +
      "Use for 'what did I get on X?'. For a computed standing or a 'what do I need on the final' projection use analyze_grade_summary instead — " +
      "do NOT do that arithmetic yourself from this output, because weights may be missing and the result would be wrong.",
    parameters: { type: "object", properties: { ...ORG_UNIT }, required: ["org_unit_id"] },
    handler: (a) => getGrades(a as never),
  },
  {
    name: "analyze_grade_summary",
    description:
      "Compute the user's current standing in a course, and what they'd need on remaining work to hit a target. Read-only, calculation only. " +
      "Use for 'how am I doing?' or 'what do I need on the final for an A-?'. " +
      "IMPORTANT: read `caveats` and `weights_available` before answering. When weights don't reconcile this tool deliberately OMITS the projection. " +
      "If there is no `target` block, say it cannot be computed and why — do NOT estimate one yourself. A confidently wrong 'you need 74%' is much worse than an honest refusal.",
    parameters: {
      type: "object",
      properties: {
        ...ORG_UNIT,
        target_percentage: {
          type: "number",
          description: "Desired final percentage, e.g. 80. Omit to just report standing.",
        },
      },
      required: ["org_unit_id"],
    },
    handler: (a) => analyzeGradeSummary(a as never),
  },
  {
    name: "list_announcements",
    description:
      "Return recent announcements for one course, newest first, with body text and links. Read-only. " +
      "Use for 'what did my prof post?' or catching up after time away. Covers ONE course — call list_courses first to sweep several.",
    parameters: {
      type: "object",
      properties: {
        ...ORG_UNIT,
        limit: { type: "integer", description: "Max items. Defaults to 20." },
        since: { type: "string", description: "ISO date; only items posted after it." },
      },
      required: ["org_unit_id"],
    },
    handler: (a) => listAnnouncements(a as never),
  },
  {
    name: "list_quizzes",
    description:
      "List a course's quizzes and tests with availability windows, due dates, and attempt limits. Read-only. " +
      "Returns QUIZ METADATA ONLY — never questions or answers. " +
      "If status is 'unknown', attempt data is unavailable: do NOT tell the user whether they have taken a quiz.",
    parameters: {
      type: "object",
      properties: {
        ...ORG_UNIT,
        include_completed: {
          type: "boolean",
          description: "Include quizzes already attempted. Defaults to true.",
        },
      },
      required: ["org_unit_id"],
    },
    handler: (a) => listQuizzes(a as never),
  },
  {
    name: "get_course_content",
    description:
      "Return a course's Content tree — modules (folders) and topics (files, links, pages). Read-only. " +
      "Use to see WHAT MATERIALS EXIST or to locate a file. This returns STRUCTURE ONLY, not file contents; reading file text is not available in this version. " +
      "If `complete` is false, the tree is partial and topic_count undercounts.",
    parameters: {
      type: "object",
      properties: {
        ...ORG_UNIT,
        max_depth: { type: "integer", description: "Recursion guard. Defaults to 10." },
      },
      required: ["org_unit_id"],
    },
    handler: (a) => getCourseContent(a as never),
  },
  {
    name: "list_discussions",
    description:
      "List a course's discussion forums and their threads. Read-only. " +
      "Forum threads are often the ONLY place an instructor's clarification exists. " +
      "This returns thread titles and post counts; use read_discussion_thread to read one.",
    parameters: { type: "object", properties: { ...ORG_UNIT }, required: ["org_unit_id"] },
    handler: (a) => listDiscussions(a as never),
  },
  {
    name: "read_discussion_thread",
    description:
      "Read the posts in one discussion thread, preserving the reply structure. Read-only. " +
      "Author NAMES are never returned — only roles (Instructor / TA / Student), which is what tells you how authoritative a reply is. " +
      "Weight an instructor's answer above a classmate's speculation.",
    parameters: {
      type: "object",
      properties: {
        ...ORG_UNIT,
        forum_id: { type: "integer", description: "From list_discussions." },
        topic_id: { type: "integer", description: "The thread id, from list_discussions." },
        max_posts: { type: "integer", description: "Defaults to 50." },
      },
      required: ["org_unit_id", "forum_id", "topic_id"],
    },
    handler: (a) => readDiscussionThread(a as never),
  },
  {
    name: "get_class_list",
    description:
      "Return the instructors and TAs for a course. Read-only. " +
      "Use for 'who teaches this?' or 'who do I email?'. " +
      "This does NOT return a student roster — that is other people's personal information and is withheld by design, not missing by accident. " +
      "Report student_count if asked how big the class is.",
    parameters: { type: "object", properties: { ...ORG_UNIT }, required: ["org_unit_id"] },
    handler: (a) => getClassList(a as never),
  },
  {
    name: "get_status",
    description:
      "Report whether the user is signed in, which school is selected, and what this extension can reach. Read-only. " +
      "Call this when another tool fails, BEFORE telling the user to sign in — it distinguishes 'not signed in' from 'this route is restricted here'.",
    parameters: { type: "object", properties: {} },
    handler: () => getStatus(),
  },
];

export const TOOLS_BY_NAME: Record<string, ToolDef> = Object.fromEntries(
  TOOLS.map((t) => [t.name, t]),
);
