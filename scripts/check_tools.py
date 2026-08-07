"""Verify the server starts over stdio and exposes the expected tools.

Phase 1/2 exit check: startup must do no network I/O and require no session,
so this must pass on a machine that has never logged in.
"""

from __future__ import annotations

import asyncio
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

EXPECTED = {
    "list_courses",
    "get_course_content",
    "read_content_file",
    "list_assignments",
    "get_upcoming_deadlines",
    "get_grades",
    "analyze_grade_summary",
    "list_announcements",
    "get_class_list",
}

# Must never appear while AVENUE_MCP_ENABLE_WRITES is off.
FORBIDDEN = {"submit_assignment", "add_submission_comment"}


async def main() -> int:
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "avenue_mcp", "serve"]
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()

    names = {t.name for t in tools.tools}
    print(f"Server started and listed {len(names)} tools.\n")
    for tool in sorted(tools.tools, key=lambda t: t.name):
        first_line = (tool.description or "").strip().split("\n")[0]
        print(f"  {tool.name:<24} {first_line[:78]}")

    ok = True
    if missing := EXPECTED - names:
        print(f"\nMISSING: {sorted(missing)}")
        ok = False
    if extra := names - EXPECTED:
        print(f"\nUNEXPECTED: {sorted(extra)}")
        ok = False
    if leaked := names & FORBIDDEN:
        print(f"\nWRITE TOOLS EXPOSED WITH THE FLAG OFF: {sorted(leaked)}")
        ok = False

    # Every description must state read-only, or the model will hedge.
    for tool in tools.tools:
        if "read-only" not in (tool.description or "").lower():
            print(f"\n{tool.name}: description does not say it is read-only")
            ok = False

    print("\nPASS" if ok else "\nFAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
