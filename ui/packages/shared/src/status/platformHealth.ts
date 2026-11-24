/**
 * The platform's own health, as the operations layer of each cluster reports
 * it (pure, no React). In the platform namespace of the services cluster, made
 * by the services cluster's chart:
 *   - the index, ConfigMap `shc-platform-health`, data key `clusters.json`:
 *     the names of the reporting clusters (a JSON list);
 *   - per reporting cluster, ConfigMap `shc-platform-health-<cluster>`, data key
 *     `report.json`, which that cluster's writer fills:
 *
 *   {"schemaVersion": 1, "cluster": "hub", "writtenAt": "<RFC 3339>", "intervalSeconds": 300,
 *    "faults": [{"id": "ShcEventActivationsNotRunning", "severity": "critical", "since": "<RFC 3339>",
 *                "object": "<namespace>/<name>", "summary": "<the alert's summary>", "details": ["..."]}],
 *    "alertsDelivered": null}
 *
 * Every cluster of the index shows, nothing is dropped silently: its faults;
 * "no report since" when the report is older than three of its intervals or
 * was never written; "cannot be read" when it cannot be fetched, does not
 * parse, has another schemaVersion or another cluster's name, or its writtenAt
 * is more than ten minutes in the future.
 */

export const PLATFORM_HEALTH_INDEX = 'shc-platform-health';
export const PLATFORM_HEALTH_INDEX_KEY = 'clusters.json';
export const PLATFORM_HEALTH_REPORT_PREFIX = 'shc-platform-health-';
export const PLATFORM_HEALTH_REPORT_KEY = 'report.json';
/** The platform namespace of the services cluster (where the platform's own objects live). */
export const PLATFORM_HEALTH_NAMESPACE = 'sovereign-cloud';
export const PLATFORM_HEALTH_SCHEMA_VERSION = 1;
/** A report is late when it is older than this many of its own intervals. */
export const LATE_AFTER_INTERVALS = 3;
/** A report written further ahead than this is not believed (a clock far off, or a forged time). */
export const FUTURE_TOLERANCE_MS = 10 * 60 * 1000;
/** At most this many clusters are read from the index. */
export const MAX_CLUSTERS = 50;

const CLUSTER_NAME = /^[a-z0-9]([-a-z0-9]*[a-z0-9])?$/;

export type FaultSeverity = 'critical' | 'warning' | 'info';

export interface PlatformFault {
  id: string;
  severity: FaultSeverity;
  since?: string;
  object?: string;
  summary?: string;
  details: string[];
  /** The reporting cluster. */
  cluster: string;
}

export interface PlatformHealthReport {
  cluster: string;
  writtenAt: string;
  intervalSeconds: number;
  faults: PlatformFault[];
  /** true / false when the cluster reports whether its alerts reach a receiver; null when it does not say. */
  alertsDelivered: boolean | null;
  /** writtenAt is older than LATE_AFTER_INTERVALS intervals. */
  late: boolean;
}

/** One cluster's report as it was read: its ConfigMap's data, absent (404) or not readable. */
export type ReportRead =
  | { state: 'read'; data: Record<string, unknown> | null; createdAt?: string }
  | { state: 'absent' }
  | { state: 'unreadable' };

/** What the banner reads: the index and each listed cluster's report. */
export interface PlatformHealthRead {
  /** The index's clusters; null when the index answered but its list cannot be read. */
  clusters: string[] | null;
  reports: Record<string, ReportRead>;
}

export interface NeverWritten {
  cluster: string;
  /** When the report's ConfigMap was made, if known. */
  since?: string;
}

export interface PlatformHealthView {
  /** Every fault of every report, critical first, then warning, then info; older first within a severity. */
  faults: PlatformFault[];
  /** Reports whose writer is late. */
  late: PlatformHealthReport[];
  /** Clusters whose report has never been written. */
  neverWritten: NeverWritten[];
  /** Clusters whose report cannot be read. */
  unreadable: string[];
  /** The index answered but its list of clusters cannot be read. */
  indexUnreadable: boolean;
  /** Reports that say their alerts reach no receiver. */
  undelivered: PlatformHealthReport[];
}

const SEVERITY_ORDER: Record<FaultSeverity, number> = { critical: 0, warning: 1, info: 2 };

function isObject(v: unknown): v is Record<string, unknown> {
  return !!v && typeof v === 'object' && !Array.isArray(v);
}

function validTime(v: unknown): v is string {
  return typeof v === 'string' && v.length > 0 && !Number.isNaN(Date.parse(v));
}

function nonEmptyString(v: unknown): v is string {
  return typeof v === 'string' && v.trim().length > 0;
}

function severityOf(v: unknown): FaultSeverity {
  return v === 'critical' || v === 'warning' || v === 'info' ? v : 'warning';
}

function parseFault(raw: unknown, cluster: string): PlatformFault | null {
  if (!isObject(raw) || !nonEmptyString(raw.id)) return null;
  const details = Array.isArray(raw.details) ? raw.details.filter(nonEmptyString) : [];
  return {
    id: raw.id,
    severity: severityOf(raw.severity),
    ...(validTime(raw.since) ? { since: raw.since } : {}),
    ...(nonEmptyString(raw.object) ? { object: raw.object } : {}),
    ...(nonEmptyString(raw.summary) ? { summary: raw.summary } : {}),
    details,
    cluster,
  };
}

/** The clusters of the index's data, or null when the list cannot be read (not a list of cluster names). */
export function parseIndex(data: unknown): string[] | null {
  if (!isObject(data)) return null;
  let list: unknown = data[PLATFORM_HEALTH_INDEX_KEY];
  if (typeof list !== 'string') return null;
  try {
    list = JSON.parse(list);
  } catch {
    return null;
  }
  if (!Array.isArray(list)) return null;
  if (!list.every((c) => typeof c === 'string' && c.length <= 63 && CLUSTER_NAME.test(c))) return null;
  return [...new Set(list as string[])].slice(0, MAX_CLUSTERS);
}

/**
 * One cluster's report from its value, or null when it cannot be read: not JSON, not the report's shape, another
 * schemaVersion, another cluster's name, or a writtenAt more than FUTURE_TOLERANCE_MS ahead of now.
 */
export function parseReport(cluster: string, value: unknown, nowMs: number = Date.now()): PlatformHealthReport | null {
  let obj: unknown = value;
  if (typeof value === 'string') {
    try {
      obj = JSON.parse(value);
    } catch {
      return null;
    }
  }
  if (!isObject(obj)) return null;
  if (obj.schemaVersion !== PLATFORM_HEALTH_SCHEMA_VERSION) return null;
  if (obj.cluster !== undefined && obj.cluster !== cluster) return null;
  if (!validTime(obj.writtenAt)) return null;
  const interval = obj.intervalSeconds;
  if (typeof interval !== 'number' || !Number.isFinite(interval) || interval <= 0) return null;
  if (obj.faults !== undefined && obj.faults !== null && !Array.isArray(obj.faults)) return null;
  const ageMs = nowMs - Date.parse(obj.writtenAt);
  if (ageMs < -FUTURE_TOLERANCE_MS) return null;
  const faults = (Array.isArray(obj.faults) ? obj.faults : [])
    .map((f) => parseFault(f, cluster))
    .filter((f): f is PlatformFault => !!f);
  return {
    cluster,
    writtenAt: obj.writtenAt,
    intervalSeconds: interval,
    faults,
    alertsDelivered: typeof obj.alertsDelivered === 'boolean' ? obj.alertsDelivered : null,
    late: ageMs > LATE_AFTER_INTERVALS * interval * 1000,
  };
}

/** Critical before warning before info; within one severity the older fault first, then by cluster and id. */
export function compareFaults(a: PlatformFault, b: PlatformFault): number {
  const s = SEVERITY_ORDER[a.severity] - SEVERITY_ORDER[b.severity];
  if (s) return s;
  const ta = a.since ? Date.parse(a.since) : Number.POSITIVE_INFINITY;
  const tb = b.since ? Date.parse(b.since) : Number.POSITIVE_INFINITY;
  if (ta !== tb) return ta < tb ? -1 : 1;
  return a.cluster.localeCompare(b.cluster) || a.id.localeCompare(b.id) || (a.object ?? '').localeCompare(b.object ?? '');
}

function emptyView(): PlatformHealthView {
  return { faults: [], late: [], neverWritten: [], unreadable: [], indexUnreadable: false, undelivered: [] };
}

/** What the banner shows for what was read (null: the index is absent or not readable, nothing is shown). */
export function platformHealthView(read: PlatformHealthRead | null | undefined, nowMs: number = Date.now()): PlatformHealthView {
  const view = emptyView();
  if (!read) return view;
  if (read.clusters === null) {
    view.indexUnreadable = true;
    return view;
  }
  for (const cluster of [...read.clusters].sort()) {
    const r = read.reports[cluster];
    if (!r || r.state === 'unreadable') {
      view.unreadable.push(cluster);
      continue;
    }
    if (r.state === 'absent') {
      view.neverWritten.push({ cluster });
      continue;
    }
    const value = r.data ? r.data[PLATFORM_HEALTH_REPORT_KEY] : undefined;
    if (value === undefined || value === null || value === '') {
      view.neverWritten.push({ cluster, ...(validTime(r.createdAt) ? { since: r.createdAt } : {}) });
      continue;
    }
    const report = parseReport(cluster, value, nowMs);
    if (!report) {
      view.unreadable.push(cluster);
      continue;
    }
    view.faults.push(...report.faults);
    if (report.late) view.late.push(report);
    if (report.alertsDelivered === false) view.undelivered.push(report);
  }
  view.faults.sort(compareFaults);
  return view;
}

/** True when the banner has something to show. */
export function hasPlatformHealthNews(view: PlatformHealthView): boolean {
  return (
    view.indexUnreadable ||
    view.faults.length > 0 ||
    view.late.length > 0 ||
    view.neverWritten.length > 0 ||
    view.unreadable.length > 0 ||
    view.undelivered.length > 0
  );
}

/** A ConfigMap's path below the API base (`/api/k8s`, or the console's `/api/kubernetes`). */
export function platformHealthPath(base: string, name: string = PLATFORM_HEALTH_INDEX): string {
  return `${base.replace(/\/+$/, '')}/api/v1/namespaces/${PLATFORM_HEALTH_NAMESPACE}/configmaps/${encodeURIComponent(name)}`;
}

/** The ConfigMap name of a cluster's report. */
export function platformHealthReportName(cluster: string): string {
  return `${PLATFORM_HEALTH_REPORT_PREFIX}${cluster}`;
}

/** A ConfigMap answer of the API as a report read (a ConfigMap without data: read, never written). */
export function reportReadOf(answer: unknown): ReportRead {
  if (!isObject(answer) || answer.kind !== 'ConfigMap') return { state: 'unreadable' };
  const meta = isObject(answer.metadata) ? answer.metadata : {};
  return {
    state: 'read',
    data: isObject(answer.data) ? answer.data : null,
    ...(validTime(meta.creationTimestamp) ? { createdAt: meta.creationTimestamp } : {}),
  };
}
