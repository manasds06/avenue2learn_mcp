/**
 * Grades — and the one place a plausible wrong number does real damage.
 * Ported from src/avenue_mcp/tools/grades.py.
 *
 * Brightspace exposes gradebook NUMBERS, not gradebook RULES. It does not
 * reliably tell you about drop-lowest rules, bonus items, nested category
 * weights, or whether a blank cell means "not marked yet" or "you didn't
 * submit". A naive weighted average is therefore silently wrong in a
 * meaningful fraction of real courses.
 *
 * So: compute over graded items only, never impute zeros, report the
 * denominator explicitly, and REFUSE rather than guess when the weights do not
 * reconcile. A confidently wrong "you need 74% on the final" is worse than an
 * honest "I can't compute that". The probed McMaster course declares weights
 * summing to 120%, so this refusal path is the normal one, not an edge case.
 */

import { avenue } from "../avenue/client.js";
import { AvenueError } from "../avenue/errors.js";
import { asFloat, asInt, isObj, pick, richText, toText } from "../avenue/models.js";
import { capability, describeDenial } from "../institutions.js";
import { getCurrentInstitution } from "../settings.js";
import { courseName } from "./context.js";

/** Outside this band the gradebook uses a rule we cannot see. */
const WEIGHT_TOLERANCE = 2.0;

interface GradeItem {
  name: string | null;
  category: string | null;
  points_earned: number | null;
  points_possible: number | null;
  percentage: number | null;
  weight: number | null;
  is_graded: boolean;
  displayed_grade: string | null;
  feedback_text: string | null;
}

export async function getGrades(args: { org_unit_id: number }) {
  const { org_unit_id } = args;
  const { items, weightsAvailable, notes } = await collect(org_unit_id);

  return {
    org_unit_id,
    course_name: await courseName(org_unit_id),
    items,
    count: items.length,
    weights_available: weightsAvailable,
    notes,
  };
}

export async function analyzeGradeSummary(args: {
  org_unit_id: number;
  target_percentage?: number;
}) {
  const { org_unit_id } = args;
  const target = args.target_percentage ?? null;
  const { items, weightsAvailable, notes } = await collect(org_unit_id);
  const caveats = [...notes];

  const graded = items.filter((i) => i.is_graded && i.percentage !== null);
  const ungraded = items.filter((i) => !i.is_graded);
  // Items marked graded but with no usable percentage (a letter or pass/fail
  // scale) belong to NEITHER bucket. Their weight still counts toward the
  // total, so remaining_weight came out short and the projection was
  // confidently wrong — e.g. telling a student an A is unreachable when it is
  // not. Tracked explicitly, and they force the projection to refuse.
  const unscored = items.filter((i) => i.is_graded && i.percentage === null);

  const result: Record<string, unknown> = {
    org_unit_id,
    course_name: await courseName(org_unit_id),
    graded_item_count: graded.length,
    ungraded_item_count: ungraded.length,
    weights_available: weightsAvailable,
    target: null,
    caveats,
  };

  // Unweighted fallback: a points average is still honest as long as we say
  // that is what it is.
  const ptsEarned = sum(graded.map((i) => i.points_earned ?? 0));
  const ptsPossible = sum(graded.map((i) => i.points_possible ?? 0));
  if (ptsPossible > 0) {
    result["points_average"] = round(100 * (ptsEarned / ptsPossible));
    result["points_earned"] = round(ptsEarned);
    result["points_possible"] = round(ptsPossible);
  }

  if (!weightsAvailable) {
    caveats.push(
      "Grade weights are not accessible from a student account here, so a " +
        "weighted standing and any target projection cannot be computed. The " +
        "points average above ignores weighting.",
    );
    result["current_weighted_average"] = null;
    result["graded_weight"] = null;
    result["remaining_weight"] = null;
    return result;
  }

  const gradedWeight = sum(graded.map((i) => i.weight ?? 0));
  const totalWeight = sum(items.map((i) => i.weight ?? 0));
  const remainingWeight = sum(ungraded.map((i) => i.weight ?? 0));
  const unscoredWeight = sum(unscored.map((i) => i.weight ?? 0));

  result["graded_weight"] = round(gradedWeight);
  result["remaining_weight"] = round(remainingWeight);
  result["total_weight_declared"] = round(totalWeight);
  result["unaccounted_weight"] = round(unscoredWeight);

  // The buckets must reconcile against the declared total, or the projection
  // denominator is wrong and any number it produces is fiction.
  const reconciles =
    Math.abs(gradedWeight + remainingWeight + unscoredWeight - totalWeight) <= 0.01;
  const sumsTo100 = Math.abs(totalWeight - 100) <= WEIGHT_TOLERANCE;
  const projectionSafe = sumsTo100 && reconciles && unscoredWeight === 0;

  if (!sumsTo100) {
    caveats.push(
      `Declared weights sum to ${totalWeight.toFixed(1)}%, not 100%. The gradebook ` +
        `likely uses a rule the API does not expose (dropped lowest, bonus items, ` +
        `or nested category weights), so treat any projection as unreliable.`,
    );
  }
  if (unscoredWeight > 0) {
    const names = unscored.map((i) => i.name).filter(Boolean).join(", ");
    caveats.push(
      `${unscoredWeight.toFixed(1)}% of the grade is on item(s) marked graded but ` +
        `with no usable score (${names}). They count as neither earned nor ` +
        `remaining, so a target projection would be wrong.`,
    );
  }

  if (gradedWeight <= 0) {
    result["current_weighted_average"] = null;
    caveats.push("Nothing with a weight has been graded yet.");
    return result;
  }

  const weighted = sum(graded.map((i) => (i.percentage ?? 0) * (i.weight ?? 0)));
  result["current_weighted_average"] = round(weighted / gradedWeight);
  result["weighted_points_of_final_grade"] = round(weighted / 100);

  if (target !== null) {
    if (remainingWeight <= 0) {
      caveats.push(
        "There is no remaining weighted work, so no target is achievable — the grade is effectively final.",
      );
    } else if (!projectionSafe) {
      caveats.push(
        "Target projection withheld: the weights do not reconcile, so any number " +
          "computed here would be misleading. Check the gradebook directly.",
      );
    } else {
      const have = weighted / 100;
      const required = (100 * (target - have)) / remainingWeight;
      result["target"] = {
        target_percentage: target,
        required_average_on_remaining: round(required),
        achievable: required <= 100,
        already_achieved: required <= 0,
        remaining_weight: round(remainingWeight),
      };
    }
  }

  // Letter grades are never claimed: cutoffs vary by faculty and inventing one
  // is a wrong answer dressed as a fact.
  result["letter_estimate"] = null;
  caveats.push(
    "Letter grades are not estimated — cutoffs vary by faculty and program, so " +
      "any letter here would be a guess.",
  );

  return result;
}

// --- collection -------------------------------------------------------------

async function collect(orgUnitId: number): Promise<{
  items: GradeItem[];
  weightsAvailable: boolean;
  notes: string[];
}> {
  const notes: string[] = [];
  const values = await gradeValues(orgUnitId);

  let objects: Record<string, unknown>[] = [];
  let weightsAvailable = true;

  try {
    const raw = await avenue.getPaged("le", `${orgUnitId}/grades/`);
    objects = raw.filter(isObj);
  } catch (err) {
    weightsAvailable = false;
    if (err instanceof AvenueError && err.kind === "PermissionDenied") {
      const inst = await getCurrentInstitution();
      notes.push(
        `The gradebook structure could not be read. ${describeDenial(inst, "grade_objects")}`,
      );
    } else if (err instanceof AvenueError) {
      notes.push(`Could not read gradebook structure: ${err.message}`);
    } else {
      throw err;
    }
  }

  const byId = new Map<number, Record<string, unknown>>();
  for (const obj of objects) {
    const oid = asInt(pick(obj, "Id", "GradeObjectId"));
    if (oid !== null) byId.set(oid, obj);
  }

  if (objects.length && !objects.some((o) => asFloat(pick(o, "Weight")))) {
    weightsAvailable = false;
    notes.push("The gradebook exposes no weights, so weighting is unknown.");
  }

  const items: GradeItem[] = [];
  const matched = new Set<number>();

  for (const val of values) {
    const oid = asInt(pick(val, "GradeObjectIdentifier", "GradeObjectId", "Id"));
    if (oid !== null) matched.add(oid);
    items.push(merge(val, oid !== null ? byId.get(oid) : undefined));
  }

  // Ungraded items exist only in the structure. They matter: remaining weight
  // is what makes "what do I need on the final" answerable at all.
  for (const [oid, obj] of byId) {
    if (!matched.has(oid)) items.push(merge(undefined, obj));
  }

  return { items, weightsAvailable, notes };
}

function merge(
  value?: Record<string, unknown>,
  obj?: Record<string, unknown>,
): GradeItem {
  let name = obj ? (pick(obj, "Name", "ShortName", "Title") as string | undefined) : undefined;
  if (!name && value) name = pick(value, "GradeObjectName", "Name") as string | undefined;

  let earned: number | null = null;
  let possible: number | null = null;
  let percentage: number | null = null;
  let isGraded = false;
  let displayed: string | null = null;

  if (value) {
    earned = asFloat(pick(value, "PointsNumerator", "PointsEarned", "Points"));
    possible = asFloat(pick(value, "PointsDenominator", "PointsPossible", "OutOf"));
    const wNum = asFloat(pick(value, "WeightedNumerator"));
    const wDen = asFloat(pick(value, "WeightedDenominator"));
    const display = pick(value, "DisplayedGrade", "Grade");

    if (earned !== null && possible) percentage = round(100 * (earned / possible));
    else if (wNum !== null && wDen) percentage = round(100 * (wNum / wDen));

    // A blank cell may mean "not marked yet" OR "you didn't submit". We do NOT
    // impute a zero — the single easiest way to report a dramatically wrong
    // standing.
    isGraded = earned !== null || wNum !== null;

    // Letter- and pass/fail-scale items carry a DisplayedGrade with no points.
    // They ARE graded, and treating them as ungraded inflated remaining_weight.
    if (!isGraded && typeof display === "string" && display.trim()) {
      isGraded = true;
      displayed = display.trim();
    }
  }

  if (possible === null && obj) {
    possible = asFloat(pick(obj, "MaxPoints", "OutOf", "PointsPossible"));
  }

  let category: string | null = null;
  if (obj) {
    const cat = pick(obj, "Category", "CategoryId");
    category = isObj(cat) ? ((pick(cat, "Name") as string) ?? null) : cat != null ? String(cat) : null;
  }

  const feedback = value ? toText(richText(pick(value, "Comments", "Feedback"))) : "";

  return {
    name: name ?? null,
    category,
    points_earned: earned,
    points_possible: possible,
    percentage,
    weight: obj ? asFloat(pick(obj, "Weight")) : null,
    is_graded: isGraded,
    displayed_grade: displayed,
    feedback_text: feedback || null,
  };
}

/** Grades are never cached — a stale grade is a bad answer. */
async function gradeValues(orgUnitId: number): Promise<Record<string, unknown>[]> {
  const data = await avenue.get("le", `${orgUnitId}/grades/values/myGradeValues/`);
  if (Array.isArray(data)) return data.filter(isObj);
  if (isObj(data)) {
    const objs = pick(data, "Objects", "Items");
    return Array.isArray(objs) ? objs.filter(isObj) : [data];
  }
  return [];
}

const sum = (xs: number[]): number => xs.reduce((a, b) => a + b, 0);
const round = (n: number): number => Math.round(n * 100) / 100;

/** Exposed so the registry can report what this instance is known to allow. */
export async function gradeCapability(): Promise<string> {
  return capability(await getCurrentInstitution(), "grade_objects");
}
