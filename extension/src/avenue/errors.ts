/**
 * Error taxonomy, ported from src/avenue_mcp/errors.py.
 *
 * Every error carries a next step in plain language, because the model is the
 * one relaying it to a human. An error that says only "403 Forbidden" produces
 * a useless response.
 *
 * One thing gets SIMPLER here than in the Python version. There, a 403 was
 * genuinely ambiguous — sign-in wall or real permission denial — and had to be
 * resolved with a liveness probe (docs/08). In the extension there is no
 * session we manage: if the user is signed out, the fix is always "open Avenue
 * and sign in", and if they are signed in, a 403 is a permission fact. We can
 * still tell them apart, because a signed-out Avenue answers with HTML while a
 * permission denial answers with JSON.
 */

export type ErrorKind =
  | "NotSignedIn"
  | "PermissionDenied"
  | "NotFound"
  | "InvalidRequest"
  | "Upstream"
  | "Network"
  | "Config";

export class AvenueError extends Error {
  readonly kind: ErrorKind;
  readonly nextStep: string;

  constructor(kind: ErrorKind, message: string, nextStep: string) {
    super(message);
    this.name = "AvenueError";
    this.kind = kind;
    this.nextStep = nextStep;
  }

  /** Shape handed back to the model as a tool result. */
  toResult(): { error: ErrorKind; message: string; next_step: string } {
    return { error: this.kind, message: this.message, next_step: this.nextStep };
  }
}

export const notSignedIn = (detail = "You are not signed in to Avenue.") =>
  new AvenueError(
    "NotSignedIn",
    detail,
    "Open https://avenue.cllmcmaster.ca in a tab and sign in with your MacID, then ask again.",
  );

export const permissionDenied = (route: string) =>
  new AvenueError(
    "PermissionDenied",
    `Your Avenue account cannot read ${route} — it is likely instructor-only.`,
    "This is not a sign-in problem; signing in again will not help. Tell the user this data is not available to student accounts.",
  );

export const notFound = (route: string) =>
  new AvenueError(
    "NotFound",
    `Avenue has no such item at ${route}.`,
    "Check the ID. list_courses returns valid org_unit_id values.",
  );

export const invalidRequest = (route: string, detail: string) =>
  new AvenueError(
    "InvalidRequest",
    `Avenue rejected the request to ${route}. ${detail}`.trim(),
    "Check the parameters passed to this tool.",
  );

export const upstream = (route: string, status: number) =>
  new AvenueError(
    "Upstream",
    `Avenue returned ${status} for ${route}.`,
    "This is an Avenue-side problem. Wait a moment and retry.",
  );

export const network = (route: string, detail: string) =>
  new AvenueError(
    "Network",
    `Could not reach Avenue for ${route}. ${detail}`.trim(),
    "Check your internet connection, then retry.",
  );
