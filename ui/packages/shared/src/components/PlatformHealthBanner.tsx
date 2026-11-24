import React, { useCallback, useEffect, useState } from 'react';
import { Alert } from '@patternfly/react-core';
import { useTranslation } from '../i18n';
import { getK8sClientConfig } from '../hooks/k8s';
import { IDLE_POLL_MS, pollDelayMs } from '../hooks/fetchState';
import {
  hasPlatformHealthNews,
  platformHealthData,
  platformHealthPath,
  platformHealthView,
  type FaultSeverity,
  type PlatformFault,
} from '../status/platformHealth';

/**
 * The platform's own faults, on every platform page: one alert per fault the
 * clusters report (critical first), a product sentence per fault id (the
 * alert's summary when the id has none), the report's detail lines, since
 * when and on which cluster; a warning when a cluster's report is late. Read
 * with the signed-in person's token; nothing when the object is absent or
 * cannot be read.
 */

const VARIANT: Record<FaultSeverity, 'danger' | 'warning' | 'info'> = {
  critical: 'danger',
  warning: 'warning',
  info: 'info',
};

function useTimeText(): (iso: string | undefined) => string {
  const { i18n } = useTranslation();
  const lng = i18n?.language || 'en';
  return useCallback(
    (iso: string | undefined) => {
      if (!iso) return '';
      const ms = Date.parse(iso);
      if (Number.isNaN(ms)) return iso;
      try {
        return new Intl.DateTimeFormat(lng, { dateStyle: 'medium', timeStyle: 'short' }).format(ms);
      } catch {
        return iso;
      }
    },
    [lng],
  );
}

function FaultAlert({ fault, time }: { fault: PlatformFault; time: (iso: string | undefined) => string }): React.ReactElement {
  const { t } = useTranslation();
  const key = `platformHealth.fault.${fault.id}`;
  const sentence = t(key, { defaultValue: '' });
  const title = sentence && sentence !== key ? sentence : fault.summary || t('platformHealth.unknownFault', { id: fault.id });
  const meta = [
    fault.since ? t('platformHealth.since', { time: time(fault.since) }) : '',
    t('platformHealth.onCluster', { cluster: fault.cluster }),
    fault.object ? t('platformHealth.object', { object: fault.object }) : '',
  ].filter(Boolean);
  return (
    <Alert variant={VARIANT[fault.severity]} isInline title={title} className="sc-mb" data-fault-id={fault.id}>
      {fault.details.length > 0 && (
        <ul className="sc-platform-health__details">
          {fault.details.map((d, i) => (
            <li key={i}>{d}</li>
          ))}
        </ul>
      )}
      <div className="sc-platform-health__meta">{meta.join(' · ')}</div>
    </Alert>
  );
}

export interface PlatformHealthBannerViewProps {
  /** The ConfigMap's data; null or undefined when it is absent or cannot be read. */
  data: Record<string, unknown> | null | undefined;
  /** Epoch ms the lateness of a report is judged at (default: now). */
  nowMs?: number;
}

/** The banner for given ConfigMap data (no reads; the reading banner is PlatformHealthBanner). */
export function PlatformHealthBannerView({ data, nowMs }: PlatformHealthBannerViewProps): React.ReactElement | null {
  const { t } = useTranslation();
  const time = useTimeText();
  if (!data) return null;
  const view = platformHealthView(data, nowMs ?? Date.now());
  if (!hasPlatformHealthNews(view)) return null;
  const critical = view.faults.filter((f) => f.severity === 'critical');
  const rest = view.faults.filter((f) => f.severity !== 'critical');
  return (
    <section className="sc-platform-health" aria-label={t('platformHealth.label')}>
      {critical.map((f, i) => (
        <FaultAlert key={`c-${f.cluster}-${f.id}-${f.object ?? ''}-${i}`} fault={f} time={time} />
      ))}
      {view.late.map((r) => (
        <Alert
          key={`late-${r.key}`}
          variant="warning"
          isInline
          title={t('platformHealth.lateReport', { cluster: r.cluster, time: time(r.writtenAt) })}
          className="sc-mb"
          data-late-report={r.cluster}
        >
          {t('platformHealth.lateReportBody')}
        </Alert>
      ))}
      {rest.map((f, i) => (
        <FaultAlert key={`w-${f.cluster}-${f.id}-${f.object ?? ''}-${i}`} fault={f} time={time} />
      ))}
      {view.undelivered.map((r) => (
        <Alert
          key={`undelivered-${r.key}`}
          variant="warning"
          isInline
          title={t('platformHealth.alertsNotDelivered', { cluster: r.cluster })}
          className="sc-mb"
        >
          {t('platformHealth.alertsNotDeliveredBody')}
        </Alert>
      ))}
    </section>
  );
}

/**
 * Reads the platform-health ConfigMap; null when it is absent or cannot be read.
 * A refusal is not reported to the permission gate (a person without the read
 * simply sees no banner, and the periodic read must not reset the gate's cache).
 */
export async function readPlatformHealth(): Promise<Record<string, unknown> | null> {
  const cfg = getK8sClientConfig();
  const headers: Record<string, string> = { Accept: 'application/json' };
  if (cfg.token) headers.Authorization = `Bearer ${cfg.token}`;
  const fetchImpl = cfg.fetchFn ?? fetch;
  try {
    const response = await fetchImpl(platformHealthPath(cfg.baseUrl ?? '/api/k8s'), { headers });
    if (!response.ok) return null;
    return platformHealthData(await response.json());
  } catch {
    return null;
  }
}

function useDocumentHidden(): boolean {
  const [hidden, setHidden] = useState(() => typeof document !== 'undefined' && document.hidden === true);
  useEffect(() => {
    if (typeof document === 'undefined') return;
    const onChange = () => setHidden(document.hidden === true);
    document.addEventListener('visibilitychange', onChange);
    return () => document.removeEventListener('visibilitychange', onChange);
  }, []);
  return hidden;
}

/** The banner, read now and again on the pages' usual interval (slower while the tab is hidden). */
export function PlatformHealthBanner(): React.ReactElement | null {
  const [data, setData] = useState<Record<string, unknown> | null>(null);
  const [tick, setTick] = useState(0);
  const hidden = useDocumentHidden();
  useEffect(() => {
    let cancelled = false;
    void readPlatformHealth().then((d) => {
      if (!cancelled) setData(d);
    });
    return () => {
      cancelled = true;
    };
  }, [tick]);
  useEffect(() => {
    const delay = pollDelayMs({ pollIntervalMs: IDLE_POLL_MS, inFlight: false, hidden });
    const id = setTimeout(() => setTick((n) => n + 1), delay);
    return () => clearTimeout(id);
  }, [tick, hidden]);
  return <PlatformHealthBannerView data={data} />;
}
