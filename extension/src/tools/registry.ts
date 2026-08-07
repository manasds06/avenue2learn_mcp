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
import {
  getPageImage,
  readContentFile,
  searchCourseMaterials,
  syncCourseMaterials,
} from "./materials.js";
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
  /**
   * Where the handler can actually execute.
   *
   * "worker" is the default: fetch + JSON reshaping, which a service worker
   * does fine. The file tools need DOMParser (OOXML/HTML) and a pdf.js worker,
   * neither of which exists in a service worker — and a first sync runs for
   * minutes, which MV3 would kill. Those run in the panel.
   */
  runsIn?: "worker" | "panel";
}

/** Tools that must run in the side panel rather than the service worker. */
export const PANEL_TOOLS = new Set([
  "sync_course_materials",
  "search_course_materials",
  "read_content_file",
  "get_page_image",
  "get_status",
]);

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
    name: "search_course_materials",
    description:
      "Semantic + keyword search across the user's INDEXED course files — outlines, slide decks, assignment specs, readings. Returns matching passages WITH CITATIONS (course, file, page or slide). Read-only. " +
      "This is the right tool for any question whose answer is INSIDE a course file: 'what's the late penalty?', 'what are the assignment weights?', 'which lecture covered red-black trees?'. " +
      "Prefer it over read_content_file when you do not already know which file holds the answer — it searches everything at once instead of opening files one by one. " +
      "Requires sync_course_materials to have run for that course. If indexed_courses is empty, NOTHING has been indexed: say so, and do not conclude the material fails to mention what was asked.",
    parameters: {
      type: "object",
      properties: {
        query: { type: "string", description: "A natural-language question." },
        org_unit_id: {
          type: "integer",
          description: "Restrict to one course. Omit to search everything indexed.",
        },
        top_k: { type: "integer", description: "Passages to return. Defaults to 8." },
      },
      required: ["query"],
    },
    handler: (a) => searchCourseMaterials(a as never),
  },
  {
    name: "sync_course_materials",
    description:
      "Download and index one course's files so search_course_materials can search them. Writes only to a LOCAL index — it changes nothing on Brightspace. " +
      "Run once per course, then again when new material is posted. Incremental: unchanged files are skipped. " +
      "The first run on a large course takes minutes and downloads a lot, so do ONE course at a time and only when the user asks for it. Never call this speculatively to rescue an empty search.",
    parameters: {
      type: "object",
      properties: {
        ...ORG_UNIT,
        force: {
          type: "boolean",
          description: "Re-index everything even if unchanged. Defaults to false.",
        },
      },
      required: ["org_unit_id"],
    },
    handler: (a) => syncCourseMaterials(a as never),
  },
  {
    name: "read_content_file",
    description:
      "Return the extracted text of ONE course file (PDF, PPTX, DOCX, HTML, TXT). Read-only. " +
      "Use when you know WHICH file you need — get topic_id from get_course_content or from a search citation. " +
      "If you are hunting for something and do not know the file, use search_course_materials instead; paginating through a deck is slow and burns context. " +
      "Check truncated: when true, continue with next_start_page. If extraction_quality is 'poor' the file is scanned images — say so, and consider get_page_image.",
    parameters: {
      type: "object",
      properties: {
        ...ORG_UNIT,
        topic_id: { type: "integer", description: "From get_course_content or a citation." },
        max_chars: { type: "integer", description: "Defaults to 12000." },
        start_page: { type: "integer", description: "Page/slide to resume from. Defaults to 1." },
      },
      required: ["org_unit_id", "topic_id"],
    },
    handler: (a) => readContentFile(a as never),
  },
  {
    name: "get_page_image",
    description:
      "Render one page of a PDF as an image so it can actually be looked at. Read-only. " +
      "Use when the answer is VISUAL — a diagram, a graph, a circuit, a worked derivation, a table whose layout matters. Text extraction gets the words on a page but loses the figure entirely. " +
      "Prefer read_content_file for prose: images cost far more context than text. One page per call; page numbers are 1-indexed and often come straight from a search citation.",
    parameters: {
      type: "object",
      properties: {
        ...ORG_UNIT,
        topic_id: { type: "integer", description: "From get_course_content or a citation." },
        page: { type: "integer", description: "1-indexed page number." },
        dpi: { type: "integer", description: "Render resolution. Defaults to 150." },
      },
      required: ["org_unit_id", "topic_id", "page"],
    },
    handler: (a) => getPageImage(a as never),
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

export const TOOLS_WITH_PLACEMENT = TOOLS.map((t) => ({
  ...t,
  runsIn: (PANEL_TOOLS.has(t.name) ? "panel" : "worker") as "panel" | "worker",
}));

export const TOOLS_BY_NAME: Record<string, ToolDef> = Object.fromEntries(
  TOOLS.map((t) => [t.name, t]),
);
