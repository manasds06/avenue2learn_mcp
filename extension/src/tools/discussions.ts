/**
 * Discussion tools — ported from src/avenue_mcp/tools/discussions.py.
 *
 * Forum threads are frequently the ONLY place a clarification exists — an
 * instructor answering "does Q3 want the recursive version?" in a reply is not
 * in the outline, not on a slide, and not in an announcement.
 *
 * AUTHOR NAMES ARE NEVER RETURNED. Posts carry `author_role` only: the role is
 * what confers authority, and the name is other students' personal information
 * with no use case here. See docs/07.
 *
 * Read-only, permanently. Posting in a student's name to a space their
 * classmates read is a higher-consequence write than submitting an assignment.
 */

import { avenue } from "../avenue/client.js";
import { AvenueError } from "../avenue/errors.js";
import { describe, parseD2L } from "../avenue/dates.js";
import { asInt, isObj, normalizeRole, pick, richText, toText, truncate } from "../avenue/models.js";
import { courseName } from "./context.js";

const POST_CAP = 3000;

export async function listDiscussions(args: { org_unit_id: number }) {
  const { org_unit_id } = args;

  const forumsRaw = await avenue.getPaged("le", `${org_unit_id}/discussions/forums/`);
  const forums = [];
  let topicCount = 0;

  for (const forum of forumsRaw) {
    if (!isObj(forum)) continue;
    const forumId = asInt(pick(forum, "ForumId", "Id"));
    if (forumId === null) continue;

    let topicsRaw: unknown[] = [];
    try {
      topicsRaw = await avenue.getPaged(
        "le",
        `${org_unit_id}/discussions/forums/${forumId}/topics/`,
      );
    } catch (err) {
      // One unreadable forum must not cost the others.
      if (!(err instanceof AvenueError)) throw err;
    }

    const topics = [];
    for (const topic of topicsRaw) {
      if (!isObj(topic)) continue;
      const threadId = asInt(pick(topic, "TopicId", "Id"));
      if (threadId === null) continue;
      topicCount++;
      topics.push({
        topic_id: threadId,
        name: String(pick(topic, "Name", "Title") ?? ""),
        post_count: asInt(pick(topic, "PostCount", "NumberOfPosts", "TotalPosts")),
        last_post_at: describe(parseD2L(pick(topic, "LastPostDate", "LastPost", "LastModified"))),
        // The highest-signal field here — a thread the instructor has answered
        // is usually the authoritative one — but it costs a read per thread,
        // so it stays null until read_discussion_thread is called.
        has_instructor_replies: null,
      });
    }

    forums.push({
      forum_id: forumId,
      name: String(pick(forum, "Name", "Title") ?? ""),
      topics,
    });
  }

  return {
    org_unit_id,
    course_name: await courseName(org_unit_id),
    forums,
    forum_count: forums.length,
    topic_count: topicCount,
    note:
      "has_instructor_replies is only known after reading a thread. Use " +
      "read_discussion_thread on the ones that look relevant.",
  };
}

export async function readDiscussionThread(args: {
  org_unit_id: number;
  forum_id: number;
  topic_id: number;
  max_posts?: number;
}) {
  const { org_unit_id, forum_id, topic_id } = args;
  const maxPosts = args.max_posts ?? 50;

  const raw = await avenue.getPaged(
    "le",
    `${org_unit_id}/discussions/forums/${forum_id}/topics/${topic_id}/posts/`,
  );

  const posts = [];
  for (const entry of raw) {
    if (!isObj(entry)) continue;
    const body = toText(richText(pick(entry, "Message", "Body", "Content")));
    const { text } = truncate(body, POST_CAP);
    if (!text) continue;

    posts.push({
      post_id: asInt(pick(entry, "PostId", "Id")),
      // Preserves the reply tree. Flattening destroys the question -> answer
      // pairing, which is the entire value of reading a thread.
      parent_post_id: asInt(pick(entry, "ParentPostId", "ParentId")),
      author_role: normalizeRole(roleOf(entry)),
      posted_at: describe(parseD2L(pick(entry, "DatePosted", "PostingDate", "CreatedDate"))),
      body_text: text,
    });
  }

  posts.sort(
    (a, b) =>
      (a.posted_at?.utc ?? "").localeCompare(b.posted_at?.utc ?? "") ||
      (a.post_id ?? 0) - (b.post_id ?? 0),
  );

  const truncated = maxPosts > 0 && posts.length > maxPosts;
  const shown = truncated ? posts.slice(0, maxPosts) : posts;

  return {
    org_unit_id,
    course_name: await courseName(org_unit_id),
    forum_id,
    topic_id,
    posts: shown,
    count: shown.length,
    truncated,
    has_instructor_replies: shown.some(
      (p) => p.author_role === "Instructor" || p.author_role === "TA",
    ),
    note: "Author names are intentionally omitted; only roles are reported.",
  };
}

function roleOf(entry: Record<string, unknown>): unknown {
  const direct = pick(entry, "AuthorRole", "Role", "RoleName", "RoleAlias");
  if (direct !== undefined) return direct;
  const author = pick(entry, "Author");
  return isObj(author) ? pick(author, "Role", "RoleName", "RoleAlias") : undefined;
}
