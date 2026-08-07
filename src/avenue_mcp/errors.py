"""The error taxonomy from docs/05-architecture.md.

Every error surfaced to the model carries a next action in plain language. The
model is the one relaying it to a human; an error that says only "403 Forbidden"
produces a useless response.

The SessionExpiredError / PermissionDeniedError split is the load-bearing one.
Both are "you can't have this", but one is fixed by logging in and the other
never will be. Collapsing them sends users into a re-login loop against a
permission wall.
"""

from __future__ import annotations


class AvenueMCPError(Exception):
    """Base for everything this server raises deliberately."""

    #: Plain-language next step, relayed to the user by the calling model.
    action: str = ""

    def __init__(self, message: str, *, action: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        if action is not None:
            self.action = action

    def to_text(self) -> str:
        """Render for a tool result: what happened, then what to do about it."""
        return f"{self.message} {self.action}".strip() if self.action else self.message


# --- Auth ---------------------------------------------------------------


class AuthError(AvenueMCPError):
    pass


class NoSessionError(AuthError):
    action = "Run `avenue-mcp login` in a terminal, complete MacID sign-in, then retry."

    def __init__(self, message: str = "Not logged in to Avenue.") -> None:
        super().__init__(message)


class SessionExpiredError(AuthError):
    action = "Run `avenue-mcp login` to sign in again, then retry."

    def __init__(self, message: str = "Avenue session expired.") -> None:
        super().__init__(message)


class LoginTimeoutError(AuthError):
    action = "Run `avenue-mcp login` and complete MacID + MFA."

    def __init__(
        self,
        message: str = "Login window closed or timed out before sign-in completed.",
    ) -> None:
        super().__init__(message)


# --- API ----------------------------------------------------------------


class APIError(AvenueMCPError):
    pass


class PermissionDeniedError(APIError):
    # Says "not a login problem" on purpose: a 403 on a healthy session is a
    # permissions fact, and telling the user to re-authenticate sends them
    # down a dead end.
    action = "This isn't a login problem, so re-authenticating won't help."

    def __init__(
        self,
        message: str = (
            "Your Avenue account doesn't have access to this — it may be instructor-only."
        ),
    ) -> None:
        super().__init__(message)


class NotFoundError(APIError):
    action = "Check the ID — `list_courses` returns valid org_unit_id values."

    def __init__(self, message: str = "Avenue has no such item.") -> None:
        super().__init__(message)


class InvalidRequestError(APIError):
    action = "Check the parameters passed to this tool."

    def __init__(self, message: str = "Avenue rejected the request parameters.") -> None:
        super().__init__(message)


class UpstreamError(APIError):
    action = "This is an Avenue-side problem; wait a moment and retry."

    def __init__(
        self, message: str = "Avenue returned a server error after retries."
    ) -> None:
        super().__init__(message)


# --- RAG ----------------------------------------------------------------


class RAGError(AvenueMCPError):
    pass


class NotIndexedError(RAGError):
    action = "Run `sync_course_materials` for it first."

    def __init__(self, course: str = "That course") -> None:
        super().__init__(f"{course} hasn't been indexed yet.")


class ExtractionError(RAGError):
    action = "The file may be scanned, encrypted, or corrupt."

    def __init__(self, message: str = "Couldn't extract text from the file.") -> None:
        super().__init__(message)


class EmbedModelMismatchError(RAGError):
    action = "Re-run `sync_course_materials` with `force: true`."

    def __init__(
        self,
        message: str = "The search index was built with a different embedding model.",
    ) -> None:
        super().__init__(message)


# --- Config -------------------------------------------------------------


class ConfigError(AvenueMCPError):
    action = "Check the AVENUE_MCP_* environment variables."
