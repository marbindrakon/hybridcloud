import React from 'react';
import { Alert, Label } from '@patternfly/react-core';
import { useTranslation } from '../i18n';
import {
  actorOf,
  changeNotApplied,
  deletionBlockers,
  phaseOf,
  phaseSinceOf,
  reasonOf,
  summaryOf,
  type PhaseSubject,
} from '../status/phase';
import { usualDuration } from '../status/durations';
import { formatRelativeTime } from '../list/listColumns';

/**
 * One callout under the page title that says what is going on (Q10, UX-053,
 * UX-056): in progress (with the usual duration), failed (summary and reason,
 * and who acts), degraded, or deleting (since when and what it waits for).
 * Ready objects show nothing. A refused edit (Accepted=False) adds a chip.
 */
export function StatusCallout({ kind, item }: { kind: string; item: PhaseSubject }): React.ReactElement | null {
  const { t } = useTranslation();
  const phase = phaseOf(item);
  const summary = summaryOf(item);
  const reason = reasonOf(item);
  const actor = actorOf(item);
  const since = phaseSinceOf(item);
  const rel = since ? formatRelativeTime(since) : undefined;
  const usual = usualDuration(kind, phase);
  const refused = changeNotApplied(item);

  const sinceText = rel ? <span title={rel.title}>{t('callout.since', { time: rel.label })}</span> : null;
  const refusedChip = refused ? (
    <Label color="orange" isCompact style={{ marginTop: '0.5rem' }}>
      {t('callout.changeNotApplied', { reason: refused.reason || refused.message || '' })}
    </Label>
  ) : null;
  const actorText =
    actor === 'P' ? t('callout.platformActs') : actor === 'T' ? t('callout.youAct') : null;

  if (phase === 'Ready') return refusedChip ? <div className="sc-mb">{refusedChip}</div> : null;

  if (phase === 'Deleting') {
    const blockers = deletionBlockers(item);
    return (
      <Alert variant="custom" isInline className="sc-mb sc-status-callout" title={t('callout.deleting')}>
        <p>
          {summary} {sinceText}
        </p>
        {blockers.length > 0 && (
          <>
            <p>{t('callout.waitsFor')}</p>
            <ul>
              {blockers.map((b, i) => (
                <li key={`${b.kind}-${b.name}-${i}`}>
                  {[b.kind ? t(`kindsOne.${b.kind}`, { defaultValue: b.kind }) : '', b.name].filter(Boolean).join(' ')}
                  {b.reason ? <span className="sc-text-muted"> ({b.reason})</span> : null}
                </li>
              ))}
            </ul>
          </>
        )}
        {usual && <p className="sc-text-muted">{t(usual.key, { defaultValue: usual.defaultValue })}</p>}
      </Alert>
    );
  }

  if (phase === 'Failed' || phase === 'Degraded') {
    return (
      <Alert
        variant={phase === 'Failed' ? 'danger' : 'warning'}
        isInline
        className="sc-mb sc-status-callout"
        title={phase === 'Failed' ? t('callout.failed') : t('callout.degraded')}
      >
        <p>
          {summary} {sinceText}
        </p>
        {reason && (
          <p className="sc-text-muted">
            {t('status.reason')}: <code>{reason}</code>
            {actorText ? ` · ${actorText}` : ''}
          </p>
        )}
        {refusedChip}
      </Alert>
    );
  }

  // Waiting, Creating, Updating
  return (
    <Alert
      variant="info"
      isInline
      className="sc-mb sc-status-callout"
      title={t(`callout.${phase.toLowerCase()}`, { defaultValue: phase })}
    >
      <p>
        {summary} {sinceText}
      </p>
      {usual && <p className="sc-text-muted">{t(usual.key, { defaultValue: usual.defaultValue })}</p>}
      {phase === 'Waiting' && actorText && <p className="sc-text-muted">{actorText}</p>}
      {refusedChip}
    </Alert>
  );
}
