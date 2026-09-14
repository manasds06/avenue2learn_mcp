/**
 * Courses — the list, with whether each one is searchable.
 *
 * The indexed badge is the useful part: it is the difference between "the
 * outline does not mention late penalties" and "you never indexed this
 * course", which the chat surface has confused more than once.
 */

import { useState } from "preact/hooks";

import { Badge, Empty, Failure, SectionTitle, Skeleton } from "../components/ui.js";
import { useTool } from "../data/useTool.js";
import { splitCourseTitle } from "../format.js";

interface Course {
  org_unit_id: number;
  name: string;
  code: string;
  is_active: boolean;
  end_date: { local?: string | null } | null;
}

interface CoursesResult {
  courses: Course[];
  count: number;
}

interface StatusResult {
  index: {
    documents: number;
    chunks: number;
    courses_indexed: Array<{ org_unit_id: number; course_name: string; files: number }>;
  };
}

export function Courses({ onOpenCourse }: { onOpenCourse: (orgUnitId: number) => void }) {
  const [showPast, setShowPast] = useState(false);
  const courses = useTool<CoursesResult>("list_courses", { include_inactive: showPast });
  const status = useTool<StatusResult>("get_status");

  const indexed = new Map(
    (status.data?.index.courses_indexed ?? []).map((c) => [c.org_unit_id, c.files]),
  );

  return (
    <div class="view">
      <SectionTitle
        action={
          <button
            type="button"
            class="btn btn-sm btn-quiet"
            onClick={() => setShowPast(!showPast)}
          >
            {showPast ? "Active only" : "Include past"}
          </button>
        }
      >
        {courses.data ? `${courses.data.count} courses` : "Courses"}
      </SectionTitle>

      {courses.initial && courses.loading ? (
        <Skeleton rows={5} />
      ) : courses.failure ? (
        <Failure failure={courses.failure} onRetry={() => courses.reload()} />
      ) : courses.data && courses.data.courses.length ? (
        <div class="stack stack-tight">
          {courses.data.courses.map((course) => {
            const files = indexed.get(course.org_unit_id);
            const { code, title } = splitCourseTitle(course.name);
            return (
              <button
                key={course.org_unit_id}
                type="button"
                class={`coursecard${files ? " coursecard-indexed" : ""}`}
                onClick={() => onOpenCourse(course.org_unit_id)}
              >
                <div class="coursecard-top">
                  {code ? <span class="coursecard-code">{code}</span> : null}
                  {/* The distinction this list exists to make: "not indexed"
                      means file search has nothing to look through, which is a
                      completely different answer from "the materials do not
                      mention it". */}
                  {files ? (
                    <span
                      class="softpill"
                      title={`${files} files indexed and searchable`}
                    >
                      indexed · {files} files
                    </span>
                  ) : (
                    <span
                      class="coursecard-unindexed"
                      title="No files indexed, so file search will find nothing for this course."
                    >
                      not indexed
                    </span>
                  )}
                </div>
                <div class="coursecard-title">{title}</div>
                {!course.is_active ? (
                  <div class="coursecard-past">
                    <Badge tone="neutral">past</Badge>
                  </div>
                ) : null}
              </button>
            );
          })}
        </div>
      ) : (
        <Empty
          title="No courses found"
          detail="If you are signed in to Brightspace and this is empty, your enrolments may not be readable yet."
        />
      )}
    </div>
  );
}
