/**
 * Step 3 checkpoint UI.
 *
 * Deliberately does one thing: call list_courses and show the result. If this
 * returns real courses, the whole design is sound — an extension can reach
 * Avenue using the user's own session without ever seeing the cookie. If it
 * doesn't, nothing else is worth building.
 *
 * Replaced by the chat panel in step 6.
 */

const button = document.getElementById("check") as HTMLButtonElement;
const statusEl = document.getElementById("status") as HTMLDivElement;
const out = document.getElementById("out") as HTMLPreElement;

interface ToolOk {
  ok: true;
  result: { courses: Array<{ org_unit_id: number; name: string }>; count: number };
}
interface ToolErr {
  ok: false;
  error: string;
  message: string;
  next_step?: string;
}

button.addEventListener("click", async () => {
  button.disabled = true;
  statusEl.className = "";
  statusEl.textContent = "Asking Avenue…";
  out.textContent = "";

  try {
    const resp: ToolOk | ToolErr = await chrome.runtime.sendMessage({
      type: "tool",
      name: "list_courses",
      args: {},
    });

    if (resp.ok) {
      const { courses, count } = resp.result;
      statusEl.className = "ok";
      statusEl.textContent =
        `${count} active course${count === 1 ? "" : "s"}.\n` +
        `The cookie premise holds — this data came back without the extension ever reading your session cookie.`;
      out.textContent = courses
        .map((c) => `${c.org_unit_id}  ${c.name}`)
        .join("\n");
    } else {
      statusEl.className = "err";
      statusEl.textContent = [resp.message, resp.next_step].filter(Boolean).join("\n\n");
    }
  } catch (err) {
    statusEl.className = "err";
    statusEl.textContent =
      `Could not reach the extension's service worker.\n\n${String(err)}`;
  } finally {
    button.disabled = false;
  }
});
