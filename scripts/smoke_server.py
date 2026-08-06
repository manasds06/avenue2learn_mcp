"""End-to-end verification: launch the server over real stdio as an MCP client.

This is the check that matters -- it proves an MCP client can connect, list
tools, and call one, rather than just proving the module imports.
"""

import asyncio
import os
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def main() -> int:
    env = dict(os.environ)
    env.setdefault("AVENUE_MCP_STATE_DIR", "/tmp/avenue-verify")
    env["AVENUE_MCP_LOG_LEVEL"] = "WARNING"
    writes_on = env.get("AVENUE_MCP_ENABLE_WRITES", "0") not in ("0", "", "false", "False")

    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "avenue_mcp", "serve"],
        env=env,
    )

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            print(f"connected to: {init.server_info.name} v{init.server_info.version}")

            tools = await session.list_tools()
            names = sorted(t.name for t in tools.tools)
            print(f"\ntools exposed: {len(names)}")
            for n in names:
                print(f"  - {n}")

            # The gate is checked in whichever direction the config implies.
            # When writes are off the tool must be ABSENT, not present-and-
            # erroring: a model cannot call a tool it cannot see.
            if writes_on:
                assert "submit_assignment" in names, "writes enabled but tool missing!"
                assert len(names) == 18, f"expected 18 tools with writes on, got {len(names)}"
                print("\nwrite gate: writes ENABLED -> submit_assignment present (correct)")
            else:
                assert "submit_assignment" not in names, "write tool leaked with writes off!"
                assert len(names) == 17, f"expected 17 read tools, got {len(names)}"
                print("\nwrite gate: writes off -> submit_assignment absent (correct)")

            # A tool that needs no session and no network.
            print("\ncalling get_status(check_session=False)...")
            result = await session.call_tool("get_status", {"check_session": False})
            text = result.content[0].text if result.content else ""
            print(f"  is_error={result.is_error}")
            print(f"  {text[:300]}")
            assert not result.is_error

            # A tool that DOES need a session -- must fail with a legible hint,
            # not a traceback.
            print("\ncalling list_courses() with no session...")
            result = await session.call_tool("list_courses", {})
            text = result.content[0].text if result.content else ""
            print(f"  is_error={result.is_error}")
            print(f"  {text[:300]}")
            assert "NoSessionError" in text, "expected a typed error"
            assert "avenue-mcp login" in text, "expected an actionable next step"

            # Search must work with no session at all.
            print("\ncalling search_course_materials() with no session...")
            result = await session.call_tool(
                "search_course_materials", {"query": "late penalty"}
            )
            text = result.content[0].text if result.content else ""
            print(f"  is_error={result.is_error}")
            print(f"  {text[:300]}")
            assert "NoSessionError" not in text, "search must not require a session!"

            # Verify a description carries its when-not-to-use guidance.
            search_tool = next(t for t in tools.tools if t.name == "search_course_materials")
            assert "indexed_courses" in (search_tool.description or "")
            print("\ndescriptions: search tool warns about unindexed courses (correct)")

            print("\nALL CHECKS PASSED")
            return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
