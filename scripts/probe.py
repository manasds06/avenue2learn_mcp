#!/usr/bin/env python3
"""Phase 0 probe: find out what a student account can actually reach.

Four routes are documented as instructor-scope, and two features hinge on them
(assignments and grade projection). Building the tool layer against guesses is
how you ship a tool description that lies to the model.

Run AFTER `avenue-mcp login`:

    python scripts/probe.py --course ORG_UNIT_ID
    python scripts/probe.py --course ORG_UNIT_ID --save-fixtures

Writes a redacted summary to stdout and (optionally) raw responses to
tests/fixtures/ -- which is gitignored, because raw responses contain real IDs
and names.

Findings go into docs/08-api-probe-results.md. Direction matters: probe -> docs,
never docs -> probe.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from avenue_mcp.config import get_settings  # noqa: E402
from avenue_mcp.context import AppContext  # noqa: E402
from avenue_mcp.errors import AvenueMCPError, PermissionDeniedError  # noqa: E402

FIXTURES = Path(__file__).resolve().parents[1] / "tests" / "fixtures"


class Probe:
    def __init__(self, ctx: AppContext, save: bool) -> None:
        self.ctx = ctx
        self.save = save
        self.results: list[dict[str, Any]] = []

    async def run(
        self,
        label: str,
        component: str,
        suffix: str,
        *,
        paged: bool = False,
        params: dict[str, Any] | None = None,
    ) -> Any:
        entry: dict[str, Any] = {"label": label, "route": f"{component}/{suffix}"}
        try:
            if paged:
                data = await self.ctx.client.get_paged(component, suffix, params=params, cache=False)  # type: ignore[arg-type]
            else:
                data = await self.ctx.client.get(component, suffix, params=params, cache=False)  # type: ignore[arg-type]
            entry["status"] = "OK"
            entry["shape"] = describe_shape(data)
            entry["count"] = len(data) if isinstance(data, list) else None
            self._save(label, data)
            print(f"  [OK]      {label}: {entry['shape']}")
            return data
        except PermissionDeniedError as exc:
            entry["status"] = "BLOCKED (403)"
            entry["detail"] = str(exc)[:200]
            print(f"  [BLOCKED] {label}: 403 -- instructor-only on this instance")
        except AvenueMCPError as exc:
            entry["status"] = type(exc).__name__
            entry["detail"] = str(exc)[:200]
            print(f"  [FAIL]    {label}: {type(exc).__name__}: {str(exc)[:120]}")
        except Exception as exc:  # noqa: BLE001
            entry["status"] = f"ERROR {type(exc).__name__}"
            entry["detail"] = str(exc)[:200]
            print(f"  [ERROR]   {label}: {type(exc).__name__}: {str(exc)[:120]}")
        finally:
            self.results.append(entry)
        return None

    def _save(self, label: str, data: Any) -> None:
        if not self.save:
            return
        FIXTURES.mkdir(parents=True, exist_ok=True)
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in label)
        (FIXTURES / f"{safe}.json").write_text(
            json.dumps(data, indent=2, default=str), encoding="utf-8"
        )


def describe_shape(data: Any, depth: int = 0) -> str:
    """Field names and types only -- never values. This output is meant to be
    safe to paste into a committed document."""
    if depth > 2:
        return "..."
    if isinstance(data, list):
        if not data:
            return "[] (empty)"
        return f"[{len(data)} x {describe_shape(data[0], depth + 1)}]"
    if isinstance(data, dict):
        keys = sorted(data.keys())[:14]
        return "{" + ", ".join(keys) + ("..." if len(data) > 14 else "") + "}"
    return type(data).__name__


async def main() -> int:
    ap = argparse.ArgumentParser(description="Phase 0 API probe")
    ap.add_argument("--course", type=int, help="org_unit_id to probe (default: first active)")
    ap.add_argument("--save-fixtures", action="store_true", help="Write raw responses")
    ap.add_argument("--out", type=Path, help="Write the redacted summary as JSON")
    args = ap.parse_args()

    settings = get_settings()
    ctx = AppContext(settings)
    probe = Probe(ctx, args.save_fixtures)

    print(f"\nProbing {settings.base_url}")
    print("=" * 72)

    # --- auth -------------------------------------------------------------
    print("\nB. AUTHENTICATION")
    if not ctx.auth.session_present:
        print("  No session. Run `avenue-mcp login` first.", file=sys.stderr)
        return 1

    state = ctx.auth.load_state()
    cookies = [c.get("name") for c in state.get("cookies", [])]
    print(f"  cookies present: {len(cookies)}")
    print(f"  names: {sorted(set(cookies))}")
    print(f"  has d2lSessionVal: {'d2lSessionVal' in cookies}")
    print(f"  has d2lSecureSessionVal: {'d2lSecureSessionVal' in cookies}")

    alive = await ctx.auth.is_alive()
    print(f"  liveness probe: {'OK' if alive else 'FAILED'}")
    if not alive:
        print("  Session appears dead. Re-run `avenue-mcp login`.", file=sys.stderr)
        return 1
    token = await ctx.auth.xsrf_token()
    print(f"  xsrf token obtained: {bool(token)}")
    print(f"  session age: {ctx.auth.session_age_minutes} min")

    # --- bootstrap --------------------------------------------------------
    print("\nA. BOOTSTRAP")
    versions = await ctx.client.versions()
    print(f"  versions: lp={versions.get('lp')} le={versions.get('le')}")
    await probe.run("whoami", "lp", "users/whoami")

    # --- courses ----------------------------------------------------------
    print("\nC1-C2. COURSES")
    enrollments = await probe.run(
        "myenrollments", "lp", "enrollments/myenrollments/", paged=True
    )

    org_unit_id = args.course
    if org_unit_id is None and isinstance(enrollments, list):
        from avenue_mcp.client import models as m

        for entry in enrollments:
            ou = m.enrollment_org_unit(entry)
            if isinstance(ou, dict) and m.is_course_offering(ou):
                candidate = m.as_int(m.pick(ou, "Id", "OrgUnitId"))
                if candidate:
                    org_unit_id = candidate
                    break

    if org_unit_id is None:
        print("\nNo course to probe. Pass --course ORG_UNIT_ID.", file=sys.stderr)
        return 1
    print(f"\n  probing course org_unit_id={org_unit_id} (redacted in output)")
    await probe.run("course_details", "lp", f"courses/{org_unit_id}")

    # --- content ----------------------------------------------------------
    print("\nC3-C6. CONTENT")
    await probe.run("content_root", "le", f"{org_unit_id}/content/root/")
    try:
        topics = await ctx.syncer.discover_files(org_unit_id)
        print(f"  [OK]      discover_files: {len(topics)} downloadable topic(s)")
        probe.results.append(
            {"label": "discover_files", "status": "OK", "count": len(topics)}
        )
        if topics:
            t = topics[0]
            await probe.run(
                "topic_metadata", "le", f"{org_unit_id}/content/topics/{t['topic_id']}"
            )
            print(f"  note: LastModifiedDate present = {bool(t.get('last_modified'))}")
    except Exception as exc:  # noqa: BLE001
        print(f"  [FAIL]    discover_files: {exc}")

    # --- assignments: THE critical one ------------------------------------
    print("\nC7-C10. ASSIGNMENTS  <-- gates the whole assignments feature")
    folders = await probe.run(
        "dropbox_folders", "le", f"{org_unit_id}/dropbox/folders/", paged=True
    )
    if isinstance(folders, list) and folders:
        from avenue_mcp.client import models as m

        fid = m.as_int(m.pick(folders[0], "Id", "FolderId"))
        if fid:
            await probe.run(
                "dropbox_folder_one", "le", f"{org_unit_id}/dropbox/folders/{fid}"
            )
            await probe.run(
                "mysubmissions",
                "le",
                f"{org_unit_id}/dropbox/folders/{fid}/submissions/mysubmissions/",
            )

    # --- grades -----------------------------------------------------------
    print("\nC11-C12. GRADES  <-- C12 gates grade projection")
    await probe.run("my_grade_values", "le", f"{org_unit_id}/grades/values/myGradeValues/")
    objects = await probe.run("grade_objects", "le", f"{org_unit_id}/grades/", paged=True)
    if isinstance(objects, list) and objects:
        from avenue_mcp.client import models as m

        has_weights = any(m.as_float(m.pick(o, "Weight")) for o in objects if isinstance(o, dict))
        print(f"  note: weights present = {has_weights}")

    # --- news / calendar --------------------------------------------------
    print("\nC13-C14. ANNOUNCEMENTS + CALENDAR")
    await probe.run("news", "le", f"{org_unit_id}/news/", paged=True)
    # startDateTime/endDateTime are REQUIRED here; without them Valence returns
    # 400 and the probe would record a malformed request as a blocked route.
    from avenue_mcp.util.dates import now_utc, to_utc_param

    _now = now_utc()
    events = await probe.run(
        "calendar_myevents",
        "le",
        f"{org_unit_id}/calendar/events/myEvents/",
        paged=True,
        params={
            "startDateTime": to_utc_param(_now - timedelta(days=30)),
            "endDateTime": to_utc_param(_now + timedelta(days=120)),
        },
    )
    if isinstance(events, list):
        from avenue_mcp.tools.assignments import _classify
        from avenue_mcp.client import models as m

        kinds: dict[str, int] = {}
        for ev in events:
            if isinstance(ev, dict):
                k = _classify(ev, str(m.pick(ev, "Title", default="") or ""))
                kinds[k] = kinds.get(k, 0) + 1
        print(f"  note: event kinds = {kinds}")
        print(
            "  ** assignment due dates in calendar: "
            f"{kinds.get('assignment', 0) > 0} (fallback viability)"
        )
        print(f"  ** quiz due dates in calendar: {kinds.get('quiz', 0) > 0}")

    # --- quizzes ----------------------------------------------------------
    print("\nC14b-C14c. QUIZZES")
    quizzes = await probe.run("quizzes", "le", f"{org_unit_id}/quizzes/", paged=True)
    if isinstance(quizzes, list) and quizzes:
        from avenue_mcp.client import models as m

        qid = m.as_int(m.pick(quizzes[0], "QuizId", "Id"))
        if qid:
            await probe.run(
                "quiz_attempts", "le", f"{org_unit_id}/quizzes/{qid}/attempts/", paged=True
            )

    # --- discussions ------------------------------------------------------
    print("\nC14d-C14f. DISCUSSIONS")
    forums = await probe.run(
        "discussion_forums", "le", f"{org_unit_id}/discussions/forums/", paged=True
    )
    if isinstance(forums, list) and forums:
        from avenue_mcp.client import models as m

        forum_id = m.as_int(m.pick(forums[0], "ForumId", "Id"))
        if forum_id:
            topics_r = await probe.run(
                "discussion_topics",
                "le",
                f"{org_unit_id}/discussions/forums/{forum_id}/topics/",
                paged=True,
            )
            if isinstance(topics_r, list) and topics_r:
                tid = m.as_int(m.pick(topics_r[0], "TopicId", "Id"))
                if tid:
                    posts = await probe.run(
                        "discussion_posts",
                        "le",
                        f"{org_unit_id}/discussions/forums/{forum_id}/topics/{tid}/posts/",
                        paged=True,
                    )
                    if isinstance(posts, list) and posts:
                        first = posts[0]
                        has_parent = "ParentPostId" in first or "ParentId" in first
                        role = m.pick(first, "AuthorRole", "Role", "RoleName")
                        print(f"  ** ParentPostId present: {has_parent} (question+reply chunking)")
                        print(f"  ** author role derivable: {role is not None}")
                        if role is None:
                            print(
                                "     WARNING: only names may be available. The index "
                                "stores roles and NOT names -- see docs/07."
                            )

    # --- classlist --------------------------------------------------------
    print("\nC15-C16. CLASS LIST  (403 expected and correct)")
    # `le`, not `lp` -- the wrong component 404s, and a 404 recorded here would
    # be transcribed into docs/08 as "roster blocked", which is a permission
    # conclusion drawn from a typo.
    await probe.run("classlist", "le", f"{org_unit_id}/classlist/", paged=True)
    await probe.run(
        "orgunit_users", "lp", f"enrollments/orgUnits/{org_unit_id}/users/", paged=True
    )

    # --- summary ----------------------------------------------------------
    print("\n" + "=" * 72)
    print("SUMMARY\n")
    ok = [r for r in probe.results if r["status"] == "OK"]
    blocked = [r for r in probe.results if "BLOCKED" in str(r["status"])]
    other = [r for r in probe.results if r not in ok and r not in blocked]

    print(f"  Working: {len(ok)}")
    for r in ok:
        print(f"    OK       {r['label']}")
    print(f"\n  Blocked: {len(blocked)}")
    for r in blocked:
        print(f"    BLOCKED  {r['label']}")
    if other:
        print(f"\n  Other: {len(other)}")
        for r in other:
            print(f"    {r['status']}  {r['label']}")

    print("\nFEATURE VIABILITY")
    status = {r["label"]: r["status"] for r in probe.results}
    checks = [
        ("Assignments (full)", "dropbox_folders"),
        ("Grade projection", "grade_objects"),
        ("Quiz status", "quizzes"),
        ("Discussions corpus", "discussion_posts"),
        ("Class roster", "classlist"),
    ]
    for feature, label in checks:
        state = status.get(label, "not probed")
        mark = "yes" if state == "OK" else "NO -- use the documented fallback"
        print(f"  {feature:24s} {mark}  ({state})")

    print("\nNext: transcribe these findings into docs/08-api-probe-results.md,")
    print("then update the status column in docs/02-api-surface.md FROM them.")

    if args.out:
        args.out.write_text(
            json.dumps({"versions": versions, "results": probe.results}, indent=2),
            encoding="utf-8",
        )
        print(f"\nRedacted summary written to {args.out}")

    await ctx.aclose()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
