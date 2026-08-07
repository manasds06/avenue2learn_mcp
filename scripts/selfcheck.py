"""Offline checks over the pure logic — no network, no session.

Covers the behaviours the roadmap calls out as the ones where being wrong does
real damage: Eastern-time rendering, and grade projections never being
fabricated from incomplete weights.
"""

from __future__ import annotations

import asyncio
import sys
from typing import Any

from avenue_mcp.tools import grades
from avenue_mcp.util.dates import describe, parse_d2l_date

FAILURES: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    print(f"  {'PASS' if condition else 'FAIL'}  {label}")
    if not condition:
        FAILURES.append(f"{label}{f' — {detail}' if detail else ''}")


class FakeClient:
    """Stands in for D2LClient. Proves tools need no network to be tested."""

    def __init__(self, responses: dict[str, Any]) -> None:
        self.responses = responses

        class _S:
            timezone = "America/Toronto"
            base_url = "https://avenue.mcmaster.ca"

        self.settings = _S()

    async def get_json(self, component: str, route: str, *, params: Any = None) -> Any:
        if route not in self.responses:
            raise KeyError(route)
        value = self.responses[route]
        if isinstance(value, Exception):
            raise value
        return value


def test_timezone() -> None:
    print("\nEastern-time rendering (docs/06 exit criterion)")
    # 11:59 PM Mar 15 EDT is 03:59Z on Mar 16. Reporting "Mar 16" is the bug.
    edt = describe(parse_d2l_date("2026-03-16T03:59:00.000Z"))
    check("EDT deadline keeps its local date", edt["local_date"] == "2026-03-15", str(edt))
    # And in winter the offset is -5, not -4.
    est = describe(parse_d2l_date("2026-02-01T04:59:00.000Z"))
    check("EST deadline keeps its local date", est["local_date"] == "2026-01-31", str(est))
    check("UTC instant is preserved", edt["utc"] == "2026-03-16T03:59:00.000Z")


async def test_grades_with_weights() -> None:
    print("\nGrade projection WITH weights")
    client = FakeClient(
        {
            "1/grades/values/myGradeValues/": [
                {
                    "GradeObjectIdentifier": "10",
                    "GradeObjectName": "Midterm",
                    "PointsNumerator": 80,
                    "PointsDenominator": 100,
                },
            ],
            "1/grades/": [
                {"Id": 10, "Name": "Midterm", "MaxPoints": 100, "Weight": 40.0},
                {"Id": 11, "Name": "Final", "MaxPoints": 100, "Weight": 60.0},
            ],
        }
    )
    # The ungraded Final has no value row, so it must come from the structure.
    client.responses["1/grades/values/myGradeValues/"].append(
        {"GradeObjectIdentifier": "11", "GradeObjectName": "Final", "DisplayedGrade": "-"}
    )

    summary = await grades.analyze_grade_summary(client, org_unit_id=1, target_percentage=85.0)
    check("weights reported available", summary["weights_available"] is True)
    check("current average is weighted 80%", summary["current_average"] == 80.0, str(summary))
    check("a projection is produced", "target" in summary, str(summary))
    if "target" in summary:
        # 40*0.8 = 32 secured; needs 53 more of 60 -> 88.33%
        needed = summary["target"]["required_average_on_remaining"]
        check("projection arithmetic is right (88.33)", abs(needed - 88.33) < 0.1, str(needed))


async def test_grades_without_weights() -> None:
    print("\nGrade projection WITHOUT weights (must refuse, not guess)")
    from avenue_mcp.errors import PermissionDeniedError

    client = FakeClient(
        {
            "2/grades/values/myGradeValues/": [
                {
                    "GradeObjectIdentifier": "10",
                    "GradeObjectName": "Midterm",
                    "PointsNumerator": 80,
                    "PointsDenominator": 100,
                }
            ],
            "2/grades/": PermissionDeniedError(),
        }
    )
    summary = await grades.analyze_grade_summary(client, org_unit_id=2, target_percentage=85.0)
    check("weights reported unavailable", summary["weights_available"] is False)
    check("NO projection is fabricated", "target" not in summary, str(summary.get("target")))
    check("a caveat explains why", bool(summary["caveats"]), str(summary))
    check(
        "average is labelled as unweighted",
        "unweighted" in summary["average_basis"],
        summary["average_basis"],
    )


async def test_assignments_degraded() -> None:
    print("\nlist_assignments when dropbox folders are blocked")
    from avenue_mcp.errors import PermissionDeniedError
    from avenue_mcp.tools import assignments

    client = FakeClient({"3/dropbox/folders/": PermissionDeniedError()})
    result = await assignments.list_assignments(client, org_unit_id=3)
    check("flagged unavailable, not 'zero assignments'", result["assignments_available"] is False)
    check("note tells the model what to do instead", "get_upcoming_deadlines" in (result["note"] or ""))


async def main() -> int:
    test_timezone()
    await test_grades_with_weights()
    await test_grades_without_weights()
    await test_assignments_degraded()

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILURE(S):")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("All offline checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
