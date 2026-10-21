import React from 'react';
import { Card, CardBody, CardTitle } from '@patternfly/react-core';
import { LockIcon } from '@patternfly/react-icons';
import { KindIcon } from '../icons';
import { useTranslation } from '../i18n';
import { HybridSovereignKind } from '../types';

export interface InventoryCardProps {
  title: string;
  count: number | string;
  hint?: string;
  /** Legacy free-form icon node; prefer `kind` for accent tiles */
  icon?: React.ReactNode;
  kind?: HybridSovereignKind | string;
  href?: string;
  status?: 'success' | 'danger' | 'warning' | 'default';
  /** The user may not list this kind: show a lock and "No access", never a count (UX-057). */
  locked?: boolean;
}

/** OpenShift Overview–style inventory tile with kind accent iconography */
export function InventoryCard({
  title,
  count,
  hint,
  icon,
  kind,
  href,
  status = 'default',
  locked = false,
}: InventoryCardProps): React.ReactElement {
  const { t } = useTranslation();
  const iconNode = kind ? <KindIcon kind={kind} size="md" /> : icon;

  const inner = (
    <Card isCompact isFullHeight className={`sc-inventory-card sc-inventory-card--${locked ? 'locked' : status}`}>
      <CardTitle>
        <span className="sc-inventory-card__icon" aria-hidden>
          {iconNode}
        </span>
        <span className="sc-inventory-card__title-text">{title}</span>
      </CardTitle>
      <CardBody>
        {locked ? (
          <div className="sc-inventory-card__count sc-inventory-card__count--locked" title={t('access.noAccessHint')}>
            <LockIcon /> <span className="sc-inventory-card__locked-text">{t('access.noAccess')}</span>
          </div>
        ) : (
          <div className="sc-inventory-card__count">{count}</div>
        )}
        {hint && !locked && <div className="sc-inventory-card__hint">{hint}</div>}
      </CardBody>
    </Card>
  );
  if (href && !locked) {
    return (
      <a href={href} className="sc-card-link">
        {inner}
      </a>
    );
  }
  return inner;
}
