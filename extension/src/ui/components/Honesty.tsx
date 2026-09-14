/**
 * The components that make this app's honesty visible.
 *
 * Every rule below is already enforced in the tool layer — these render it.
 * That distinction matters: in prose, "submission status is unavailable on
 * this instance" is a caveat people skim past on the way to the answer. As a
 * greyed chip next to the assignment, it is unmissable and unmistakable.
 *
 * The three states this app cares about are NOT "success" and "error". They
 * are: we know, we know we cannot know, and we could not reach it. Collapsing
 * the middle one into either of the others is the failure this whole project
 * keeps circling — reporting silence as a definite answer.
 */

import type { ComponentChildren } from "preact";

/**
 * "We cannot check this."
 *
 * Used for `submission_status: "unknown"` and quiz status `"unknown"`, where
 * the route is denied so neither "done" nor "not done" is true. Neutral
 * colouring on purpose: this is a gap, not a failure, and red would read as
 * something being broken.
 */
export function Unknown({ what, why }: { what: string; why?: string }) {
  return (
    <span
      class="chip chip-unknown"
      title={
        why ??
        `${what} cannot be checked — your school's Brightspace does not allow student accounts to read it.`
      }
    >
      <span aria-hidden="true">?</span> {what} unknown
    </span>
  );
}

/**
 * A refusal, with its reason.
 *
 * `analyze_grade_summary` withholds a projection when weights do not
 * reconcile; `get_whats_new` reports `complete: false` when a source failed.
 * Both are correct behaviour, and both are worth MORE space than the answer
 * they replace, not less — a confidently wrong "you need 74% on the final" is
 * the single most damaging thing this could output.
 */
export function Caveat({
  title,
  children,
}: {
  title?: string;
  children: ComponentChildren;
}) {
  return (
    <div class="caveat" role="note">
      {title ? <strong class="caveat-title">{title}</strong> : null}
      <div class="caveat-body">{children}</div>
    </div>
  );
}

/**
 * "Nothing has been indexed for this course."
 *
 * The distinction this exists for: an empty search result means either "the
 * materials do not say" or "you never synced this course", and those are
 * completely different answers. `search_course_materials` returns
 * `indexed_courses` on every call so the two stay separable — this renders it.
 */
export function NotIndexed({ courseName, onSync }: { courseName?: string; onSync?: () => void }) {
  return (
    <div class="notindexed">
      <p>
        {courseName ? <b>{courseName}</b> : "This course"} has no indexed files, so file
        search has nothing to look through. That is different from the materials not
        mentioning something.
      </p>
      {onSync ? (
        <button type="button" class="btn btn-sm" onClick={onSync}>
          Index this course
        </button>
      ) : null}
    </div>
  );
}

/** A denial that is expected, phrased with the weight the profile justifies. */
export function Restricted({ what, detail }: { what: string; detail?: string }) {
  return (
    <div class="caveat caveat-quiet" role="note">
      <strong class="caveat-title">{what} is not available</strong>
      <div class="caveat-body">
        {detail ??
          "Your school does not allow student accounts to read this. Signing in again will not change it."}
      </div>
    </div>
  );
}
