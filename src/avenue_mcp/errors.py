"""Error taxonomy.

Every error surfaced to the model carries an actionable next step in plain
language, because the model is the one that has to relay it to a human.

The SessionExpiredError / PermissionDeniedError split is load-bearing: both
mean "you can't have this", but one is fixed by logging in and the other
never will be. Collapsing them sends users into a re-login loop against a
permission wall.
"""

from __future__ import annotations


class AvenueMCPError(Exception):
    """Base. `hint` is what the model should tell the user to do."""

    hint: str = ""

    def __init__(self, message: str = "", hint: str | None = None) -> None:
        super().__init__(message or self.__class__.__doc__ or "")
        if hint is not None:
            self.hint = hint

    def as_dict(self) -> dict[str, str]:
        return {
            "error": type(self).__name__,
            "message": str(self),
            "next_step": self.hint,
        }


# --- Auth -----------------------------------------------------------------


class AuthError(AvenueMCPError):
    pass


class NoSessionError(AuthError):
    """Not logged in to Avenue."""

    hint = (
        "Run `avenue-mcp login` in a terminal, complete MacID sign-in "
        "(including 2FA), then retry."
    )


class SessionExpiredError(AuthError):
    """The Avenue session expired."""

    hint = "Run `avenue-mcp login` to sign in again, then retry."


class LoginTimeoutError(AuthError):
    """Login did not complete before the window closed."""

    hint = "Run `avenue-mcp login` again and complete MacID + 2FA."


# --- API ------------------------------------------------------------------


class APIError(AvenueMCPError):
    pass


class PermissionDeniedError(APIError):
    """Authenticated, but not permitted."""

    # Deliberately does NOT suggest re-login: the session is fine.
    hint = (
        "Your Avenue account doesn't have access to this -- it may be "
        "instructor-only. This is not a login problem, so signing in again "
        "will not help."
    )


class NotFoundError(APIError):
    """No such course, topic, folder, or thread."""

    hint = "Check the ID. Use `list_courses` to get a valid org_unit_id."


class InvalidRequestError(APIError):
    """The request parameters were rejected."""

    hint = "Check the parameters and retry."


class UpstreamError(APIError):
    """Avenue returned a server error after retries."""

    hint = "Avenue may be having trouble. Wait a minute and retry."


# --- RAG ------------------------------------------------------------------


class RAGError(AvenueMCPError):
    pass


class NotIndexedError(RAGError):
    """This course has not been indexed yet."""

    hint = "Run `sync_course_materials` for this course first, then search again."


class ExtractionError(RAGError):
    """Could not extract text from the file."""

    hint = "The file may be scanned, corrupt, or an unsupported format."


class EmbedModelMismatchError(RAGError):
    """The index was built with a different embedding model."""

    hint = (
        "Re-run `sync_course_materials` with force=true, or restore the "
        "previous AVENUE_MCP_EMBED_MODEL value."
    )


class RenderError(RAGError):
    """Could not render the page as an image."""

    hint = "Only PDF and PPTX can be rendered. PPTX additionally needs LibreOffice installed."


# --- Config ---------------------------------------------------------------


class ConfigError(AvenueMCPError):
    """Configuration problem."""

    hint = "Check the AVENUE_MCP_* environment variables."


class WritesDisabledError(AvenueMCPError):
    """Write operations are not enabled."""

    hint = (
        "Write mode is off. Set AVENUE_MCP_ENABLE_WRITES=1 and restart the "
        "server -- but read docs/07-risks-and-policy.md first: submissions "
        "cannot be undone."
    )
