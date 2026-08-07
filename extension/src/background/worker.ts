/**
 * Service worker: the only place that talks to Brightspace.
 *
 * MV3 kills this worker after ~30s idle. Every handler here is short-lived
 * request/response, which is fine — and is also why file indexing is deferred:
 * a multi-minute job needs an offscreen document.
 */

import { AvenueError } from "../avenue/errors.js";
import { TOOLS_BY_NAME } from "../tools/registry.js";

export interface ToolRequest {
  type: "tool";
  name: string;
  args?: Record<string, unknown>;
}

export type ToolResponse =
  | { ok: true; result: unknown }
  | { ok: false; error: string; message: string; next_step?: string };

export async function runTool(name: string, args: Record<string, unknown>): Promise<ToolResponse> {
  const tool = TOOLS_BY_NAME[name];
  if (!tool) {
    return {
      ok: false,
      error: "UnknownTool",
      message: `No tool named ${name}.`,
      next_step: `Available tools: ${Object.keys(TOOLS_BY_NAME).join(", ")}.`,
    };
  }

  try {
    return { ok: true, result: await tool.handler(args) };
  } catch (err) {
    if (err instanceof AvenueError) {
      // Typed errors carry their own next step, which is the whole point —
      // the model has to relay something actionable to the user.
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

chrome.runtime.onMessage.addListener((msg: ToolRequest, _sender, sendResponse) => {
  if (msg?.type !== "tool") return false;
  void runTool(msg.name, msg.args ?? {}).then(sendResponse);
  return true; // keep the channel open for the async reply
});

chrome.runtime.onInstalled.addListener(() => {
  void chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: true });
});
