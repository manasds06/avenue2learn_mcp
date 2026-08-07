/**
 * get_class_list — course staff, and honesty about the roster.
 * Ported from src/avenue_mcp/tools/classlist.py.
 *
 * TWO THINGS HERE ARE LOAD-BEARING AND BOTH WERE LEARNED THE HARD WAY:
 *
 * 1. The route is under `le`, NOT `lp`. The wrong component returns 404, which
 *    the error path records as "roster unavailable" — degrading the tool
 *    permanently on a typo rather than a real permission boundary.
 *
 * 2. Roles arrive in `ClasslistRoleDisplayName`. Not `Role`, `RoleName`, or
 *    `RoleDisplayName` — none of which are present. Measured on this instance:
 *    "Instructor" x1, "TA 1" x10, "Student" x134. With that key missing every
 *    entry normalized to Unknown, all 145 landed in students, and `instructors`
 *    came back empty for a course with 11 staff.
 *
 * AND THE POLICY DECISION: the roster IS available here — 145 people with
 * names, McMaster emails, usernames, and student IDs. docs/02 predicted a 403
 * and the tool was written for that. Returning the array would ship a third of
 * a lecture hall's personal information to a model provider on every call, so
 * students are counted and withheld. docs/07 is unambiguous that this is FIPPA
 * data and that this tool exists to return instructor contacts.
 */

import { avenue } from "../avenue/client.js";
import { AvenueError } from "../avenue/errors.js";
import { isObj, normalizeRole, pick } from "../avenue/models.js";
import { courseName } from "./context.js";

interface Person {
  name: string | null;
  role: ReturnType<typeof normalizeRole>;
  email: string | null;
}

export async function getClassList(args: { org_unit_id: number }) {
  const { org_unit_id } = args;

  let people: Person[] = [];
  let rosterReadable = false;
  let note: string | null = null;

  try {
    const raw = await avenue.getPaged("le", `${org_unit_id}/classlist/`);
    people = raw.filter(isObj).map(toPerson);
    rosterReadable = true;
  } catch (err) {
    if (!(err instanceof AvenueError)) throw err;
    note =
      err.kind === "PermissionDenied"
        ? "The class list is not accessible from a student account on this course — this is expected, not an error."
        : `Could not read the class list: ${err.message}`;
  }

  const staff = people.filter((p) => p.role === "Instructor" || p.role === "TA");
  const students = people.filter((p) => p.role !== "Instructor" && p.role !== "TA");

  if (staff.length && !staff.some((p) => p.email)) {
    // Measured: the classlist populates DisplayName/FirstName/LastName/Username
    // but leaves Email empty for staff. Usernames are present and McMaster
    // addresses are conventionally <macid>@mcmaster.ca — but composing one
    // would be a guess presented as a contact detail.
    note =
      "Avenue did not provide email addresses for course staff. Names and roles " +
      "are accurate; find contact details on the course homepage or in the outline " +
      "rather than guessing an address.";
  }

  if (students.length && !note) {
    note =
      `${students.length} student records were returned by Avenue but are ` +
      `deliberately not included here. A roster with names and emails is personal ` +
      `information under FIPPA, and this tool returns course staff by design.`;
  }

  return {
    org_unit_id,
    course_name: await courseName(org_unit_id),
    instructors: staff,
    // Never returned in bulk, even when Avenue hands them over. The count
    // answers "how big is this class" without handing over the list.
    students: [] as Person[],
    student_roster_returned: false,
    student_count: rosterReadable ? students.length : null,
    note,
  };
}

function toPerson(entry: Record<string, unknown>): Person {
  const nested = pick(entry, "User");
  const user = isObj(nested) ? nested : entry;

  const first = String(pick(user, "FirstName", "GivenName") ?? "");
  const last = String(pick(user, "LastName", "Surname") ?? "");
  const display = String(pick(user, "DisplayName", "FullName") ?? `${first} ${last}`).trim();

  // ClasslistRoleDisplayName FIRST — see the header note. RoleId is an integer
  // and matches no hint, so it is the last resort rather than the first try.
  let role = normalizeRole(
    pick(entry, "ClasslistRoleDisplayName", "Role", "RoleName", "RoleDisplayName", "RoleAlias"),
  );
  if (role === "Unknown") role = normalizeRole(pick(entry, "RoleId"));

  return {
    name: display || null,
    role,
    email: (pick(user, "EmailAddress", "Email", "ExternalEmail") as string) ?? null,
  };
}
