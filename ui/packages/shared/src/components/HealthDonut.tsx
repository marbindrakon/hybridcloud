import React, { useMemo } from 'react';

export interface HealthDonutProps {
  ready: number;
  failed: number;
  pending: number;
  reconciling?: number;
  title?: string;
  /** Compact size for list headers */
  size?: 'md' | 'sm';
}

/** Lightweight SVG donut — OpenShift Overview style without chart deps */
export function HealthDonut({
  ready,
  failed,
  pending,
  reconciling = 0,
  title = 'Health',
  size = 'md',
}: HealthDonutProps): React.ReactElement {
  const other = pending + reconciling;
  const total = ready + failed + other;
  const pct = total === 0 ? 0 : Math.round((ready / total) * 100);
  const dim = size === 'sm' ? 112 : 148;

  const segments = useMemo(() => {
    const r = 42;
    const c = 2 * Math.PI * r;
    const parts = [
      { key: 'ready', value: ready, color: 'var(--sc-success, #3e8635)' },
      { key: 'failed', value: failed, color: 'var(--sc-danger, #c9190b)' },
      { key: 'pending', value: other, color: 'var(--sc-warning, #f0ab00)' },
    ];
    let offset = 0;
    return parts.map((p) => {
      const len = total === 0 ? 0 : (p.value / total) * c;
      const seg = { ...p, dash: `${len} ${c - len}`, offset: -offset };
      offset += len;
      return seg;
    });
  }, [ready, failed, other, total]);

  return (
    <div
      className={`sc-health-donut sc-health-donut--${size}`}
      aria-label={`${title} ${pct}% healthy`}
    >
      <div className="sc-health-donut__chart">
        <svg viewBox="0 0 120 120" width={dim} height={dim} role="img">
          <circle
            cx="60"
            cy="60"
            r="42"
            fill="none"
            stroke="var(--sc-border, #c7c7c7)"
            strokeWidth="12"
            opacity="0.45"
          />
          {segments.map((s) =>
            s.value > 0 ? (
              <circle
                key={s.key}
                cx="60"
                cy="60"
                r="42"
                fill="none"
                stroke={s.color}
                strokeWidth="12"
                strokeLinecap="round"
                strokeDasharray={s.dash}
                strokeDashoffset={s.offset}
                transform="rotate(-90 60 60)"
              />
            ) : null,
          )}
          <text x="60" y="54" textAnchor="middle" className="sc-health-donut__total">
            {total}
          </text>
          <text x="60" y="70" textAnchor="middle" className="sc-health-donut__sub">
            Total
          </text>
          <text x="60" y="86" textAnchor="middle" className="sc-health-donut__pct">
            {total === 0 ? '—' : `${pct}% ok`}
          </text>
        </svg>
      </div>
      <ul className="sc-health-donut__legend">
        <li>
          <span className="sc-dot sc-dot--ready" /> Ready <strong>{ready}</strong>
        </li>
        <li>
          <span className="sc-dot sc-dot--failed" /> Failed <strong>{failed}</strong>
        </li>
        <li>
          <span className="sc-dot sc-dot--pending" /> Pending <strong>{other}</strong>
        </li>
      </ul>
    </div>
  );
}
