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
  ExclamationTriangleIcon,
  SyncAltIcon,
  OutlinedClockIcon,
  TrashIcon,
} from '@patternfly/react-icons';
import { useTranslation } from '../i18n';
import {
  derivePhase,
  healthOfPhase,
  phaseOf,
  phaseSinceOf,
  reasonOf,
  summaryOf,
  type HealthBucket,
  type Phase,
  type PhaseSubject,
} from '../status/phase';
import { formatRelativeTime } from '../list/listColumns';

export type ResourceHealth = 'ready' | 'pending' | 'failed' | 'reconciling' | 'unknown';

type LabelColor = 'green' | 'orange' | 'red' | 'blue' | 'grey' | 'purple' | 'yellow';

const PHASE_COLOR: Record<Phase, LabelColor> = {
  Waiting: 'orange',
  Creating: 'blue',
  Updating: 'blue',
  Ready: 'green',
  Degraded: 'yellow',
  Failed: 'red',
  Deleting: 'purple',
};

const PHASE_ICON: Record<Phase, React.ComponentType> = {
  Waiting: OutlinedClockIcon,
  Creating: InProgressIcon,
  Updating: SyncAltIcon,
  Ready: CheckCircleIcon,
  Degraded: ExclamationTriangleIcon,
  Failed: ExclamationCircleIcon,
  Deleting: TrashIcon,
};

export interface StatusBadgeProps {
  /** The object: the badge shows its phase (stored status.phase, else derived). */
  item?: PhaseSubject | null;
  /** Legacy props for callers that pass status fields only. */
  status?: string | null;
  ready?: boolean;
  message?: string;
  lastTransition?: string;
  /** Without the details popover (dense tables that show the summary next to it). */
  plain?: boolean;
}

/** Health bucket of an object for counters, donuts and filters (from its phase). */
export function healthOf(item: PhaseSubject | null | undefined): HealthBucket {
  return healthOfPhase(phaseOf(item));
}

/**
 * Health bucket. Pass the object (preferred): the bucket then follows its
 * phase. The legacy form (ready, status) derives the phase from those two
 * fields only.
 */
export function normalizeHealth(readyOrItem?: boolean | PhaseSubject | null, status?: string | null): ResourceHealth {
  if (readyOrItem && typeof readyOrItem === 'object') return healthOf(readyOrItem);
  const s = (status ?? '').toLowerCase();
  if (s === 'error') return 'failed';
  if (s === 'success') return 'ready';
  if (s === 'running') return 'reconciling';
  if (s && !['ready', 'failed', 'pending', 'reconciling'].includes(s)) return readyOrItem === true ? 'ready' : 'unknown';
  return healthOfPhase(derivePhase({ status: { ready: readyOrItem, status: s || undefined } }));
}

function PopoverBody({ rows }: { rows: [string, React.ReactNode][] }): React.ReactElement {
  return (
    <DescriptionList isCompact isHorizontal>
      {rows.map(([term, desc]) => (
        <DescriptionListGroup key={term}>
          <DescriptionListTerm>{term}</DescriptionListTerm>
          <DescriptionListDescription>{desc}</DescriptionListDescription>
        </DescriptionListGroup>
      ))}
    </DescriptionList>
  );
}

/**
 * Status label with icon and text (never colour alone), one of the seven phase
 * words. The label is a real button: Tab reaches it and Enter opens the
 * details (summary, reason, since).
 */
export function StatusBadge({ item, status, ready, message, lastTransition, plain }: StatusBadgeProps): React.ReactElement {
  const { t } = useTranslation();
  const subject: PhaseSubject = item ?? { status: { status: status ?? undefined, ready, message, lastReconciledAt: lastTransition } };
  const phase = phaseOf(subject);
  const Icon = PHASE_ICON[phase];
  const word = t(`phase.${phase}`, { defaultValue: phase });
  const summary = summaryOf(subject);
  const reason = reasonOf(subject);
  const since = phaseSinceOf(subject) ?? lastTransition;

  const rows: [string, React.ReactNode][] = [];
  if (summary) rows.push([t('status.summary'), summary]);
  if (reason) rows.push([t('status.reason'), <code key="r">{reason}</code>]);
  if (since) {
    const rel = formatRelativeTime(since);
    rows.push([t('status.since'), <span key="s" title={rel.title}>{rel.label}</span>]);
  }

  const className = `sc-status-badge sc-status-badge--${phase.toLowerCase()}`;
  if (plain || rows.length === 0) {
    return (
      <Label color={PHASE_COLOR[phase]} icon={<Icon />} className={className}>
        {word}
      </Label>
    );
  }
  return (
    <Popover headerContent={t('status.title', { phase: word })} bodyContent={<PopoverBody rows={rows} />}>
      <Label
        color={PHASE_COLOR[phase]}
        icon={<Icon />}
        className={className}
        onClick={() => undefined}
        aria-label={t('status.badgeAria', { phase: word })}
      >
        {word}
      </Label>
    </Popover>
  );
}
