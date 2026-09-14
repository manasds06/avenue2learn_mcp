/**
 * Service worker: live Brightspace API access.
 *
 * It handles only the tools that are fetch + JSON reshaping. The file tools
 * (sync, search, read, render) run in the side panel instead, for two reasons:
 * they need DOMParser and a pdf.js worker, neither of which exists in a
 * service worker; and a first sync runs for minutes, which MV3's ~30s idle
 * kill would cut off.
 *
 * This module therefore imports the registry LAZILY. A static import would
 * pull in rag/extract.ts at load time and the whole worker would fail to
 * start.
 */

import { AvenueError } from "../avenue/errors.js";

export interface ToolRequest {
  type: "tool";
  name: string;
  args?: Record<string, unknown>;
}

/** Sent by the side panel when it mounts. See onPanelOpened. */
export interface PanelOpenedMessage {
  type: "panel-opened";
}

/**
 * Sent by Setup's "Refresh all data".
 *
 * Must be handled HERE, not in the panel: the panel and the worker each hold
 * their own in-memory copy, and clearing only the panel's would leave the
 * worker — which serves every Brightspace route — still answering from the
 * entries the user just asked to throw away.
 */
export interface ClearCacheMessage {
  type: "clear-cache";
}

export type ToolResponse =
  | { ok: true; result: unknown }
  | { ok: false; error: string; message: string; next_step?: string };

export async function runTool(
  name: string,
  args: Record<string, unknown>,
): Promise<ToolResponse> {
  const { TOOLS_BY_NAME, PANEL_TOOLS, coerceArgs } = await import("../tools/registry.js");

  if (PANEL_TOOLS.has(name)) {
    return {
      ok: false,
      error: "WrongContext",
      message: `${name} runs in the side panel, not the service worker.`,
      next_step: "This is a bug in the extension's dispatch, not something the user can fix.",
    };
  }

  const tool = TOOLS_BY_NAME[name];
  if (!tool) {
    return {
      ok: false,
      error: "UnknownTool",
      message: `No tool named ${name}.`,
      next_step: `Available: ${Object.keys(TOOLS_BY_NAME).join(", ")}.`,
    };
  }

  try {
    return { ok: true, result: await tool.handler(coerceArgs(tool, args)) };
  } catch (err) {
    if (err instanceof AvenueError) {
      // Typed errors carry their own next step, which is the whole point: the
      // model has to relay something actionable to the user.
      return { ok: false, ...err.toResult() };
    }
    console.error("tool failed", name, err);
    return {
      ok: false,
      error: "Unexpected",
      message: String(err),
      next_step: "This looks like a bug. Check the service worker console.",
    };
  }
}

chrome.runtime.onMessage.addListener(
  (msg: ToolRequest | PanelOpenedMessage | ClearCacheMessage, _sender, sendResponse) => {
    if (msg?.type === "panel-opened") {
      // Opening the panel is the moment someone expects current information.
      // Grades, quiz attempts and submissions are dropped; the slow structural
      // reads that make the panel feel instant are kept.
      void import("../avenue/cache.js").then(async ({ onPanelOpened }) =>
        sendResponse({ ok: true, dropped: await onPanelOpened() }),
      );
      return true;
    }
    if (msg?.type === "clear-cache") {
      void import("../avenue/cache.js").then(async ({ clearCache }) =>
        sendResponse({ ok: true, cleared: await clearCache() }),
      );
      return true;
    }
    if (msg?.type !== "tool") return false;
    void runTool(msg.name, msg.args ?? {}).then(sendResponse);
    return true; // keep the channel open for the async reply
  },
);

chrome.runtime.onInstalled.addListener(() => {
  void chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: true });
});
