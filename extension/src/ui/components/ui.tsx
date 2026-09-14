/**
 * The small shared pieces. Deliberately plain: styling lives in CSS variables
 * so a mockup can be matched by changing tokens, not components.
 */

import type { ComponentChildren } from "preact";

export function Card({
  children,
  onClick,
  accent,
}: {
  children: ComponentChildren;
  onClick?: () => void;
  accent?: boolean;
}) {
  const cls = `card${accent ? " card-accent" : ""}${onClick ? " card-click" : ""}`;
  return onClick ? (
    <button type="button" class={cls} onClick={onClick}>
      {children}
    </button>
  ) : (
    <div class={cls}>{children}</div>
  );
}

export type Tone = "neutral" | "ok" | "warn" | "danger" | "unknown" | "accent";

export function Badge({
  children,
  tone = "neutral",
  title,
}: {
  children: ComponentChildren;
  tone?: Tone;
  title?: string;
}) {
  return (
    <span class={`chip chip-${tone}`} title={title}>
      {children}
    </span>
  );
}

export function SectionTitle({
  children,
  action,
}: {
  children: ComponentChildren;
  action?: ComponentChildren;
}) {
  return (
    <div class="section-title">
      <h2>{children}</h2>
      {action}
    </div>
  );
}

/**
 * An empty state should say what would fill it, not just that it is empty.
 * "No deadlines" and "no deadlines because nothing is indexed" send a user in
 * completely different directions.
 */
export function Empty({
  title,
  detail,
  action,
}: {
  title: string;
  detail?: string;
  action?: ComponentChildren;
}) {
  return (
    <div class="empty">
      <p class="empty-title">{title}</p>
      {detail ? <p class="empty-detail">{detail}</p> : null}
      {action}
    </div>
  );
}

export function Skeleton({ rows = 3 }: { rows?: number }) {
  return (
    <div class="skeleton" aria-hidden="true">
      {Array.from({ length: rows }, (_, i) => (
        <div key={i} class="skeleton-row" />
      ))}
    </div>
  );
}

/**
 * Renders a typed tool failure.
 *
 * `next_step` is the whole point — every error in this codebase carries one,
 * and dropping it leaves the user with "something went wrong".
 */
export function Failure({
  failure,
  onRetry,
}: {
  failure: { error: string; message: string; next_step?: string };
  onRetry?: () => void;
}) {
  return (
    <div class="failure" role="alert">
      <p class="failure-message">{failure.message}</p>
      {failure.next_step ? <p class="failure-next">{failure.next_step}</p> : null}
      {onRetry ? (
        <button type="button" class="btn btn-sm btn-quiet" onClick={onRetry}>
          Try again
        </button>
      ) : null}
    </div>
  );
}

export function Tabs<T extends string>({
  tabs,
  active,
  onSelect,
}: {
  tabs: Array<{ id: T; label: string }>;
  active: T;
  onSelect: (id: T) => void;
}) {
  return (
    <div class="tabs" role="tablist">
      {tabs.map((t) => (
        <button
          key={t.id}
          type="button"
          role="tab"
          aria-selected={t.id === active}
          class={`tab${t.id === active ? " tab-active" : ""}`}
          onClick={() => onSelect(t.id)}
        >
          {t.label}
        </button>
      ))}
    </div>
  );
}

export function Stat({ label, value, tone }: { label: string; value: string; tone?: Tone }) {
  return (
    <div class="stat">
      <div class={`stat-value${tone ? ` stat-${tone}` : ""}`}>{value}</div>
      <div class="stat-label">{label}</div>
    </div>
  );
}

/** A horizontal weight bar — grades read far faster than a list of numbers. */
export function WeightBar({
  segments,
}: {
  segments: Array<{ label: string; weight: number; tone: Tone }>;
}) {
  const total = segments.reduce((n, s) => n + s.weight, 0) || 1;
  return (
    <div class="weightbar" role="img" aria-label="Grade weight breakdown">
      {segments.map((s, i) => (
        <div
          key={i}
          class={`weightseg weightseg-${s.tone}`}
          style={{ width: `${(s.weight / total) * 100}%` }}
          title={`${s.label}: ${s.weight}%`}
        />
      ))}
    </div>
  );
}
