"""CLI: `avenue-mcp serve | login | status | logout`.

Login is deliberately separate from serve. Login needs a visible browser window
and a human; the server runs headless under an MCP client. Conflating them
produces a server that hangs on a window nobody can see.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys

from avenue_mcp.config import get_settings
from avenue_mcp.util.logging import configure_logging


def _setup_logging(level: str) -> None:
    """Kept as a thin alias so the CLI reads the same as before.

    The implementation is shared with `server.run()` -- see util/logging.py for
    why having two copies of this was a credential leak waiting to happen.
    """
    configure_logging(level)


def cmd_serve(_: argparse.Namespace) -> int:
    from avenue_mcp.server import run

    run()
    return 0


def cmd_login(args: argparse.Namespace) -> int:
    from avenue_mcp.auth.login import interactive_login
    from avenue_mcp.errors import LoginTimeoutError

    settings = get_settings()
    settings.ensure_dirs()
    profile = settings.institution_profile
    try:
        interactive_login(
            settings.base_url,
            settings.session_path,
            timeout_seconds=args.timeout,
            headless=False,
            login_url=settings.login_url,
            credential_brand=profile.credential_brand,
            lms_name=profile.lms_name,
        )
    except LoginTimeoutError as exc:
        print(f"\nLogin failed: {exc}", file=sys.stderr)
        print("Run `avenue-mcp login` again and complete sign-in, including 2FA if prompted.", file=sys.stderr)
        return 1
    except RuntimeError as exc:
        print(f"\n{exc}", file=sys.stderr)
        return 1

    print(
        "\nThis session file is equivalent to a logged-in Brightspace session.\n"
        "Do not commit, sync, or share it."
    )
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    from avenue_mcp.context import AppContext
    from avenue_mcp.tools.status import get_status

    async def go() -> dict:
        ctx = AppContext()
        try:
            return await get_status(ctx, check_session=not args.offline)
        finally:
            await ctx.aclose()

    result = asyncio.run(go())
    if args.json:
        print(json.dumps(result, indent=2, default=str))
        return 0

    session = result["session"]
    index = result["index"]
    inst = result["institution"]
    print(f"Institution:   {inst['display']}")
    print(f"Instance:      {result['config']['base_url']}")
    print(f"Session:       present={session['present']} alive={session['alive']}", end="")
    if session.get("age_minutes") is not None:
        print(f" age={session['age_minutes']:.0f}m", end="")
    print()
    print(f"Writes:        {'ENABLED' if result['config']['writes_enabled'] else 'disabled'}")
    print(f"Index:         {index['course_count']} course(s), {index['chunks']} chunk(s)")
    print(f"Embed model:   {index['embed_model'] or '(none yet)'}")
    for course in index["courses_indexed"]:
        print(
            f"  - {course['course_name']}: {course['files']} file(s), "
            f"{course['threads']} thread(s), {course['chunks']} chunk(s)"
        )
    print("\nAdvice:")
    for line in result["advice"]:
        print(f"  * {line}")
    return 0


def cmd_logout(_: argparse.Namespace) -> int:
    settings = get_settings()
    path = settings.session_path
    if path.is_file():
        path.unlink()
        print(f"Removed {path}")
    else:
        print("No saved session.")
    print("Note: this only deletes the local copy. To invalidate the session on")
    print("Brightspace's side, sign out in your browser.")
    return 0


def cmd_reindex(args: argparse.Namespace) -> int:
    from avenue_mcp.context import AppContext

    async def go() -> int:
        ctx = AppContext()
        try:
            if args.all:
                ctx.store.clear_all()
                print("Cleared the entire local index.")
            elif args.course:
                removed = ctx.store.delete_course(args.course)
                print(f"Cleared {removed} document(s) for course {args.course}.")
            else:
                print("Specify --course ORG_UNIT_ID or --all", file=sys.stderr)
                return 1
            print("Run sync_course_materials from your MCP client to rebuild.")
            return 0
        finally:
            await ctx.aclose()

    return asyncio.run(go())


def main(argv: list[str] | None = None) -> int:
    settings = get_settings()
    profile = settings.institution_profile
    parser = argparse.ArgumentParser(
        prog="avenue-mcp",
        description=f"MCP server for {profile.display} (D2L Brightspace).",
    )
    sub = parser.add_subparsers(dest="command")

    p_serve = sub.add_parser("serve", help="Run the MCP server over stdio")
    p_serve.set_defaults(func=cmd_serve)

    p_login = sub.add_parser(
        "login", help=f"Sign in to {profile.lms_name} in a real browser"
    )
    p_login.add_argument(
        "--timeout", type=int, default=300, help="Seconds to wait for sign-in"
    )
    p_login.set_defaults(func=cmd_login)

    p_status = sub.add_parser("status", help="Show session and index state")
    p_status.add_argument("--json", action="store_true")
    p_status.add_argument(
        "--offline", action="store_true", help="Skip the network liveness probe"
    )
    p_status.set_defaults(func=cmd_status)

    p_logout = sub.add_parser("logout", help="Delete the local session file")
    p_logout.set_defaults(func=cmd_logout)

    p_reindex = sub.add_parser("reindex", help="Clear index data so it can be rebuilt")
    p_reindex.add_argument("--course", type=int, help="org_unit_id to clear")
    p_reindex.add_argument("--all", action="store_true", help="Clear everything")
    p_reindex.set_defaults(func=cmd_reindex)

    args = parser.parse_args(argv)
    _setup_logging(get_settings().log_level)

    if not getattr(args, "func", None):
        parser.print_help()
        return 1
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
