"""Third-party logger levels, capped independently of ours.

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

Enforced by tests/test_credential_logging.py, which fails if the cap is removed.
"""

from __future__ import annotations

import logging

# INFO is the ceiling: every leak measured above lives at DEBUG.
#
# httpcore is the one that matters -- it handles raw headers. The rest are capped
# because they are noisy at DEBUG and sit next to credentials: playwright drives
# the login browser, and urllib3/asyncio are transitive.
_NOISY: tuple[str, ...] = (
    "httpcore",
    "httpx",
    "playwright",
    "urllib3",
    "asyncio",
)


def cap_third_party_loggers(ceiling: int = logging.INFO) -> None:
    """Stop third-party libraries from logging below `ceiling`.

    Idempotent, and safe to call before or after `logging.basicConfig`. Applies
    to each named logger's children too, since `httpcore.http11` -- the one that
    logs headers -- is a child rather than the parent itself.
    """
    for name in _NOISY:
        logger = logging.getLogger(name)
        if logger.level == logging.NOTSET or logger.level < ceiling:
            logger.setLevel(ceiling)
