"""Grades, and the one place a plausible wrong number does real damage.

Brightspace exposes gradebook NUMBERS, not gradebook RULES. It does not reliably
tell you about drop-lowest rules, bonus items, nested category weights, or
whether a blank cell means "not marked yet" or "you didn't submit". A naive
weighted average is therefore silently wrong in a meaningful fraction of real
courses.

So this module: computes over graded items only, never imputes zeros, reports the
denominator explicitly, populates caveats when the gradebook shape suggests a
rule the API didn't expose, and REFUSES rather than guesses when weights don't
reconcile. A confidently wrong "you need 74% on the final" is worse than an
honest "I can't compute that".
"""

from __future__ import annotations

import logging
from typing import Any

from avenue_mcp.client import models as m
from avenue_mcp.context import AppContext
from avenue_mcp.errors import APIError, PermissionDeniedError
from avenue_mcp.util.html import to_text

log = logging.getLogger(__name__)

# If summed weights land outside this band, the gradebook is using a rule we
# cannot see and projections are not trustworthy.
_WEIGHT_TOLERANCE = 2.0


async def get_grades(ctx: AppContext, org_unit_id: int) -> dict[str, Any]:
    await ctx.require_session()
    items, weights_available, notes = await _collect(ctx, org_unit_id)

    return {
        "org_unit_id": org_unit_id,
        "course_name": await ctx.course_name(org_unit_id),
        "items": items,
        "count": len(items),
        "weights_available": weights_available,
        "notes": notes,
    }


async def analyze_grade_summary(
    ctx: AppContext, org_unit_id: int, target_percentage: float | None = None
) -> dict[str, Any]:
    await ctx.require_session()
    items, weights_available, notes = await _collect(ctx, org_unit_id)
    caveats: list[str] = list(notes)

    graded = [i for i in items if i["is_graded"] and i["percentage"] is not None]
    ungraded = [i for i in items if not i["is_graded"]]
    # Items marked graded but with no usable percentage (points recorded, no
    # denominator) belong to NEITHER bucket. Their weight was still counted in
    # total_weight, so remaining_weight came out short and the projection was
    # confidently wrong -- e.g. telling a student an A is unreachable when it
    # isn't. They are tracked explicitly and force the projection to refuse.
    unscored = [
        i for i in items if i["is_graded"] and i["percentage"] is None
    ]

    result: dict[str, Any] = {
        "org_unit_id": org_unit_id,
        "course_name": await ctx.course_name(org_unit_id),
        "graded_item_count": len(graded),
        "ungraded_item_count": len(ungraded),
        "weights_available": weights_available,
        "target": None,
        "caveats": caveats,
    }

    # Unweighted fallback: a points-based average is still honest, as long as we
    # say that is what it is.
    pts_earned = sum(i["points_earned"] or 0 for i in graded if i["points_earned"] is not None)
    pts_possible = sum(
        i["points_possible"] or 0 for i in graded if i["points_possible"] is not None
    )
    if pts_possible > 0:
        result["points_average"] = round(100.0 * pts_earned / pts_possible, 2)
        result["points_earned"] = round(pts_earned, 2)
        result["points_possible"] = round(pts_possible, 2)

    if not weights_available:
        caveats.append(
            "Grade weights are not accessible from a student account on this "
            "instance, so a weighted standing and any target projection cannot "
            "be computed. The points average above ignores weighting."
        )
        result["current_weighted_average"] = None
        result["graded_weight"] = None
        result["remaining_weight"] = None
        return result

    graded_weight = sum(i["weight"] or 0 for i in graded)
    total_weight = sum(i["weight"] or 0 for i in items)
    remaining_weight = sum(i["weight"] or 0 for i in ungraded)
    unscored_weight = sum(i["weight"] or 0 for i in unscored)

    result["graded_weight"] = round(graded_weight, 2)
    result["remaining_weight"] = round(remaining_weight, 2)
    result["total_weight_declared"] = round(total_weight, 2)
    result["unaccounted_weight"] = round(unscored_weight, 2)

    # The buckets must reconcile against the declared total, or the projection
    # denominator is wrong and any number it produces is fiction.
    reconciles = abs((graded_weight + remaining_weight + unscored_weight) - total_weight) <= 0.01
    weights_sum_to_100 = abs(total_weight - 100.0) <= _WEIGHT_TOLERANCE
    projection_safe = weights_sum_to_100 and reconciles and unscored_weight == 0

    if not weights_sum_to_100:
        caveats.append(
            f"Declared weights sum to {total_weight:.1f}%, not 100%. The "
            "gradebook likely uses a rule the API does not expose (dropped "
            "lowest, bonus items, or nested category weights), so treat any "
            "projection as unreliable."
        )
    if unscored_weight > 0:
        names = ", ".join(str(i["name"]) for i in unscored if i["name"])
        caveats.append(
            f"{unscored_weight:.1f}% of the grade is on item(s) that are marked "
            f"graded but have no usable score ({names}). They count as neither "
            "earned nor remaining, so a target projection would be wrong."
        )

    if graded_weight <= 0:
        result["current_weighted_average"] = None
        caveats.append("Nothing with a weight has been graded yet.")
        return result

    weighted = sum((i["percentage"] or 0) * (i["weight"] or 0) for i in graded)
    current = weighted / graded_weight
    result["current_weighted_average"] = round(current, 2)
    result["weighted_points_of_final_grade"] = round(weighted / 100.0, 2)

    # --- target projection ------------------------------------------------
    if target_percentage is not None:
        if remaining_weight <= 0:
            caveats.append(
                "There is no remaining weighted work, so no target is achievable "
                "-- the grade is effectively final."
            )
        elif not projection_safe:
            caveats.append(
                "Target projection withheld: the weights do not reconcile, so "
                "any number computed here would be misleading. Check the "
                "gradebook on Avenue directly."
            )
        else:
            have = weighted / 100.0
            need = target_percentage - have
            required = 100.0 * need / remaining_weight
            result["target"] = {
                "target_percentage": target_percentage,
                "required_average_on_remaining": round(required, 2),
                "achievable": required <= 100.0,
                "already_achieved": required <= 0.0,
                "remaining_weight": round(remaining_weight, 2),
            }

    scale = ctx.settings.load_grade_scale()
    if scale and result.get("current_weighted_average") is not None:
        result["letter_estimate"] = _letter(result["current_weighted_average"], scale)
    else:
        # Emitted whether or not a target was requested. Gating this on
        # `target_percentage is None` meant the caveat vanished in exactly the
        # case where letters matter most -- someone asking "what do I need for
        # an A-?" got no letter and no explanation of why.
        result["letter_estimate"] = None
        caveats.append(
            "No letter-grade scale is configured, so letter grades are not "
            "claimed. Set AVENUE_MCP_GRADE_SCALE to a cutoff table if you want "
            "them -- cutoffs vary by faculty, so none is assumed."
        )

    return result


def _letter(percentage: float, scale: dict[str, float]) -> str | None:
    best: tuple[str, float] | None = None
    for letter, cutoff in scale.items():
        if percentage >= cutoff and (best is None or cutoff > best[1]):
            best = (letter, cutoff)
    return best[0] if best else None


async def _collect(
    ctx: AppContext, org_unit_id: int
) -> tuple[list[dict[str, Any]], bool, list[str]]:
    notes: list[str] = []

    values = await _grade_values(ctx, org_unit_id)

    objects: list[dict[str, Any]] = []
    weights_available = True
    try:
        raw = await ctx.client.get_paged("le", f"{org_unit_id}/grades/")
        objects = [o for o in raw if isinstance(o, dict)]
    except PermissionDeniedError:
        weights_available = False
        notes.append("The gradebook structure is instructor-only on this instance.")
    except APIError as exc:
        weights_available = False
        notes.append(f"Could not read gradebook structure: {exc}")

    by_id: dict[int, dict[str, Any]] = {}
    for obj in objects:
        oid = m.as_int(m.pick(obj, "Id", "GradeObjectId"))
        if oid is not None:
            by_id[oid] = obj

    if objects and not any(m.as_float(m.pick(o, "Weight")) for o in objects):
        weights_available = False
        notes.append("The gradebook exposes no weights, so weighting is unknown.")

    items: list[dict[str, Any]] = []
    matched: set[int] = set()

    for val in values:
        oid = m.as_int(m.pick(val, "GradeObjectIdentifier", "GradeObjectId", "Id"))
        obj = by_id.get(oid) if oid is not None else None
        if oid is not None:
            matched.add(oid)
        items.append(_merge(val, obj))

    # Ungraded items exist only in the structure. They matter: remaining weight
    # is what makes "what do I need on the final" answerable at all.
    for oid, obj in by_id.items():
        if oid in matched:
            continue
        items.append(_merge(None, obj))

    return items, weights_available, notes


def _merge(value: dict[str, Any] | None, obj: dict[str, Any] | None) -> dict[str, Any]:
    name = None
    if obj:
        name = m.pick(obj, "Name", "ShortName", "Title")
    if not name and value:
        name = m.pick(value, "GradeObjectName", "Name")

    earned = None
    possible = None
    percentage = None
    is_graded = False
    displayed: str | None = None

    if value:
        earned = m.as_float(m.pick(value, "PointsNumerator", "PointsEarned", "Points"))
        possible = m.as_float(m.pick(value, "PointsDenominator", "PointsPossible", "OutOf"))
        weighted_num = m.as_float(m.pick(value, "WeightedNumerator"))
        weighted_den = m.as_float(m.pick(value, "WeightedDenominator"))
        display = m.pick(value, "DisplayedGrade", "Grade")

        if earned is not None and possible:
            percentage = round(100.0 * earned / possible, 2)
        elif weighted_num is not None and weighted_den:
            percentage = round(100.0 * weighted_num / weighted_den, 2)

        # A blank cell may mean "not marked yet" OR "you didn't submit". We do
        # NOT impute a zero -- that is the single easiest way to report a
        # dramatically wrong standing.
        is_graded = earned is not None or weighted_num is not None

        # Letter- and pass/fail-scale items carry a DisplayedGrade with no
        # points. They ARE graded, and treating them as ungraded inflated
        # remaining_weight. Recorded with percentage=None so the reconciliation
        # check above catches them rather than a projection quietly using a
        # wrong denominator.
        if not is_graded and isinstance(display, str) and display.strip():
            is_graded = True
            displayed = display.strip()

    if possible is None and obj:
        possible = m.as_float(m.pick(obj, "MaxPoints", "OutOf", "PointsPossible"))

    weight = m.as_float(m.pick(obj, "Weight")) if obj else None
    category = None
    if obj:
        cat = m.pick(obj, "Category", "CategoryId")
        if isinstance(cat, dict):
            category = m.pick(cat, "Name")
        elif cat is not None:
            category = str(cat)

    feedback = None
    if value:
        raw_fb = m.pick(value, "Comments", "Feedback", default="")
        if isinstance(raw_fb, dict):
            raw_fb = m.pick(raw_fb, "Html", "Text", default="")
        feedback = to_text(str(raw_fb or "")) or None

    return {
        "name": name,
        "category": category,
        "points_earned": earned,
        "points_possible": possible,
        "percentage": percentage,
        "weight": weight,
        "is_graded": is_graded,
        "displayed_grade": displayed,
        "feedback_text": feedback,
    }


async def _grade_values(ctx: AppContext, org_unit_id: int) -> list[dict[str, Any]]:
    """Grades are never cached -- a stale grade is a bad answer."""
    data = await ctx.client.get(
        "le", f"{org_unit_id}/grades/values/myGradeValues/", cache=False
    )
    if isinstance(data, list):
        return [d for d in data if isinstance(d, dict)]
    if isinstance(data, dict):
        objs = m.pick(data, "Objects", "Items")
        if isinstance(objs, list):
            return [d for d in objs if isinstance(d, dict)]
        return [data]
    return []
