/**
 * The status contract seen from the UI (Q10, OD8).
 *
 * Every object of the platform carries a stored `status.phase` (one of seven
 * words), `status.phaseSince` and a tenant-safe `status.summary`, written by the
 * status library on each write. The UI shows the stored phase. Objects the
 * library has not written yet (older objects, or a status written by an older
 * job) get the phase derived here with the same six rules, in the same order,
 * as `derive_phase` in the Ansible collection's `shc_status.py`. Both
 * implementations run the shared fixtures in `tests/fixtures/status-phase-cases.json`.
 *
 * Pure module: type-only imports, so it can be bundled for node tests.
 */
import { DELETE_REQUESTED_SUFFIX, PHASE_SUMMARIES, REASONS, type ReasonClass, type ReasonEntry } from './reasons.generated';

export const PHASES = ['Waiting', 'Creating', 'Updating', 'Ready', 'Degraded', 'Failed', 'Deleting'] as const;
export type Phase = (typeof PHASES)[number];

/** Phases in which something is still happening (the UI polls faster then). */
export const IN_FLIGHT_PHASES: ReadonlySet<Phase> = new Set<Phase>(['Waiting', 'Creating', 'Updating', 'Deleting']);

/** Longest summary the UI shows on one line. */
export const SUMMARY_MAX = 140;

export interface PhaseCondition {
  type?: string;
  status?: string | boolean;
  reason?: string;
  message?: string;
  lastTransitionTime?: string;
}

export interface DeletionBlocker {
  kind?: string;
  name?: string;
  namespace?: string;
  reason?: string;
}

/** The parts of an object the status functions read (any K8sResource fits). */
export interface PhaseSubject {
  metadata?: {
    name?: string;
    generation?: number;
    deletionTimestamp?: string;
    creationTimestamp?: string;
    annotations?: Record<string, string>;
  };
  status?: unknown;
}

interface StatusView {
  phase?: unknown;
  phaseSince?: unknown;
  summary?: unknown;
  status?: unknown;
  ready?: unknown;
  message?: unknown;
  reason?: unknown;
  realized?: unknown;
  observedGeneration?: unknown;
  lastReconciledAt?: unknown;
  lastHandledReconcileRequest?: unknown;
  conditions?: unknown;
  deletionBlockedBy?: unknown;
}

function statusOf(item: PhaseSubject | null | undefined): StatusView {
  const s = item?.status;
  return s && typeof s === 'object' ? (s as StatusView) : {};
}

/** "True" | "False" | "Unknown" for any spelling the writers used (bools, lower case). */
export function condStatus(value: unknown): 'True' | 'False' | 'Unknown' {
  if (typeof value === 'boolean') return value ? 'True' : 'False';
  const low = String(value ?? '').toLowerCase();
  if (low === 'true') return 'True';
  if (low === 'false') return 'False';
  return 'Unknown';
}

export function conditionsOf(item: PhaseSubject | null | undefined): PhaseCondition[] {
  const c = statusOf(item).conditions;
  return Array.isArray(c) ? (c.filter((x) => x && typeof x === 'object') as PhaseCondition[]) : [];
}

export function findCondition(item: PhaseSubject | null | undefined, type: string): PhaseCondition | undefined {
  return conditionsOf(item).find((c) => c.type === type);
}

function condTrue(item: PhaseSubject | null | undefined, type: string): boolean {
  const c = findCondition(item, type);
  return !!c && condStatus(c.status) === 'True';
}

/** Registry entry of a reason (operator delete requests are named <Kind>DeleteRequested). */
export function reasonEntry(reason: string | undefined | null): ReasonEntry | undefined {
  if (!reason) return undefined;
  const e = REASONS[reason];
  if (e) return e;
  if (reason.endsWith(DELETE_REQUESTED_SUFFIX)) return REASONS.DeletionRequested;
  return undefined;
}

export function reasonClass(reason: string | undefined | null): ReasonClass | undefined {
  return reasonEntry(reason)?.class;
}

export function isPhase(v: unknown): v is Phase {
  return typeof v === 'string' && (PHASES as readonly string[]).includes(v);
}

function intOrUndefined(v: unknown): number | undefined {
  if (v === null || v === undefined || v === '') return undefined;
  const n = typeof v === 'number' ? v : Number(v);
  return Number.isFinite(n) ? Math.trunc(n) : undefined;
}

/**
 * The phase derived from the object (no stored phase involved). Same rules and
 * order as derive_phase in shc_status.py.
 */
export function derivePhase(item: PhaseSubject | null | undefined): Phase {
  const meta = item?.metadata ?? {};
  const status = statusOf(item);
  if (meta.deletionTimestamp) return 'Deleting';
  if (condTrue(item, 'Stalled')) return 'Failed';
  const readyCond = findCondition(item, 'Ready');
  const ready = readyCond ? condStatus(readyCond.status) === 'True' : status.ready === true;
  const observed = intOrUndefined(status.observedGeneration);
  const generation = intOrUndefined(meta.generation);
  if (ready) {
    if (condTrue(item, 'Degraded')) return 'Degraded';
    if (observed !== undefined && generation !== undefined && generation > observed) return 'Updating';
    return 'Ready';
  }
  const legacy = String(status.status ?? '').toLowerCase();
  const rclass = readyCond ? reasonClass(readyCond.reason) : undefined;
  if (legacy === 'failed' || rclass === 'failed' || rclass === 'blocked') return 'Failed';
  if (legacy === 'pending' || rclass === 'waiting') return 'Waiting';
  if (!observed && status.realized !== true) return 'Creating';
  return 'Updating';
}

/**
 * The phase to show: deletion always wins, then the stored `status.phase` when
 * it is one of the seven words, otherwise the derived phase.
 */
export function phaseOf(item: PhaseSubject | null | undefined): Phase {
  if (item?.metadata?.deletionTimestamp) return 'Deleting';
  const stored = statusOf(item).phase;
  if (isPhase(stored)) return stored;
  return derivePhase(item);
}

export function isInFlight(phase: Phase | null | undefined): boolean {
  return !!phase && IN_FLIGHT_PHASES.has(phase);
}

/** When the current phase started: stored phaseSince, else the deciding condition's transition time. */
export function phaseSinceOf(item: PhaseSubject | null | undefined): string | undefined {
  const status = statusOf(item);
  if (typeof status.phaseSince === 'string' && status.phaseSince) return status.phaseSince;
  const phase = phaseOf(item);
  if (phase === 'Deleting' && item?.metadata?.deletionTimestamp) return item.metadata.deletionTimestamp;
  const cond = (phase === 'Failed' && findCondition(item, 'Stalled')) || findCondition(item, 'Ready');
  if (cond?.lastTransitionTime) return cond.lastTransitionTime;
  if (typeof status.lastReconciledAt === 'string' && status.lastReconciledAt) return status.lastReconciledAt;
  return item?.metadata?.creationTimestamp;
}

/** The reason that decides the phase (Ready, or Stalled/DeletionBlocked where they decide). */
export function reasonOf(item: PhaseSubject | null | undefined): string | undefined {
  const phase = phaseOf(item);
  if (phase === 'Deleting') {
    const blocked = findCondition(item, 'DeletionBlocked');
    if (blocked && condStatus(blocked.status) === 'True' && blocked.reason) return blocked.reason;
  }
  if (phase === 'Failed') {
    const stalled = findCondition(item, 'Stalled');
    if (stalled && condStatus(stalled.status) === 'True' && stalled.reason) return stalled.reason;
  }
  if (phase === 'Degraded') {
    const degraded = findCondition(item, 'Degraded');
    if (degraded?.reason) return degraded.reason;
  }
  const ready = findCondition(item, 'Ready');
  if (ready?.reason) return ready.reason;
  const legacy = statusOf(item).reason;
  return typeof legacy === 'string' && legacy ? legacy : undefined;
}

function trimSummary(text: string): string {
  const one = text.split(/\s+/).filter(Boolean).join(' ');
  return one.length > SUMMARY_MAX ? `${one.slice(0, SUMMARY_MAX - 1).trimEnd()}…` : one;
}

/**
 * One line that says what is going on: the stored tenant-safe summary, else
 * the registry sentence of the deciding reason, else the legacy message, else
 * the phase sentence.
 */
export function summaryOf(item: PhaseSubject | null | undefined): string {
  const status = statusOf(item);
  if (typeof status.summary === 'string' && status.summary.trim()) return trimSummary(status.summary);
  const phase = phaseOf(item);
  const entry = reasonEntry(reasonOf(item));
  if (entry && phase !== 'Ready') return entry.summary;
  if (phase !== 'Ready' && typeof status.message === 'string' && status.message.trim()) {
    return trimSummary(status.message);
  }
  return PHASE_SUMMARIES[phase] ?? '';
}

/** Who acts next for the deciding reason (A automatic, T the tenant, P the platform team). */
export function actorOf(item: PhaseSubject | null | undefined): 'A' | 'T' | 'P' | undefined {
  return reasonEntry(reasonOf(item))?.actor;
}

/** A refused edit (the `Accepted` condition False): its reason and message, else null. */
export function changeNotApplied(item: PhaseSubject | null | undefined): { reason?: string; message?: string } | null {
  const c = findCondition(item, 'Accepted');
  if (!c || condStatus(c.status) !== 'False') return null;
  return { reason: c.reason, message: c.message };
}

/** What a requested delete waits for: typed entries, or the legacy list of strings. */
export function deletionBlockers(item: PhaseSubject | null | undefined): DeletionBlocker[] {
  const raw = statusOf(item).deletionBlockedBy;
  if (!Array.isArray(raw)) return [];
  return raw
    .map((b): DeletionBlocker | null => {
      if (typeof b === 'string') return b.trim() ? { name: b.trim() } : null;
      if (b && typeof b === 'object') {
        const o = b as Record<string, unknown>;
        return {
          kind: typeof o.kind === 'string' ? o.kind : undefined,
          name: typeof o.name === 'string' ? o.name : undefined,
          namespace: typeof o.namespace === 'string' ? o.namespace : undefined,
          reason: typeof o.reason === 'string' ? o.reason : undefined,
        };
      }
      return null;
    })
    .filter((b): b is DeletionBlocker => !!b);
}

export const RECONCILE_ANNOTATION = 'ansible.sdk.operatorframework.io/reconcileNow';

/** A Retry now request the controller has not picked up yet. */
export function retryQueued(item: PhaseSubject | null | undefined): boolean {
  const requested = item?.metadata?.annotations?.[RECONCILE_ANNOTATION];
  if (!requested) return false;
  const handled = statusOf(item).lastHandledReconcileRequest;
  return requested !== handled;
}

/**
 * Health buckets for counters, donuts and filters, from the phase:
 * ready = Ready; failed = Failed and Degraded; reconciling = Creating,
 * Updating, Deleting; pending = Waiting.
 */
export type HealthBucket = 'ready' | 'pending' | 'failed' | 'reconciling';

export function healthOfPhase(phase: Phase): HealthBucket {
  switch (phase) {
    case 'Ready':
      return 'ready';
    case 'Failed':
    case 'Degraded':
      return 'failed';
    case 'Waiting':
      return 'pending';
    default:
      return 'reconciling';
  }
}

/** Order for sorting by phase: things that need attention first. */
export const PHASE_SORT_ORDER: Record<Phase, number> = {
  Failed: 0,
  Degraded: 1,
  Waiting: 2,
  Deleting: 3,
  Creating: 4,
  Updating: 5,
  Ready: 6,
};
