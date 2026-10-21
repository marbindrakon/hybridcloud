import React from 'react';
import { Card, CardBody, CardTitle, Label } from '@patternfly/react-core';
import { useTranslation } from '../i18n';
import { usualCreateDuration } from '../status/durations';

/** What a create will do, in one short paragraph (Q8, UX-090, UX-045). */
export function reviewLines(
  kind: string,
  name: string,
  namespace: string,
  spec: Record<string, unknown> | null | undefined,
  t: (k: string, o?: Record<string, unknown>) => string,
): string[] {
  const lines: string[] = [];
  const kindLabel = t(`kindsOne.${kind}`, { defaultValue: kind });
  const site = ((spec?.cloudRef ?? {}) as { name?: string }).name;
  lines.push(
    site
      ? t('review.createsOnSite', { kind: kindLabel, name: name || '…', namespace, site })
      : t('review.creates', { kind: kindLabel, name: name || '…', namespace }),
  );
  if (kind === 'CloudVirt') {
    const nets = ((spec?.namespaces ?? []) as Array<{ name?: string; network?: string }>).filter((n) => n.network);
    if (nets.length) {
      lines.push(
        t('review.virtNetworks', {
          list: nets.map((n) => `${n.name} → ${n.network}`).join(', '),
        }),
      );
    }
  }
  if (kind === 'NetworkPlacement') {
    const backend = (spec?.backend ?? {}) as { kind?: string; name?: string };
    if (spec?.network && backend.name) {
      lines.push(
        t('review.attaches', {
          network: String(spec.network),
          environment: `${t(`kindsOne.${backend.kind}`, { defaultValue: backend.kind ?? '' })} ${backend.name}`,
        }),
      );
    }
  }
  const usual = usualCreateDuration(kind);
  if (usual) lines.push(t(usual.key, { defaultValue: usual.defaultValue }));
  return lines;
}

export function ReviewPanel({
  kind,
  name,
  namespace,
  spec,
}: {
  kind: string;
  name: string;
  namespace: string;
  spec: Record<string, unknown> | null | undefined;
}): React.ReactElement {
  const { t } = useTranslation();
  const billable = kind === 'CloudAWS' || kind === 'CloudGCP';
  return (
    <Card isCompact className="sc-mb sc-review">
      <CardTitle>{t('review.title')}</CardTitle>
      <CardBody>
        {reviewLines(kind, name, namespace, spec, t).map((l) => (
          <p key={l}>{l}</p>
        ))}
        {billable && (
          <Label color="orange" isCompact>
            {t('sites.billable')}
          </Label>
        )}
      </CardBody>
    </Card>
  );
}
