/**
 * `useTool` — call a tool from a component, with loading and error states.
 *
 * Deliberately returns the typed FAILURE rather than throwing. Every tool error
 * in this project carries a next step, and a view that renders "something went
 * wrong" throws away the one useful part. `error.next_step` is what the user
 * actually needs.
 */

import { useCallback, useEffect, useRef, useState } from "preact/hooks";

import { cachedTool, type ToolResult } from "./tools.js";

export interface ToolFailure {
  error: string;
  message: string;
  next_step?: string;
}

export interface ToolState<T> {
  data: T | null;
  failure: ToolFailure | null;
  loading: boolean;
  /** True only on the FIRST load, so a refresh does not flash a skeleton. */
  initial: boolean;
  reload: (opts?: { force?: boolean }) => void;
}

export function useTool<T>(
  name: string,
  args: Record<string, unknown> = {},
  opts: { enabled?: boolean } = {},
): ToolState<T> {
  const enabled = opts.enabled ?? true;
  const [data, setData] = useState<T | null>(null);
  const [failure, setFailure] = useState<ToolFailure | null>(null);
  const [loading, setLoading] = useState(enabled);
  const [initial, setInitial] = useState(true);

  // Args are usually an inline object literal, so a plain dependency would
  // refetch on every render. Compare by value.
  const argsKey = JSON.stringify(args, Object.keys(args).sort());
  // Guards against a slow response for course A landing after the user has
  // already switched to course B.
  const generation = useRef(0);

  const run = useCallback(
    async (force = false) => {
      if (!enabled) return;
      const mine = ++generation.current;
      setLoading(true);

      const outcome: ToolResult = await cachedTool(name, JSON.parse(argsKey), { force });
      if (mine !== generation.current) return;

      if (outcome.ok) {
        setData(outcome.result as T);
        setFailure(null);
      } else {
        setFailure({
          error: outcome.error,
          message: outcome.message,
          next_step: outcome.next_step,
        });
      }
      setLoading(false);
      setInitial(false);
    },
    [name, argsKey, enabled],
  );

  useEffect(() => {
    void run();
    return () => {
      // Anything in flight belongs to a previous view.
      generation.current++;
    };
  }, [run]);

  const reload = useCallback(
    (o: { force?: boolean } = {}) => void run(o.force ?? true),
    [run],
  );

  return { data, failure, loading, initial, reload };
}
