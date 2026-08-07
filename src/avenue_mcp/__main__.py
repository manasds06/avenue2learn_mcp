"""CLI entrypoint: `login`, `serve`, `probe`, `status`.

Login is deliberately separate from serve. Login needs a visible browser window
and a human; the server runs headless under an MCP client. Conflating them
produces a server that hangs on a window nobody can see.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from avenue_mcp import __version__
from avenue_mcp.config import get_settings
from avenue_mcp.errors import AvenueMCPError


def _cmd_login() -> int:
    from avenue_mcp.auth.login import login_sync

    try:
        result = login_sync()
    except AvenueMCPError as exc:
        print(f"\nLogin failed: {exc.to_text()}", file=sys.stderr)
        return 1

    print("\nSigned in.")
    print(f"  session file : {result['session_path']}")
    print(f"  cookies      : {result['cookies_captured']}")
    print(f"  bearer token : {'captured' if result['bearer_captured'] else 'not seen'}")
    print(f"  permissions  : {result['permissions'][:300]}")
    print("\nNext: `avenue-mcp probe` to check what your account can reach.")
    return 0


def _cmd_serve() -> int:
    from avenue_mcp.server import main as serve_main

    serve_main()
    return 0


def _cmd_probe() -> int:
    import os

    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "scripts"))
    from probe import main as probe_main  # type: ignore[import-not-found]

    return probe_main()


def _cmd_status() -> int:
    """Whether a session exists and which auth strategy it resolves to."""
    from avenue_mcp.auth.filelock import describe_permissions
    from avenue_mcp.auth.session import SessionManager

    settings = get_settings()
    manager = SessionManager(settings)

    print(f"base URL    : {settings.base_url}")
    print(f"state dir   : {settings.state_dir}")
    print(f"session file: {manager.session_path}")

    if not manager.has_session():
        print("\nNot logged in. Run `avenue-mcp login`.")
        return 1

    print(f"permissions : {describe_permissions(manager.session_path)[:300]}")

    async def _check() -> int:
        try:
            provider = await manager.resolve_provider()
        except AvenueMCPError as exc:
            print(f"\nSession is not usable: {exc.to_text()}")
            return 1
        finally:
            await manager.aclose()

        print(f"\nAuth strategy: {provider.strategy.value}")
        if provider.needs_browser_to_refresh:
            print(
                "  Note: this strategy expires roughly hourly and can only be "
                "refreshed by re-running `avenue-mcp login`."
            )
        else:
            print("  The browser is only needed to log in, not to run.")
        return 0

    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    return asyncio.run(_check())


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="avenue-mcp",
        description="MCP server for Avenue to Learn (McMaster D2L Brightspace). Read-only.",
    )
    parser.add_argument("--version", action="version", version=f"avenue-mcp {__version__}")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("login", help="Sign in to Avenue in a browser and save the session")
    sub.add_parser("serve", help="Run the MCP server over stdio")
    sub.add_parser("probe", help="Phase 0: find out what your account can reach")
    sub.add_parser("status", help="Show session state and the active auth strategy")

    args = parser.parse_args()

    match args.command:
        case "login":
            return _cmd_login()
        case "serve":
            return _cmd_serve()
        case "probe":
            return _cmd_probe()
        case "status":
            return _cmd_status()
        case _:
            parser.print_help()
            return 0


if __name__ == "__main__":
    raise SystemExit(main())
