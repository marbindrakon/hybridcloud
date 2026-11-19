/**
 * The installation of an OpenShift cluster as the pages show it, read from `status.install`
 * (stage, attempt, attempts limit, run, start, end and check times, the last failed attempt).
 *
 * What the cluster page says while the installation runs and when it stopped, what the action
 * button reads and asks, and which Configuration fields may change in which stage. The texts come
 * from the string catalogue: the functions take `t` and return the sentences, so the tests read
 * the same words the page shows.
 *
 * Pure module: type-only imports besides the phase and duration modules, so it can be bundled for
 * node tests.
 */
import { detailOf, isInFlight, phaseOf, reasonOf, retryQueued, type PhaseSubject } from './phase';
import { CLUSTER_INSTALL_MINUTES } from './durations';

export const INSTALL_STAGES = ['Preparing', 'CleaningUp', 'Installing', 'Finishing', 'Installed', 'Stopped'] as const;
export type InstallStage = (typeof INSTALL_STAGES)[number];

/** Stages of an installation that is still running. */
export const INSTALL_RUNNING_STAGES: ReadonlySet<InstallStage> = new Set<InstallStage>([
  'Preparing',
  'CleaningUp',
  'Installing',
  'Finishing',
]);

/** The reason of the Ready condition while an attempt failed and the installer tries again. */
export const INSTALL_RETRYING_REASON = 'InstallRetrying';

/**
 * The reason of the Ready condition of an installation past the time allowed: shown failed while the
 * platform keeps checking it (it becomes ready if it finishes; Retry installation once the installer stops).
 */
export const INSTALL_OVERDUE_REASON = 'InstallOverdue';

/** A last check older than this many minutes is said plainly: the state shown may be out of date. */
export const INSTALL_STALE_AFTER_MINUTES = 15;

export interface InstallFailure {
  attempt?: number;
  reason?: string;
  message?: string;
  time?: string;
}

export interface InstallStatus {
  stage?: InstallStage;
  attempt?: number;
  attemptsLimit?: number;
  run?: number;
  startedAt?: string;
  endedAt?: string;
  observedAt?: string;
  lastFailure?: InstallFailure;
}

type T = (key: string, opts?: Record<string, unknown>) => string;

function rec(v: unknown): Record<string, unknown> | undefined {
  return v && typeof v === 'object' && !Array.isArray(v) ? (v as Record<string, unknown>) : undefined;
}

function posInt(v: unknown): number | undefined {
  const n = typeof v === 'number' ? v : typeof v === 'string' && v.trim() ? Number(v) : NaN;
  return Number.isFinite(n) && n >= 0 ? Math.trunc(n) : undefined;
}

function str(v: unknown): string | undefined {
  return typeof v === 'string' && v.trim() ? v : undefined;
}

function isStage(v: unknown): v is InstallStage {
  return typeof v === 'string' && (INSTALL_STAGES as readonly string[]).includes(v);
}

/** status.install with well-formed values only (a stage outside the list reads as no stage). */
export function installOf(item: PhaseSubject | null | undefined): InstallStatus | undefined {
  const raw = rec(rec(item?.status)?.install);
  if (!raw) return undefined;
  const out: InstallStatus = {};
  if (isStage(raw.stage)) out.stage = raw.stage;
  for (const k of ['attempt', 'attemptsLimit', 'run'] as const) {
    const n = posInt(raw[k]);
    if (n !== undefined) out[k] = n;
  }
  for (const k of ['startedAt', 'endedAt', 'observedAt'] as const) {
    const s = str(raw[k]);
    if (s) out[k] = s;
  }
  const lf = rec(raw.lastFailure);
  if (lf) {
    const f: InstallFailure = {};
    const a = posInt(lf.attempt);
    if (a !== undefined) f.attempt = a;
    for (const k of ['reason', 'message', 'time'] as const) {
      const s = str(lf[k]);
      if (s) f[k] = s;
    }
    out.lastFailure = f;
  }
  return out;
}

export function installStage(item: PhaseSubject | null | undefined): InstallStage | undefined {
  return installOf(item)?.stage;
}

/** The installation ended without a cluster: Retry installation starts a new one. */
export function isInstallStopped(item: PhaseSubject | null | undefined): boolean {
  return installStage(item) === 'Stopped';
}

function msSince(iso: string | undefined, now: number): number | undefined {
  if (!iso) return undefined;
  const then = Date.parse(iso);
  return Number.isNaN(then) ? undefined : Math.max(0, now - then);
}

/**
 * A steady length of time: seconds under a minute, then whole minutes, and hours with minutes
 * from two hours on ("40 seconds", "12 minutes", "84 minutes", "2 hours 5 minutes").
 */
export function elapsedText(t: T, ms: number): string {
  const sec = Math.max(0, Math.floor(ms / 1000));
  if (sec < 60) return t('install.seconds', { count: sec });
  const min = Math.floor(sec / 60);
  if (min < 120) return t('install.minutes', { count: min });
  const hours = Math.floor(min / 60);
  const rest = min % 60;
  const h = t('install.hours', { count: hours });
  return rest ? t('install.hoursMinutes', { hours: h, minutes: t('install.minutes', { count: rest }) }) : h;
}

/** What the callout says under the summary while an installation runs. */
export interface InstallProgress {
  /** warning while an attempt failed and the installer tries again */
  variant: 'info' | 'warning';
  /** "Installing, attempt 2 of 3." */
  stage?: string;
  /** "Started 12 minutes ago. Last checked 20 seconds ago." */
  times?: string;
  /** the failed attempt, while the installer tries again */
  failure?: string;
  /** past the usual time: "This is taking longer than usual. …" (shown instead of the estimate) */
  longer?: string;
  /** the last check is old: "The state shown was last checked 20 minutes ago." */
  stale?: string;
  /** whether the usual-duration line still applies */
  showEstimate: boolean;
}

/**
 * The attempt the installer runs or is about to run. While a failed attempt is retried the recorded attempt can
 * still be the one that failed (the next has not started yet): the line then names the next one, so it never repeats
 * the failed attempt shown below it. Undefined when no attempt is left (the failure line says enough).
 */
function currentAttempt(install: InstallStatus, retrying: boolean): number | undefined | null {
  const failed = retrying ? install.lastFailure?.attempt : undefined;
  if (failed === undefined) return install.attempt;
  const next = Math.max(install.attempt ?? 0, failed + 1);
  if (install.attemptsLimit && next > install.attemptsLimit) return null;
  return next;
}

function stageSentence(t: T, install: InstallStatus, retrying = false): string | undefined {
  switch (install.stage) {
    case 'Preparing':
      return t('install.stagePreparing');
    case 'CleaningUp':
      return t('install.stageCleaningUp');
    case 'Installing': {
      const attempt = currentAttempt(install, retrying);
      if (attempt === null) return undefined;
      if (attempt && install.attemptsLimit) {
        return t('install.stageInstalling', { attempt, limit: install.attemptsLimit });
      }
      return attempt ? t('install.stageInstallingAttempt', { attempt }) : t('install.stageInstallingPlain');
    }
    case 'Finishing':
      return t('install.stageFinishing');
    default:
      return undefined;
  }
}

/**
 * The installation lines of a cluster that is being created or changed and whose installation runs;
 * null otherwise (no status.install, another phase, or an installation that ended).
 */
export function installProgress(t: T, item: PhaseSubject | null | undefined, now: number = Date.now()): InstallProgress | null {
  const install = installOf(item);
  if (!install?.stage || !INSTALL_RUNNING_STAGES.has(install.stage)) return null;
  const phase = phaseOf(item);
  if (phase !== 'Creating' && phase !== 'Updating') return null;

  const retrying = reasonOf(item) === INSTALL_RETRYING_REASON;
  const out: InstallProgress = { variant: 'info', showEstimate: true, stage: stageSentence(t, install, retrying) };

  const elapsed = msSince(install.startedAt, now);
  const checked = msSince(install.observedAt, now);
  const stale = checked !== undefined && checked > INSTALL_STALE_AFTER_MINUTES * 60_000;
  const times = [
    elapsed !== undefined ? t('install.started', { elapsed: elapsedText(t, elapsed) }) : '',
    checked !== undefined && !stale ? t('install.lastChecked', { elapsed: elapsedText(t, checked) }) : '',
  ].filter(Boolean);
  if (times.length) out.times = times.join(' ');
  if (stale && checked !== undefined) out.stale = t('install.stale', { elapsed: elapsedText(t, checked) });

  if (retrying) {
    out.variant = 'warning';
    out.failure = detailOf(item) ?? install.lastFailure?.message;
  }

  if (elapsed !== undefined && elapsed > CLUSTER_INSTALL_MINUTES.max * 60_000) {
    out.showEstimate = false;
    out.longer =
      install.stage === 'Finishing'
        ? t('install.longerFinishing')
        : install.attemptsLimit
          ? t('install.longerThanUsual', { limit: install.attemptsLimit })
          : t('install.longerThanUsualNoLimit');
  }
  return out;
}

/**
 * What the failed callout says about the installation; null when the failure is not the installation's.
 * Stage Stopped: the installation ended without a cluster and Retry installation starts a new one.
 * Past the time allowed (reason InstallOverdue while the stage says the installer runs): after the summary
 * ("…The platform keeps checking it."), what follows: ready if it finishes, a retry once the installer stops,
 * delete now. Retry installation is not offered while it runs.
 */
export function installStoppedCallout(t: T, item: PhaseSubject | null | undefined): { title: string; retryLine: string } | null {
  if (phaseOf(item) !== 'Failed') return null;
  const stage = installStage(item);
  if (stage === 'Stopped') return { title: t('install.stoppedTitle'), retryLine: t('install.retryUses') };
  if (stage && INSTALL_RUNNING_STAGES.has(stage) && reasonOf(item) === INSTALL_OVERDUE_REASON) {
    return { title: t('install.unfinishedTitle'), retryLine: t('install.unfinishedLine') };
  }
  return null;
}

/** The label key of the retry button: Retry installation on a stopped installation, else Retry now. */
export function retryLabelKey(item: PhaseSubject | null | undefined): 'install.retryInstallation' | 'detail.retryNow' {
  return isInstallStopped(item) ? 'install.retryInstallation' : 'detail.retryNow';
}

/** Node counts of a cluster's spec (the provider's block named by spec.type); undefined when not set. */
export function clusterNodeCounts(spec: unknown): { controlPlane?: number; workers?: number } {
  const s = rec(spec);
  const type = typeof s?.type === 'string' ? s.type : '';
  const block = type ? rec(s?.[type]) : undefined;
  return { controlPlane: posInt(block?.controlPlaneCount), workers: posInt(block?.workerCount) };
}

/**
 * The confirmation before a new installation starts (Retry installation, or Save on a stopped
 * cluster): the question, what happens, and the node counts it installs with.
 */
export function retryConfirmation(t: T, spec: unknown): { title: string; body: string; settings: string; confirm: string } {
  const { controlPlane, workers } = clusterNodeCounts(spec);
  return {
    title: t('install.confirmTitle'),
    body: t('install.confirmBody'),
    settings: t('install.confirmSettings', {
      controlPlane: controlPlane !== undefined ? t('install.controlPlaneNodes', { count: controlPlane }) : t('install.controlPlaneDefault'),
      workers: workers !== undefined ? t('install.workers', { count: workers }) : t('install.workersDefault'),
    }),
    confirm: t('install.confirmButton'),
  };
}

/**
 * Whether the install-time fields of a cluster may change, as the API decides it:
 * open (the installation has not started) and stopped accept a change; installing and installed
 * refuse it. status.clusterInstalled true is installed whatever the stage. An object without status.install (or still preparing) counts as installing once
 * status.installStarted is true and as installed once status.clusterInstalled is true.
 */
export type InstallEditState = 'open' | 'stopped' | 'installing' | 'installed';

export function installEditState(item: PhaseSubject | null | undefined): InstallEditState {
  const status = rec(item?.status) ?? {};
  const stage = installStage(item);
  // a cluster recorded as installed is never reinstalled from an edit, whatever the stage says
  if (status.clusterInstalled === true) return 'installed';
  if (stage === 'Stopped') return 'stopped';
  if (stage === 'Installed') return 'installed';
  if (stage === 'CleaningUp' || stage === 'Installing' || stage === 'Finishing') return 'installing';
  if (status.installStarted === true) return 'installing';
  return 'open';
}

/**
 * Spec fields the installer reads once and the platform does not apply to an installed cluster
 * (dot paths under spec): read-only while the cluster installs and once it is installed. The same list
 * as the API's rule (a backend test compares them). Fields fixed from creation (the type, environment,
 * network, region, address ranges) are not listed: they are read-only in every state.
 */
export const INSTALL_TIME_FIELDS: readonly string[] = [
  'openstack.controlPlaneCount',
  'openstack.workerCount',
  'openstack.controlPlaneFlavor',
  'openstack.workerFlavor',
  'openstack.ocpImageSet',
  'openstack.glanceImageOverride',
  'openstack.trustBundle',
  'openstack.pullSecretVaultPath',
  'openstack.pullSecretVaultProperty',
  'openstack.sshKeyVaultPath',
  'aws.publish',
  'aws.clusterType',
  'aws.controlPlaneCount',
  'aws.controllerFlavor',
  'aws.workerFlavor',
  'gcp.publish',
  'gcp.controlPlaneCount',
  'gcp.controllerFlavor',
  'gcp.workerFlavor',
];

/**
 * Spec fields the platform applies to an installed cluster (the worker count on AWS and Google Cloud):
 * read-only only while the cluster installs. The same list as the API's rule.
 */
export const INSTALLED_CHANGEABLE_FIELDS: readonly string[] = ['aws.workerCount', 'gcp.workerCount'];

const INSTALL_TIME_SET = new Set(INSTALL_TIME_FIELDS);
const INSTALLED_CHANGEABLE_SET = new Set(INSTALLED_CHANGEABLE_FIELDS);

export function isInstallTimeField(path: string): boolean {
  return INSTALL_TIME_SET.has(path);
}

/** Whether a field is read-only in this state of the installation (as the API's rule decides). */
export function isInstallLocked(path: string, state: InstallEditState): boolean {
  if (state === 'installing') return INSTALL_TIME_SET.has(path) || INSTALLED_CHANGEABLE_SET.has(path);
  if (state === 'installed') return INSTALL_TIME_SET.has(path);
  return false;
}

/** JSON text with the keys of every object sorted (the order a form keeps them in does not count). */
function stableText(v: unknown): string {
  if (Array.isArray(v)) return `[${v.map(stableText).join(',')}]`;
  const r = rec(v);
  if (r) {
    return `{${Object.keys(r)
      .filter((k) => r[k] !== undefined)
      .sort()
      .map((k) => `${JSON.stringify(k)}:${stableText(r[k])}`)
      .join(',')}}`;
  }
  return JSON.stringify(v ?? null);
}

/** The part of a cluster's spec the installer reads (its type and the provider blocks), as text. */
function installerPart(spec: unknown): string {
  const s = rec(spec) ?? {};
  return stableText(['type', 'openstack', 'aws', 'gcp'].map((k) => s[k] ?? null));
}

/**
 * Whether a change of a stopped cluster starts a new installation: only a change of the installer's part
 * of the spec does (the type or a provider block: size, flavors, release, image); access groups, viewers
 * and deletion protection are applied without one.
 */
export function installSettingsChanged(before: unknown, after: unknown): boolean {
  return installerPart(before) !== installerPart(after);
}

/** How often an open detail page reads its object while it is not settled, and otherwise. */
export const DETAIL_POLL_UNSETTLED_MS = 12_000;
export const DETAIL_POLL_SETTLED_MS = 60_000;

/**
 * The refresh interval of an object's detail page: every DETAIL_POLL_UNSETTLED_MS while the object is not settled
 * (being created, changed or deleted, waiting, a Retry or reconcile request not yet handled, or an installation
 * recorded as running), else every DETAIL_POLL_SETTLED_MS. A page that has not read the object yet counts as not
 * settled.
 */
export function detailPollMs(item: PhaseSubject | null | undefined, opts: { retrySent?: boolean } = {}): number {
  if (!item) return DETAIL_POLL_UNSETTLED_MS;
  if (opts.retrySent || retryQueued(item)) return DETAIL_POLL_UNSETTLED_MS;
  if (isInFlight(phaseOf(item))) return DETAIL_POLL_UNSETTLED_MS;
  const stage = installStage(item);
  if (stage && INSTALL_RUNNING_STAGES.has(stage)) return DETAIL_POLL_UNSETTLED_MS;
  return DETAIL_POLL_SETTLED_MS;
}

/**
 * What Save does on a cluster's Configuration tab: on a cluster the page believes stopped, a change of the
 * installation settings always asks first ('confirm'; the API then answers the save itself, its refusal included),
 * whatever the server's check said; any other change is saved once the check passed ('save'), else nothing.
 */
export function saveStep(
  state: InstallEditState,
  before: unknown,
  after: unknown,
  checkPassed: boolean,
): 'confirm' | 'save' | 'none' {
  if (state === 'stopped' && installSettingsChanged(before, after)) return 'confirm';
  return checkPassed ? 'save' : 'none';
}
