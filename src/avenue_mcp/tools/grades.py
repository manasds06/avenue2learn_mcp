"""get_grades and analyze_grade_summary (docs/03-mcp-tools.md).

The rule that governs this whole module: **never fabricate a projection from
incomplete weights.** A confidently wrong "you need 74% on the final" is
materially worse than "I can't compute that". Grade math is exactly where a
plausible wrong number does real damage.

Note the two capabilities degrade separately. myGradeValues carries
WeightedNumerator/WeightedDenominator on a weighted gradebook, so a current
standing is often computable even when the structure route is blocked. Only
the forward projection strictly needs the full weight table.
"""

from __future__ import annotations

import logging
from typing import Any

from avenue_mcp.client.d2l import D2LClient
from avenue_mcp.errors import PermissionDeniedError
from avenue_mcp.util.html import html_to_text

log = logging.getLogger(__name__)


def _as_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


async def _fetch_structure(client: D2LClient, org_unit_id: int) -> tuple[dict[int, dict], bool, str]:
    """Grade item definitions — names, max points, weights.

    Returns (by_id, available, reason). A 403 here is a fact to report, not an
    error to propagate: the values route may still answer.
    """
    try:
        raw = await client.get_json("le", f"{org_unit_id}/grades/")
    except PermissionDeniedError:
        return {}, False, "The gradebook structure is not accessible from your account."
    except Exception as exc:  # noqa: BLE001 - degrade, don't fail the whole tool
        log.debug("Grade structure fetch failed: %s", exc)
        return {}, False, f"Could not read the gradebook structure ({type(exc).__name__})."

    by_id: dict[int, dict] = {}
    for item in raw if isinstance(raw, list) else []:
        if isinstance(item, dict) and item.get("Id") is not None:
            by_id[int(item["Id"])] = item
    return by_id, True, ""


async def get_grades(client: D2LClient, *, org_unit_id: int) -> dict[str, Any]:
    """Per-item grades for one course."""
    values = await client.get_json("le", f"{org_unit_id}/grades/values/myGradeValues/")
    structure, weights_available, reason = await _fetch_structure(client, org_unit_id)

    items: list[dict[str, Any]] = []
    for value in values if isinstance(values, list) else []:
        if not isinstance(value, dict):
            continue

        object_id = value.get("GradeObjectIdentifier")
        try:
            definition = structure.get(int(object_id)) if object_id is not None else None
        except (TypeError, ValueError):
            definition = None

        earned = _as_float(value.get("PointsNumerator"))
        possible = _as_float(value.get("PointsDenominator"))
        if possible is None and definition:
            possible = _as_float(definition.get("MaxPoints"))

        weight = _as_float((definition or {}).get("Weight"))
        weighted_num = _as_float(value.get("WeightedNumerator"))
        weighted_den = _as_float(value.get("WeightedDenominator"))
        # The weight is recoverable from the values route on a weighted
        # gradebook even without the structure route.
        if weight is None and weighted_den is not None:
            weight = weighted_den

        feedback_html = (value.get("Comments") or {}).get("Html") or ""

        items.append(
            {
                "grade_object_id": object_id,
                "name": value.get("GradeObjectName")
                or (definition or {}).get("Name")
                or "",
                "points_earned": earned,
                "points_possible": possible,
                "weight": weight,
                "weighted_earned": weighted_num,
                "percentage": (
                    round(earned / possible * 100, 2)
                    if earned is not None and possible
                    else None
                ),
                "display": value.get("DisplayedGrade"),
                "is_graded": earned is not None or value.get("DisplayedGrade") not in (None, "", "-"),
                "feedback_text": html_to_text(feedback_html) or None,
            }
        )

    return {
        "org_unit_id": org_unit_id,
        "items": items,
        "count": len(items),
        "weights_available": weights_available,
        "caveats": [] if weights_available else [reason],
    }


async def analyze_grade_summary(
    client: D2LClient,
    *,
    org_unit_id: int,
    target_percentage: float | None = None,
) -> dict[str, Any]:
    """Current standing, and what's needed on remaining work to hit a target."""
    grades = await get_grades(client, org_unit_id=org_unit_id)
    items = grades["items"]
    caveats: list[str] = list(grades["caveats"])

    graded = [i for i in items if i["is_graded"] and i["percentage"] is not None]
    ungraded = [i for i in items if not i["is_graded"]]

    have_all_weights = bool(items) and all(i["weight"] is not None for i in items)

    graded_weight = sum(i["weight"] or 0.0 for i in graded)
    remaining_weight = sum(i["weight"] or 0.0 for i in ungraded)

    # Prefer the gradebook's own weighted arithmetic when it's present; fall
    # back to weighting percentages ourselves; fall back again to an unweighted
    # mean, clearly labelled as such.
    weighted_earned = sum(
        i["weighted_earned"] for i in graded if i["weighted_earned"] is not None
    )
    weighted_basis = sum(
        i["weight"] for i in graded if i["weighted_earned"] is not None and i["weight"]
    )

    if weighted_basis > 0:
        current_average = round(weighted_earned / weighted_basis * 100, 2)
        basis = "weighted"
    elif graded_weight > 0 and all(i["weight"] is not None for i in graded):
        current_average = round(
            sum((i["percentage"] or 0.0) * (i["weight"] or 0.0) for i in graded) / graded_weight, 2
        )
        basis = "weighted"
    elif graded:
        current_average = round(sum(i["percentage"] or 0.0 for i in graded) / len(graded), 2)
        basis = "unweighted mean of graded items"
        caveats.append(
            "Weights are unavailable, so this is an unweighted average of graded items "
            "and will not match your official course grade."
        )
    else:
        current_average = None
        basis = "nothing graded yet"

    summary: dict[str, Any] = {
        "org_unit_id": org_unit_id,
        "graded_items": len(graded),
        "ungraded_items": len(ungraded),
        "graded_weight": round(graded_weight, 2) if have_all_weights else None,
        "remaining_weight": round(remaining_weight, 2) if have_all_weights else None,
        "current_average": current_average,
        "average_basis": basis,
        "weights_available": have_all_weights,
    }

    if target_percentage is None:
        summary["caveats"] = caveats
        return summary

    # The projection is the one output that must never be guessed. It needs
    # every weight and a nonzero remaining weight; anything less and we say so
    # instead of producing a number.
    if not have_all_weights:
        caveats.append(
            "No projection: computing what you need on remaining work requires every "
            "item's weight, and some are unavailable. A projection from partial "
            "weights would be confidently wrong."
        )
    elif remaining_weight <= 0:
        caveats.append("No projection: there is no ungraded weight remaining.")
    elif current_average is None:
        caveats.append("No projection: nothing has been graded yet.")
    else:
        earned_points = current_average / 100 * graded_weight
        needed = (target_percentage - earned_points) / remaining_weight * 100
        summary["target"] = {
            "target_percentage": target_percentage,
            "required_average_on_remaining": round(needed, 2),
            "achievable": needed <= 100.0,
            "already_secured": needed <= 0.0,
        }

    summary["caveats"] = caveats
    return summary
