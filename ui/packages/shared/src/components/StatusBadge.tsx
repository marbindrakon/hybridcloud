import React from 'react';
import {
  Label,
  Popover,
  DescriptionList,
  DescriptionListGroup,
  DescriptionListTerm,
  DescriptionListDescription,
} from '@patternfly/react-core';
import {
  CheckCircleIcon,
  InProgressIcon,
  ExclamationCircleIcon,
  SyncAltIcon,
  OutlinedQuestionCircleIcon,
} from '@patternfly/react-icons';
import { useTranslation } from '../i18n';

export type ResourceHealth = 'ready' | 'pending' | 'failed' | 'reconciling' | 'unknown';

const COLOR: Record<ResourceHealth, 'green' | 'orange' | 'red' | 'blue' | 'grey'> = {
  ready: 'green',
  pending: 'orange',
  failed: 'red',
  reconciling: 'blue',
  unknown: 'grey',
};

const HEALTH_ICON: Record<ResourceHealth, React.ComponentType> = {
  ready: CheckCircleIcon,
  pending: InProgressIcon,
  failed: ExclamationCircleIcon,
  reconciling: SyncAltIcon,
  unknown: OutlinedQuestionCircleIcon,
};

export interface StatusBadgeProps {
  status?: string | null;
  ready?: boolean;
  message?: string;
  lastTransition?: string;
}

export function normalizeHealth(ready?: boolean, status?: string | null): ResourceHealth {
  const s = (status ?? '').toLowerCase();
  if (ready === true || s === 'ready' || s === 'success') return 'ready';
  if (s === 'failed' || s === 'error') return 'failed';
  if (s === 'reconciling' || s === 'running') return 'reconciling';
  if (s === 'pending' || s === '') return ready === false ? 'pending' : 'pending';
  return 'unknown';
}

/** Compact OCP-style status label with health icon + optional conditions popover */
export function StatusBadge({
  status,
  ready,
  message,
  lastTransition,
}: StatusBadgeProps): React.ReactElement {
  const { t } = useTranslation();
  const health = normalizeHealth(ready, status);
  const Icon = HEALTH_ICON[health];
  const label = (
    <Label color={COLOR[health]} icon={<Icon />} className={`sc-status-badge sc-status-badge--${health}`}>
      {t(`status.${health}`)}
    </Label>
  );

  if (!message && !lastTransition) {
    return label;
  }

  return (
    <Popover
      headerContent={t('common.status')}
      bodyContent={
        <DescriptionList isCompact isHorizontal>
          {message && (
            <DescriptionListGroup>
              <DescriptionListTerm>{t('common.message')}</DescriptionListTerm>
              <DescriptionListDescription>{message}</DescriptionListDescription>
            </DescriptionListGroup>
          )}
          {lastTransition && (
            <DescriptionListGroup>
              <DescriptionListTerm>{t('common.lastTransition')}</DescriptionListTerm>
              <DescriptionListDescription>{lastTransition}</DescriptionListDescription>
            </DescriptionListGroup>
          )}
        </DescriptionList>
      }
    >
      <span style={{ cursor: 'pointer' }}>{label}</span>
    </Popover>
  );
}
