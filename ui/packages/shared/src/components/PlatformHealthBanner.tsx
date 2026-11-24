import React, { useCallback, useEffect, useState } from 'react';
import { Alert } from '@patternfly/react-core';
import { useTranslation } from '../i18n';
import { getK8sClientConfig } from '../hooks/k8s';
import { IDLE_POLL_MS, pollDelayMs } from '../hooks/fetchState';
import { loadUiConfig } from '../shell/uiConfig';
import {
  hasPlatformHealthNews,
  parseIndex,
  platformHealthPath,
  platformHealthReportName,
  platformHealthView,
  reportReadOf,
  type FaultSeverity,
  type PlatformFault,
  type PlatformHealthRead,
  type ReportRead,
} from '../status/platformHealth';

/**
 * The platform's own faults, on every platform page: one alert per fault the
 * clusters report (critical first), a product sentence per fault id (the
 * alert's summary when the id has none), the report's detail lines, since
 * when and on which cluster; a warning for each listed cluster whose report is
 * late, was never written or cannot be read. Read with the signed-in person's
 * token, in the platform namespace the dashboard's settings name; nothing when
 * the index is absent or refused (reporting off, or the person may not read
 * it); "platform health cannot be read" when the server answers with an error.
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

function Notice({ title, body, attr }: { title: string; body: string; attr: Record<string, string> }): React.ReactElement {
  return (
    <Alert variant="warning" isInline title={title} className="sc-mb" {...attr}>
      {body}
    </Alert>
  );
}

export interface PlatformHealthBannerViewProps {
  /** What was read; null or undefined when the index is absent or cannot be fetched. */
  read: PlatformHealthRead | null | undefined;
  /** Epoch ms the lateness of a report is judged at (default: now). */
  nowMs?: number;
}

/** The banner for what was read (no reads; the reading banner is PlatformHealthBanner). */
export function PlatformHealthBannerView({ read, nowMs }: PlatformHealthBannerViewProps): React.ReactElement | null {
  const { t } = useTranslation();
  const time = useTimeText();
  if (!read) return null;
  const view = platformHealthView(read, nowMs ?? Date.now());
  if (!hasPlatformHealthNews(view)) return null;
  const critical = view.faults.filter((f) => f.severity === 'critical');
  const rest = view.faults.filter((f) => f.severity !== 'critical');
  return (
    <section className="sc-platform-health" aria-label={t('platformHealth.label')}>
      {view.indexFailed && (
        <Notice
          title={t('platformHealth.indexFailed')}
          body={t('platformHealth.indexFailedBody')}
          attr={{ 'data-index-failed': 'true' }}
        />
      )}
      {view.indexUnreadable && (
        <Notice
          title={t('platformHealth.unreadableIndex')}
          body={t('platformHealth.unreadableIndexBody')}
          attr={{ 'data-index-unreadable': 'true' }}
        />
      )}
      {critical.map((f, i) => (
        <FaultAlert key={`c-${f.cluster}-${f.id}-${f.object ?? ''}-${i}`} fault={f} time={time} />
      ))}
      {view.late.map((r) => (
        <Notice
          key={`late-${r.cluster}`}
          title={t('platformHealth.lateReport', { cluster: r.cluster, time: time(r.writtenAt) })}
          body={t('platformHealth.lateReportBody')}
          attr={{ 'data-late-report': r.cluster }}
        />
      ))}
      {view.neverWritten.map((n) => (
        <Notice
          key={`never-${n.cluster}`}
          title={
            n.since
              ? t('platformHealth.neverWritten', { cluster: n.cluster, time: time(n.since) })
              : t('platformHealth.neverWrittenNoTime', { cluster: n.cluster })
          }
          body={t('platformHealth.neverWrittenBody')}
          attr={{ 'data-never-written': n.cluster }}
        />
      ))}
      {view.unreadable.map((c) => (
        <Notice
          key={`unreadable-${c}`}
          title={t('platformHealth.unreadableReport', { cluster: c })}
          body={t('platformHealth.unreadableReportBody')}
          attr={{ 'data-unreadable-report': c }}
        />
      ))}
      {rest.map((f, i) => (
        <FaultAlert key={`w-${f.cluster}-${f.id}-${f.object ?? ''}-${i}`} fault={f} time={time} />
      ))}
      {view.undelivered.map((r) => (
        <Alert
          key={`undelivered-${r.cluster}`}
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

type Fetch = (url: string, init: { headers: Record<string, string> }) => Promise<{ ok: boolean; status: number; json: () => Promise<unknown> }>;

async function readReport(
  fetchImpl: Fetch,
  base: string,
  namespace: string,
  headers: Record<string, string>,
  cluster: string,
): Promise<ReportRead> {
  try {
    const response = await fetchImpl(platformHealthPath(base, namespace, platformHealthReportName(cluster)), { headers });
    if (response.status === 404) return { state: 'absent' };
    if (!response.ok) return { state: 'unreadable' };
    return reportReadOf(await response.json());
  } catch {
    return { state: 'unreadable' };
  }
}

/** The index answers that mean "nothing to show": absent (reporting off), refused, or no session. */
const INDEX_SILENT = new Set([401, 403, 404]);

export interface PlatformHealthSettings {
  /** The platform namespace (the dashboard's setting); empty: the settings were not read. */
  namespace?: string;
  /** The longest report interval believed, in minutes (the dashboard's setting). */
  maxReportIntervalMinutes?: number | null;
}

/**
 * Reads the index and each listed cluster's report; null when the index is
 * absent or refused (reporting is off, or the person has no read). An index
 * the server answers with another error, a failed read, or no platform
 * namespace in the dashboard's settings: `indexFailed`. A refusal is not
 * reported to the permission gate (the periodic read must not reset the gate's
 * cache).
 */
export async function readPlatformHealth(settings: PlatformHealthSettings): Promise<PlatformHealthRead | null> {
  const minutes = settings.maxReportIntervalMinutes;
  const max = typeof minutes === 'number' && minutes > 0 ? { maxIntervalSeconds: minutes * 60 } : {};
  const failed: PlatformHealthRead = { clusters: null, reports: {}, indexFailed: true, ...max };
  const namespace = settings.namespace ?? '';
  if (!namespace) return failed;
  const cfg = getK8sClientConfig();
  const headers: Record<string, string> = { Accept: 'application/json' };
  if (cfg.token) headers.Authorization = `Bearer ${cfg.token}`;
  const fetchImpl = (cfg.fetchFn ?? fetch) as unknown as Fetch;
  const base = cfg.baseUrl ?? '/api/k8s';
  let answer: unknown;
  try {
    const response = await fetchImpl(platformHealthPath(base, namespace), { headers });
    if (INDEX_SILENT.has(response.status)) return null;
    if (!response.ok) return failed;
    answer = await response.json();
  } catch {
    return failed;
  }
  const data = answer && typeof answer === 'object' ? (answer as { data?: unknown }).data : undefined;
  const clusters = parseIndex(data);
  if (clusters === null) return { clusters: null, reports: {}, ...max };
  const reads = await Promise.all(clusters.map((c) => readReport(fetchImpl, base, namespace, headers, c)));
  const reports: Record<string, ReportRead> = {};
  clusters.forEach((c, i) => {
    reports[c] = reads[i];
  });
  return { clusters, reports, ...max };
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
  const [read, setRead] = useState<PlatformHealthRead | null>(null);
  const [tick, setTick] = useState(0);
  const hidden = useDocumentHidden();
  useEffect(() => {
    let cancelled = false;
    void loadUiConfig()
      .then((c) =>
        readPlatformHealth({
          namespace: c?.platformNamespace,
          maxReportIntervalMinutes: c?.platformHealthMaxReportIntervalMinutes,
        }),
      )
      .then((d) => {
        if (!cancelled) setRead(d);
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
  return <PlatformHealthBannerView read={read} />;
}
