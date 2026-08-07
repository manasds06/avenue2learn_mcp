/**
 * Per-institution Brightspace profiles.
 *
 * Ported from the Python side's `institutions.py`. Every school runs the same
 * D2L Valence API, so the client and tool layers are institution-agnostic.
 * What differs is the host, the SSO entry point, what the school *calls* its
 * Brightspace instance, and which routes a student account can actually reach.
 *
 * McMaster brands its instance "Avenue to Learn". That name is McMaster's
 * alone — Carleton's is just "Brightspace". Calling Carleton's LMS "Avenue" in
 * text a student reads is simply wrong, which is why `lmsName` lives here.
 *
 * `capabilities` records what an authenticated probe MEASURED, never what the
 * Valence docs predict. An unprobed instance reports "unverified" for
 * everything, and the tools say so rather than borrowing another school's
 * results.
 *
 * THIS ONLY EVER SHAPES PROSE. It must never gate a request: every tool
 * discovers the truth by attempting the route and catching the denial. Skipping
 * a call because a profile says "denied" would hide a route that works.
 */

/**
 * "permitted"/"denied" mean an authenticated probe measured it on THIS
 * instance. "unverified" means nobody has looked, and it is the default for a
 * reason.
 */
export type Capability = "permitted" | "denied" | "unverified";

/** The closed set of routes the probe reports on. */
export const CAPABILITY_KEYS = [
  "course_details",
  "dropbox_folders",
  "dropbox_mysubmissions",
  "grade_objects",
  "grade_weights",
  "calendar_myevents",
  "quizzes",
  "quiz_attempts",
  "classlist",
  "orgunit_users",
] as const;

export type CapabilityKey = (typeof CAPABILITY_KEYS)[number];

export interface Institution {
  id: string;
  orgName: string;
  /** What the school calls its Brightspace. Never assume "Avenue". */
  lmsName: string;
  baseUrl: string;
  /** Where sign-in STARTS. Not always the same host as baseUrl. */
  loginUrl: string;
  /** What the school calls its credentials, e.g. "MacID". */
  credentialBrand: string;
  timezone: string;
  capabilities: Partial<Record<CapabilityKey, Capability>>;
  probedAt: string | null;
  probeDoc: string | null;
}

export const MCMASTER: Institution = {
  id: "mcmaster",
  orgName: "McMaster University",
  lmsName: "Avenue to Learn",
  // avenue.mcmaster.ca is a static Apache landing page, NOT the Brightspace
  // application — every /d2l/* path 404s there. Login STARTS on the landing
  // page (login.php -> Microsoft Entra SAML) and ends on the Brightspace host,
  // so the two are configured separately.
  baseUrl: "https://avenue.cllmcmaster.ca",
  loginUrl: "https://avenue.mcmaster.ca/login.php",
  credentialBrand: "MacID",
  timezone: "America/Toronto",
  capabilities: {
    course_details: "denied",
    dropbox_folders: "permitted",
    // Documented as a Learner route, and blocked anyway. Submission status is
    // simply not knowable here.
    dropbox_mysubmissions: "denied",
    grade_objects: "permitted",
    grade_weights: "permitted",
    calendar_myevents: "permitted",
    quizzes: "permitted",
    quiz_attempts: "denied",
    // Predicted 403 in docs/02, measured readable. get_class_list withholds
    // the roster on FIPPA grounds regardless.
    classlist: "permitted",
    orgunit_users: "denied",
  },
  probedAt: "2026-08-07",
  probeDoc: "docs/08-api-probe-results.md",
};

export const CARLETON: Institution = {
  id: "carleton",
  orgName: "Carleton University",
  // Carleton does not brand its instance. It is "Brightspace".
  lmsName: "Brightspace",
  baseUrl: "https://brightspace.carleton.ca",
  // MUST be the explicit SAML initiator. The root and /d2l/login serve a LOCAL
  // D2L username/password box that MyCarletonOne credentials do not work in.
  loginUrl: "https://brightspace.carleton.ca/d2l/lp/auth/saml/login",
  credentialBrand: "MyCarletonOne",
  timezone: "America/Toronto",
  capabilities: {
    course_details: "denied",
    dropbox_folders: "permitted",
    // Denied, same as McMaster — but it took a per-folder check to see it. A
    // probe that samples folders[0] can hit a leftover scratch folder that
    // returns 200 + [] while every real folder 403s. Verify across folders,
    // never on a sample of one.
    dropbox_mysubmissions: "denied",
    grade_objects: "permitted",
    grade_weights: "permitted",
    calendar_myevents: "permitted",
    quizzes: "permitted",
    quiz_attempts: "denied",
    classlist: "permitted",
    orgunit_users: "denied",
  },
  probedAt: "2026-08-07",
  probeDoc: "docs/09-carleton-probe-results.md",
};

export const INSTITUTIONS: Record<string, Institution> = {
  [MCMASTER.id]: MCMASTER,
  [CARLETON.id]: CARLETON,
};

export const DEFAULT_INSTITUTION_ID = MCMASTER.id;

export function getInstitution(id: string): Institution {
  const found = INSTITUTIONS[id.trim().toLowerCase()];
  if (!found) {
    throw new Error(
      `Unknown institution "${id}". Valid ids: ${Object.keys(INSTITUTIONS).sort().join(", ")}.`,
    );
  }
  return found;
}

export function institutionHost(inst: Institution): string {
  return new URL(inst.baseUrl).host;
}

/** The match pattern this profile needs granted before any request works. */
export function originPattern(inst: Institution): string {
  return `${new URL(inst.baseUrl).origin}/*`;
}

/** Every host the extension might ever need, for optional_host_permissions. */
export function allOriginPatterns(): string[] {
  const out = new Set<string>();
  for (const inst of Object.values(INSTITUTIONS)) {
    out.add(originPattern(inst));
    out.add(`${new URL(inst.loginUrl).origin}/*`);
  }
  return [...out].sort();
}

export function capability(inst: Institution, key: CapabilityKey): Capability {
  return inst.capabilities[key] ?? "unverified";
}

/**
 * One sentence on how much weight to put on a denial.
 *
 * A 403 means the same thing mechanically everywhere, but what we can honestly
 * SAY about it differs. On a probed instance a denial on a known-denied route
 * is expected and permanent. On an unprobed one, the same 403 might be
 * permanent or might be specific to one course — and asserting the former
 * would be borrowing another school's measurements.
 */
export function describeDenial(inst: Institution, key: CapabilityKey): string {
  const status = capability(inst, key);
  if (status === "denied") {
    const doc = inst.probeDoc ? ` (see ${inst.probeDoc})` : "";
    return `This route is denied to student accounts on ${inst.lmsName} and that is expected, not an error${doc}.`;
  }
  if (status === "permitted") {
    return `This route normally works on ${inst.lmsName}, so this denial is more likely specific to this course than a general restriction.`;
  }
  return (
    `API permissions on this ${inst.lmsName} instance have not been systematically ` +
    `verified, so this may be a permanent restriction or specific to this course. ` +
    `Report it as observed here, and do not generalize it to other courses.`
  );
}
