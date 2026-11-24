/**
 * The platform's own health, as the operations layer of each cluster reports
 * it (pure, no React): one ConfigMap in the platform namespace of the services
 * cluster, one data key `<cluster>.json` per reporting cluster.
 *
 *   {"schemaVersion": 1, "cluster": "hub", "writtenAt": "<RFC 3339>", "intervalSeconds": 300,
 *    "faults": [{"id": "ShcEventActivationsNotRunning", "severity": "critical", "since": "<RFC 3339>",
 *                "object": "<namespace>/<name>", "summary": "<the alert's summary>", "details": ["..."]}],
 *    "alertsDelivered": null}
 *
 * A key that does not have this shape is left out (the other keys still show).
 * A report whose writtenAt is older than three of its intervals is late: the
 * banner says so, because a writer that stopped reports nothing.
 */

export const PLATFORM_HEALTH_CONFIGMAP = 'shc-platform-health';
/** The platform namespace of the services cluster (where the platform's own objects live). */
export const PLATFORM_HEALTH_NAMESPACE = 'sovereign-cloud';
export const PLATFORM_HEALTH_SCHEMA_VERSION = 1;
/** A report is late when it is older than this many of its own intervals. */
export const LATE_AFTER_INTERVALS = 3;

export type FaultSeverity = 'critical' | 'warning' | 'info';

export interface PlatformFault {
  id: string;
  severity: FaultSeverity;
  since?: string;
  object?: string;
  summary?: string;
  details: string[];
  /** The reporting cluster, as the report names it. */
  cluster: string;
}

export interface PlatformHealthReport {
  /** The data key the report came from. */
  key: string;
  cluster: string;
  writtenAt: string;
  intervalSeconds: number;
  faults: PlatformFault[];
  /** true / false when the cluster reports whether its alerts reach a receiver; null when it does not say. */
  alertsDelivered: boolean | null;
  /** writtenAt is older than LATE_AFTER_INTERVALS intervals. */
  late: boolean;
}

export interface PlatformHealthView {
  /** Every fault of every report, critical first, then warning, then info; older first within a severity. */
  faults: PlatformFault[];
  /** Reports whose writer is late. */
  late: PlatformHealthReport[];
  /** Reports that say their alerts reach no receiver. */
  undelivered: PlatformHealthReport[];
  /** Data keys left out because they do not have the report's shape. */
  malformed: string[];
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

/** One data key's report, or null when the value does not have the report's shape. */
export function parseReport(key: string, value: unknown, nowMs: number = Date.now()): PlatformHealthReport | null {
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
  if (!validTime(obj.writtenAt)) return null;
  const interval = obj.intervalSeconds;
  if (typeof interval !== 'number' || !Number.isFinite(interval) || interval <= 0) return null;
  if (obj.faults !== undefined && obj.faults !== null && !Array.isArray(obj.faults)) return null;
  const cluster = nonEmptyString(obj.cluster) ? obj.cluster : key.replace(/\.json$/, '');
  const faults = (Array.isArray(obj.faults) ? obj.faults : [])
    .map((f) => parseFault(f, cluster))
    .filter((f): f is PlatformFault => !!f);
  const ageMs = nowMs - Date.parse(obj.writtenAt);
  return {
    key,
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

/** What the banner shows for the ConfigMap's data (null or not an object: nothing). */
export function platformHealthView(data: unknown, nowMs: number = Date.now()): PlatformHealthView {
  const view: PlatformHealthView = { faults: [], late: [], undelivered: [], malformed: [] };
  if (!isObject(data)) return view;
  for (const key of Object.keys(data).sort()) {
    if (!key.endsWith('.json')) continue;
    const report = parseReport(key, data[key], nowMs);
    if (!report) {
      view.malformed.push(key);
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
  return view.faults.length > 0 || view.late.length > 0 || view.undelivered.length > 0;
}

/** The ConfigMap's path below the API base (`/api/k8s`, or the console's `/api/kubernetes`). */
export function platformHealthPath(base: string): string {
  return `${base.replace(/\/+$/, '')}/api/v1/namespaces/${PLATFORM_HEALTH_NAMESPACE}/configmaps/${PLATFORM_HEALTH_CONFIGMAP}`;
}

/** The ConfigMap's data from an answer of the API (anything else: null). */
export function platformHealthData(answer: unknown): Record<string, unknown> | null {
  if (!isObject(answer) || !isObject(answer.data)) return null;
  return answer.data;
}
