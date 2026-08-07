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
  | "Config"
  /** The user has not granted this extension access to their school's host. */
  | "HostNotGranted";

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

/**
 * Messages name the school's own brand — "Avenue to Learn" for McMaster,
 * "Brightspace" for Carleton — because telling a Carleton student to sign in
 * to Avenue is simply wrong.
 */
export interface Brand {
  lmsName: string;
  credentialBrand: string;
  loginUrl: string;
}

export const notSignedIn = (brand: Brand, detail?: string) =>
  new AvenueError(
    "NotSignedIn",
    detail ?? `You are not signed in to ${brand.lmsName}.`,
    `Open ${brand.loginUrl} in a tab and sign in with your ${brand.credentialBrand}, then ask again.`,
  );

export const hostNotGranted = (brand: Brand, origin: string) =>
  new AvenueError(
    "HostNotGranted",
    `This extension has not been granted access to ${brand.lmsName} (${origin}).`,
    "Open the side panel and choose your school — Chrome will ask you to allow access. This has to be a click, so it cannot be done automatically.",
  );

export const permissionDenied = (route: string, brand: Brand, weight?: string) =>
  new AvenueError(
    "PermissionDenied",
    `Your ${brand.lmsName} account cannot read ${route} — it is likely instructor-only.` +
      (weight ? ` ${weight}` : ""),
    "This is not a sign-in problem; signing in again will not help. Tell the user this data is not available to student accounts.",
  );

export const notFound = (route: string) =>
  new AvenueError(
    "NotFound",
    `Brightspace has no such item at ${route}.`,
    "Check the ID. list_courses returns valid org_unit_id values.",
  );

export const invalidRequest = (route: string, detail: string) =>
  new AvenueError(
    "InvalidRequest",
    `Brightspace rejected the request to ${route}. ${detail}`.trim(),
    "Check the parameters passed to this tool.",
  );

export const upstream = (route: string, status: number) =>
  new AvenueError(
    "Upstream",
    `Brightspace returned ${status} for ${route}.`,
    "This is a server-side problem. Wait a moment and retry.",
  );

export const network = (route: string, detail: string) =>
  new AvenueError(
    "Network",
    `Could not reach your school's Brightspace for ${route}. ${detail}`.trim(),
    "Check your internet connection, then retry.",
  );
