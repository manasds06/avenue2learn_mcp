/**
 * Service worker: the only place that talks to Avenue.
 *
 * MV3 kills this worker after ~30s idle. Everything here is short-lived
 * request/response, which is fine; it is also why the RAG sync is deferred
 * (a multi-minute job needs an offscreen document).
 */

import { AvenueError } from "../avenue/errors.js";
import { listCourses } from "../tools/courses.js";

type Handler = (args: Record<string, unknown>) => Promise<unknown>;

/** Tool name -> handler. The Gemini loop and the UI both dispatch through this. */
const HANDLERS: Record<string, Handler> = {
  list_courses: (a) => listCourses(a as { include_inactive?: boolean }),
};

export interface ToolRequest {
  type: "tool";
  name: string;
  args?: Record<string, unknown>;
}

chrome.runtime.onMessage.addListener((msg: ToolRequest, _sender, sendResponse) => {
  if (msg?.type !== "tool") return false;

  void (async () => {
    const handler = HANDLERS[msg.name];
    if (!handler) {
      sendResponse({ ok: false, error: "UnknownTool", message: `No tool named ${msg.name}.` });
      return;
    }
    try {
      sendResponse({ ok: true, result: await handler(msg.args ?? {}) });
    } catch (err) {
      if (err instanceof AvenueError) {
        sendResponse({ ok: false, ...err.toResult() });
      } else {
        console.error("tool failed", msg.name, err);
        sendResponse({
          ok: false,
          error: "Unexpected",
          message: String(err),
          next_step: "This looks like a bug. Check the service worker console.",
        });
      }
    }
  })();

  return true; // keep the channel open for the async reply
});

// Clicking the toolbar icon opens the side panel.
chrome.runtime.onInstalled.addListener(() => {
  void chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: true });
});
