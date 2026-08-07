/**
 * get_course_content — the Content tree, structure only.
 * Ported from src/avenue_mcp/tools/content.py (tree walk only).
 *
 * Reading a file's text needs a PDF/PPTX extractor, which is deferred out of
 * v1 along with the rest of RAG — see the plan. So this tool tells you what
 * exists and where, and `is_downloadable` marks the topics a future
 * read_content_file could open.
 */

import { avenue } from "../avenue/client.js";
import { describe, parseD2L } from "../avenue/dates.js";
import { asInt, isObj, pick } from "../avenue/models.js";
import { courseName } from "./context.js";

const MAX_TREE_DEPTH = 10;

interface TopicNode {
  id: number;
  title: string;
  type: string;
  file_name: string | null;
  last_modified: ReturnType<typeof describe>;
  is_downloadable: boolean;
}

interface ModuleNode {
  id: number;
  title: string;
  topics: TopicNode[];
  modules: ModuleNode[];
}

export async function getCourseContent(args: { org_unit_id: number; max_depth?: number }) {
  const { org_unit_id } = args;
  const maxDepth = args.max_depth ?? MAX_TREE_DEPTH;

  const root = await avenue.get("le", `${org_unit_id}/content/root/`);

  const seenModules = new Set<number>();
  const unreadable: Array<{ module_id: number; title: string; reason: string }> = [];
  let topicCount = 0;

  async function walk(
    nodes: unknown,
    depth: number,
  ): Promise<{ modules: ModuleNode[]; topics: TopicNode[] }> {
    if (depth > maxDepth || !Array.isArray(nodes)) return { modules: [], topics: [] };

    const modules: ModuleNode[] = [];
    const topics: TopicNode[] = [];

    for (const node of nodes) {
      if (!isObj(node)) continue;
      const title = String(pick(node, "Title", "Name") ?? "");

      const isModule =
        String(pick(node, "Type") ?? "").toLowerCase() === "module" ||
        "Structure" in node ||
        "Modules" in node ||
        asInt(pick(node, "ModuleId")) !== null;

      if (isModule) {
        const modId = asInt(pick(node, "Id", "ModuleId"));
        // Depth cap AND a visited set: recursive tree-walking against a remote
        // API is a good way to write an accidental infinite loop if the data
        // ever contains a cycle.
        if (modId === null || seenModules.has(modId)) continue;
        seenModules.add(modId);

        let structure = pick(node, "Structure", "Modules", "Topics");
        if (!Array.isArray(structure)) {
          try {
            structure = await avenue.get("le", `${org_unit_id}/content/modules/${modId}/structure/`);
          } catch (err) {
            // Recorded, not silently swallowed. Dropping a subtree while still
            // reporting a confident topic_count made the tool claim a course
            // has 12 files when it has 40, with nothing to signal the gap.
            unreadable.push({ module_id: modId, title, reason: String(err).slice(0, 160) });
            structure = [];
          }
        }

        const child = await walk(structure, depth + 1);
        modules.push({ id: modId, title, topics: child.topics, modules: child.modules });
        continue;
      }

      const topicId = asInt(pick(node, "Id", "TopicId"));
      if (topicId === null) continue;
      topicCount++;

      const url = String(pick(node, "Url") ?? "");
      const fileName = url ? (url.split("/").pop() ?? null) : null;

      topics.push({
        id: topicId,
        title: title || fileName || "",
        type: topicTypeName(node),
        file_name: fileName,
        last_modified: describe(parseD2L(pick(node, "LastModifiedDate", "LastModified"))),
        // False for link and embedded-page topics, so a model does not try to
        // open something with no body.
        is_downloadable: isFileTopic(node, url),
      });
    }

    return { modules, topics };
  }

  const tree = await walk(Array.isArray(root) ? root : [root], 0);

  return {
    org_unit_id,
    course_name: await courseName(org_unit_id),
    modules: tree.modules,
    topics: tree.topics,
    topic_count: topicCount,
    complete: unreadable.length === 0,
    unreadable_modules: unreadable,
    note: unreadable.length
      ? `${unreadable.length} module(s) could not be read, so this tree is INCOMPLETE and topic_count undercounts the course.`
      : "Structure only. Reading file contents is not available in this version.",
  };
}

function topicTypeName(node: Record<string, unknown>): string {
  const t = asInt(pick(node, "TopicType", "Type"));
  if (t === 1) return "File";
  if (t === 3) return "Link";
  return "Topic";
}

function isFileTopic(node: Record<string, unknown>, url: string): boolean {
  if (String(pick(node, "TypeIdentifier") ?? "") === "Link") return false;
  if (!url) return false;
  // An external link topic points off-instance; there is nothing to fetch from
  // the content API for it.
  return !/^https?:\/\//i.test(url);
}
