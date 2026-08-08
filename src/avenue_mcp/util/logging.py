"""Logging setup, in one place, with third-party levels capped.

`AVENUE_MCP_LOG_LEVEL=DEBUG` is a reasonable thing for a user to set when a tool
misbehaves. It must not turn into a credential dump.

**Measured, not assumed.** With the root logger at DEBUG and a real socket,
`httpcore` writes the entire response header list into the log. The mechanism is
`httpcore/_trace.py`:

    args = " ".join([f"{key}={value!r}" for key, value in info.items()])
    self.logger.debug(f"{name} {args}")

For `receive_response_headers.complete`, `info["return_value"]` is
`(version, status, reason, headers)` -- every header, verbatim. A canary probe
confirmed `X-Csrf-Token` landing in the log, and `Set-Cookie` travels the same
path, so a Brightspace session-cookie rotation would be written to disk as
plainly as any other header. That is the actual session credential.

The outbound direction is safe and deliberately left alone: httpcore logs
`request=<Request [b'GET']>`, a repr that omits headers, so the cookie jar we
send never appears. The leak is one-directional.

`httpx` is capped at INFO rather than silenced, which keeps
`HTTP Request: GET <url> "200 OK"` -- genuinely useful, and Valence puts no
tokens in URLs. Deleting it would cost real debuggability to fix nothing.

**Why `configure_logging` lives here rather than in the CLI.** There were two
independent `logging.basicConfig` calls -- one in `__main__._setup_logging`, one
in `server.run()` -- and only the first capped anything. `run()` was safe purely
by accident: importing `avenue_mcp.server` constructs an `MCPServer`, whose
`__init__` installs a root handler, which silently turns `run()`'s own
`basicConfig` into a no-op. Safety resting on a third-party import side effect is
not safety. Both callers now go through one function.

Enforced by tests/test_credential_logging.py, including that both process entry
points actually call `configure_logging` -- the earlier version of this note
claimed the tests caught a removed cap and they did not, because every test
called the function directly and nothing watched the wiring.

**Not covered here, and not fixable here:** Playwright's verbose channel is the
`DEBUG=pw:protocol` / `DEBUG=pw:*` environment variable, not Python logging. Its
Node driver writes protocol frames -- including `Set-Cookie` and, in a real SSO
flow, the IdP POST body -- straight to stderr. Confirmed with a canary. Nothing
in this module can intercept that, because no `logging` call is involved. Do not
set those variables while logging into Brightspace.
"""

from __future__ import annotations

import logging
import sys

# INFO is the ceiling: every leak measured above lives at DEBUG.
#
# httpcore is the one that matters -- it handles raw headers, and its
# `httpcore.http11` child is what logs them. The others are capped because they
# are noisy at DEBUG and sit near credentials.
#
# `playwright` is listed for the day it grows a stdlib logger; today it has none
# (see the module docstring), so its entry is inert rather than protective.
_NOISY: tuple[str, ...] = (
    "httpcore",
    "httpx",
    "playwright",
    "urllib3",
    "asyncio",
)


def cap_third_party_loggers(ceiling: int = logging.INFO) -> None:
    """Stop third-party libraries from logging below `ceiling`.

    Idempotent, and safe to call before or after `logging.basicConfig`.

    Setting the parent covers children while they are NOTSET, which is the normal
    case. Children that carry an explicit level of their own are handled too --
    a parent's level is ignored once a child sets one, so capping only the parent
    would leave such a child logging headers.
    """
    for name in _NOISY:
        # NOTSET is 0, so `< ceiling` already covers the unset case. A logger
        # deliberately set quieter than the ceiling (say WARNING) is left alone.
        parent = logging.getLogger(name)
        if parent.level < ceiling:
            parent.setLevel(ceiling)

        prefix = f"{name}."
        for existing in list(logging.root.manager.loggerDict):
            if not existing.startswith(prefix):
                continue
            child = logging.getLogger(existing)
            if child.level != logging.NOTSET and child.level < ceiling:
                child.setLevel(ceiling)


def configure_logging(level: str) -> None:
    """The single logging entry point. Every process start must call this.

    Logs go to stderr because stdout is the MCP transport -- a stray log line on
    stdout corrupts the protocol stream.
    """
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    # After basicConfig, deliberately: our own level may be DEBUG, and the cap is
    # what stops that from reaching httpcore.
    cap_third_party_loggers()
