import React from 'react';
import {
  CheckCircleIcon,
  ExclamationCircleIcon,
  InProgressIcon,
  SyncAltIcon,
} from '@patternfly/react-icons';

export type StatVariant = 'ready' | 'failed' | 'pending' | 'reconciling' | 'neutral';

export interface StatTileProps {
  label: string;
  value: React.ReactNode;
  hint?: string;
  variant?: StatVariant;
  /** 0–100 share of total for the mini meter */
  share?: number;
  children?: never;
}

const VARIANT_ICON: Record<StatVariant, React.ComponentType<{ className?: string }>> = {
  ready: CheckCircleIcon,
  failed: ExclamationCircleIcon,
  pending: InProgressIcon,
  reconciling: SyncAltIcon,
  neutral: CheckCircleIcon,
};

export function StatTile({
  label,
  value,
  hint,
  variant = 'neutral',
  share,
}: StatTileProps): React.ReactElement {
  const Icon = VARIANT_ICON[variant];
  const meter = typeof share === 'number' ? Math.max(0, Math.min(100, share)) : undefined;

  return (
    <div className={`sc-stat-tile sc-stat-tile--${variant}`} data-variant={variant}>
      <div className="sc-stat-tile__head">
        <span className="sc-stat-tile__icon" aria-hidden>
          <Icon />
        </span>
        <span className="sc-stat-tile__label">{label}</span>
      </div>
      <div className="sc-stat-tile__value">{value}</div>
      {meter !== undefined && (
        <div className="sc-stat-tile__meter" aria-hidden>
          <span className="sc-stat-tile__meter-fill" style={{ width: `${meter}%` }} />
        </div>
      )}
      {hint && <div className="sc-stat-tile__hint">{hint}</div>}
    </div>
  );
}

export interface HealthStripTile extends StatTileProps {
  key?: string;
  href?: string;
  onClick?: () => void;
}

export interface HealthStripProps {
  tiles: HealthStripTile[];
}

export function HealthStrip({ tiles }: HealthStripProps): React.ReactElement {
  return (
    <div className="sc-health-strip" role="list" aria-label="Platform health">
      {tiles.map((t) => {
        const tile = (
          <StatTile
            label={t.label}
            value={t.value}
            hint={t.hint}
            variant={t.variant}
            share={t.share}
          />
        );
        return (
          <div key={t.key ?? t.label} role="listitem" className="sc-health-strip__item">
            {t.href ? (
              <a href={t.href} className="sc-card-link">
                {tile}
              </a>
            ) : t.onClick ? (
              <button type="button" className="sc-card-link sc-card-link--button" onClick={t.onClick}>
                {tile}
              </button>
            ) : (
              tile
            )}
          </div>
        );
      })}
    </div>
  );
}
