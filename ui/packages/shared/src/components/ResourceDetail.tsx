import React, { useEffect, useMemo, useRef, useState } from 'react';
import {
  Alert,
  Button,
  ExpandableSection,
  Label,
  Spinner,
  Tab,
  Tabs,
  TabTitleText,
} from '@patternfly/react-core';
import { ArrowLeftIcon, RedoIcon, SyncAltIcon, TrashIcon } from '@patternfly/react-icons';
import { useLocation } from 'react-router-dom';
import {
  deleteDashboardResource,
  forceReconcile,
  useK8sResource,
} from '../hooks/k8s';
import { classifyError } from '../hooks/errors';
import { HybridSovereignKind, K8sResource, KIND_PLURALS } from '../types';
import { GenericSpecEditor } from '../forms/GenericSpecEditor';
import { PageHeader } from './PageHeader';
import { StatusBadge } from './StatusBadge';
import { StatusCallout } from './StatusCallout';
import { ToastHost, pushToast } from './ToastHost';
import { RefreshFailedAlert } from './ResourceListTable';
import { EnvironmentNetworksTab, NetworkEnvironmentsTab, isEnvironmentKind } from './PlacementTabs';
import { WayInPanel } from './WayInPanel';
import { DeleteConfirmModal } from './DeleteConfirmModal';
import { GatedButton } from '../access/GatedButton';
import { ForbiddenState } from '../access/ForbiddenState';
import { errorText } from '../access/errorText';
import { useTranslation } from '../i18n';
import { condStatus, phaseOf, retryQueued } from '../status/phase';
import { formatRelativeTime } from '../list/listColumns';
import {
  COMMON_STATUS_KEYS,
  KIND_STATUS_KEYS,
  KindStatusDetails,
  RealizationBlock,
  StatusWaitBlocks,
  redactForTenant,
} from './KindStatusDetails';

export interface ResourceDetailProps {
  kind: HybridSovereignKind;
  name: string;
  namespace: string;
  parentTitle: string;
  parentPath: string;
  onBack: () => void;
  /** Called once the deleted object is gone (the page shows Deleting until then). */
  onDeleted?: () => void;
  /** Show the object without edit, retry or delete actions */
  readOnly?: boolean;
  /** tenant pages hide platform credentials and platform-only fields (default tenant) */
  audience?: 'admin' | 'tenant';
}

/**
 * Status keys the Overview grid leaves out: the status contract (shown by the
 * badge and the callout), and the platform's delivery diagnostics (admins see
 * them under Diagnostics).
 */
const CONTRACT_KEYS = ['phase', 'phaseSince', 'summary', 'endpoints', 'config', 'lastHandledReconcileRequest', 'retry'];
const DIAGNOSTIC_KEYS = ['delivery', 'edaJobs', 'observedGeneration', 'lastReconciledAt', 'status', 'ready', 'message', 'realized'];

function formatValue(val: unknown): React.ReactNode {
  if (val === null || val === undefined || val === '') {
    return <span className="sc-text-muted">—</span>;
  }
  if (typeof val === 'boolean') return val ? 'true' : 'false';
  if (typeof val === 'object') {
    return <code style={{ fontSize: '0.8125rem' }}>{JSON.stringify(val)}</code>;
  }
  const str = String(val);
  if (str.startsWith('http://') || str.startsWith('https://')) {
    return (
      <a href={str} target="_blank" rel="noopener noreferrer" style={{ wordBreak: 'break-all' }}>
        {str}
      </a>
    );
  }
  return str;
}

function flattenObject(obj: Record<string, unknown>, prefix = ''): [string, unknown][] {
  if (!obj || typeof obj !== 'object') return [];
  return Object.entries(obj).flatMap(([k, v]) => {
    const key = prefix ? `${prefix}.${k}` : k;
    if (v && typeof v === 'object' && !Array.isArray(v)) {
      return flattenObject(v as Record<string, unknown>, key);
    }
    return [[key, v] as [string, unknown]];
  });
}

function FieldGrid({ data }: { data: Record<string, unknown> }): React.ReactElement {
  const { t } = useTranslation();
  const pairs = flattenObject(data);
  if (pairs.length === 0) {
    return <p className="sc-text-muted">{t('detail.noData')}</p>;
  }
  return (
    <div className="sc-fields">
      {pairs.map(([label, val]) => (
        <div key={label} className="sc-fields__row">
          <div className="sc-fields__label">{label}</div>
          <div className="sc-fields__value">{formatValue(val)}</div>
        </div>
      ))}
    </div>
  );
}

/**
 * Conditions coloured by meaning, not by value: a condition whose good value
 * is False (Stalled, Degraded, DeletionBlocked, Failure) is red only when True.
 */
const NEGATIVE_POLARITY = new Set(['Stalled', 'Degraded', 'DeletionBlocked', 'Failure', 'Reconciling', 'DeliveryStalled']);

function ConditionsTab({ item }: { item: K8sResource }): React.ReactElement {
  const { t } = useTranslation();
  const conditions = (item.status as { conditions?: Array<Record<string, string>> } | undefined)?.conditions;
  if (!conditions?.length) {
    return <p className="sc-text-muted">{t('detail.noConditions')}</p>;
  }
  return (
    <div className="sc-conditions">
      {conditions.map((c, i) => {
        const value = condStatus(c.status);
        const bad = NEGATIVE_POLARITY.has(c.type) ? value === 'True' : value === 'False';
        const color = value === 'Unknown' ? 'grey' : c.type === 'Reconciling' && value === 'True' ? 'blue' : bad ? 'red' : 'green';
        const rel = c.lastTransitionTime ? formatRelativeTime(c.lastTransitionTime) : undefined;
        return (
          <div key={`${c.type}-${i}`} className="sc-condition">
            <Label color={color as 'green' | 'red' | 'blue' | 'grey'} isCompact>
              {c.type}
            </Label>
            <div>
              <div className="sc-condition__type">{value}</div>
              {c.message && <div className="sc-condition__message">{c.message}</div>}
              {c.reason && (
                <div className="sc-condition__message">
                  {t('status.reason')}: <code>{c.reason}</code>
                </div>
              )}
            </div>
            {rel && (
              <div className="sc-condition__time" title={rel.title}>
                {rel.label}
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}

/** The object as JSON without server bookkeeping (managedFields). */
function JsonTab({ item }: { item: K8sResource | null }): React.ReactElement {
  const shown = useMemo(() => {
    if (!item) return item;
    const copy = JSON.parse(JSON.stringify(item)) as K8sResource & { metadata: { managedFields?: unknown } };
    delete copy.metadata.managedFields;
    return copy;
  }, [item]);
  return <pre className="sc-yaml">{JSON.stringify(shown, null, 2)}</pre>;
}

/** Admins only: delivery and job diagnostics of the platform, collapsed. */
function DiagnosticsBlock({ item }: { item: K8sResource }): React.ReactElement | null {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const status = (item.status ?? {}) as Record<string, unknown>;
  const data: Record<string, unknown> = {};
  for (const k of [...DIAGNOSTIC_KEYS, ...CONTRACT_KEYS]) if (status[k] !== undefined) data[k] = status[k];
  if (!Object.keys(data).length) return null;
  const jobs = Array.isArray(status.edaJobs) ? (status.edaJobs as Array<{ name?: string; jobId?: string; url?: string; status?: string }>) : [];
  return (
    <ExpandableSection toggleText={t('detail.diagnostics')} isExpanded={open} onToggle={(_e, v) => setOpen(v)} className="sc-mb">
      {jobs.length > 0 && (
        <ul>
          {jobs.map((j, i) => (
            <li key={`${j.jobId ?? j.name}-${i}`}>
              {j.url ? (
                <a href={j.url} target="_blank" rel="noopener noreferrer">
                  {j.name ?? j.jobId}
                </a>
              ) : (
                j.name ?? j.jobId
              )}{' '}
              <span className="sc-text-muted">{j.status}</span>
            </li>
          ))}
        </ul>
      )}
      <FieldGrid data={data} />
    </ExpandableSection>
  );
}

function SpecEditor(props: {
  kind: HybridSovereignKind;
  namespace: string;
  item: K8sResource;
  onSaved: () => void;
  readOnly?: boolean;
  audience?: 'admin' | 'tenant';
}): React.ReactElement {
  return <GenericSpecEditor {...props} />;
}

export function ResourceDetail({
  kind,
  name,
  namespace,
  parentTitle,
  parentPath,
  onBack,
  onDeleted,
  readOnly = false,
  audience = 'tenant',
}: ResourceDetailProps): React.ReactElement {
  const { t } = useTranslation();
  // ?tab=networks / ?tab=environments opens that tab (links from the Attach dialog, GT-F3).
  const { search } = useLocation();
  const tabParam = new URLSearchParams(search).get('tab');
  const linkedTab = tabParam === 'networks' || tabParam === 'environments' ? tabParam : null;
  const [activeTab, setActiveTab] = useState<string | number>(linkedTab ?? 0);
  useEffect(() => {
    if (linkedTab) setActiveTab(linkedTab);
  }, [linkedTab, kind, name]);
  const [actionError, setActionError] = useState<string | null>(null);
  const [retrying, setRetrying] = useState(false);
  const [retrySent, setRetrySent] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);
  /** A delete was requested here: keep polling until the object is gone (UX-061). */
  const [deleteRequested, setDeleteRequested] = useState(false);
  const kindLabel = t(`kindsOne.${kind}`, { defaultValue: kind });

  const { resource, loading, error, refreshError, loadedAt, refresh } = useK8sResource<K8sResource>(kind, name, {
    namespace,
    pollWhileInFlight: true,
    // After a delete request the object may not show deletionTimestamp yet: poll fast regardless.
    pollIntervalMs: deleteRequested ? 5000 : undefined,
  });

  // The object went away after a delete requested from this page: say so and go to the parent.
  const goneHandled = useRef(false);
  useEffect(() => {
    if (!deleteRequested || goneHandled.current) return;
    if (error && classifyError(error) === 'notFound') {
      goneHandled.current = true;
      pushToast({ variant: 'info', title: t('toasts.deleted', { kind: kindLabel, name }) });
      (onDeleted ?? onBack)();
    }
  }, [deleteRequested, error, kindLabel, name, onBack, onDeleted, t]);

  /** What this page shows: tenant pages never see platform credential references. */
  const shown = useMemo(
    () => (resource && audience === 'tenant' ? redactForTenant(kind, resource) : resource),
    [resource, audience, kind],
  );

  const overviewFields = useMemo(() => {
    if (!resource || !shown) return {};
    const statusRaw = { ...(shown.status ?? {}) } as Record<string, unknown>;
    for (const key of [...COMMON_STATUS_KEYS, ...(KIND_STATUS_KEYS[kind] ?? []), ...CONTRACT_KEYS, ...DIAGNOSTIC_KEYS]) {
      delete statusRaw[key];
    }
    return {
      [t('detail.name')]: resource.metadata.name,
      [t('detail.namespace')]: resource.metadata.namespace ?? namespace,
      ...(resource.metadata.labels ? { [t('detail.labels')]: resource.metadata.labels } : {}),
      ...statusRaw,
    };
  }, [resource, shown, namespace, kind, t]);

  const queued = retrySent || (resource ? retryQueued(resource) : false);
  const handleRetry = async () => {
    if (retrying || queued || !namespace || !name) return;
    setRetrying(true);
    setActionError(null);
    try {
      await forceReconcile(kind, name, namespace);
      setRetrySent(true);
      setTimeout(() => setRetrySent(false), 10000);
      refresh();
    } catch (err) {
      const txt = errorText(err, t, { kindLabel, action: 'action' });
      setActionError([txt.title, txt.body].filter(Boolean).join(' '));
    } finally {
      setRetrying(false);
    }
  };

  const handleDelete = async () => {
    setDeleting(true);
    setActionError(null);
    try {
      await deleteDashboardResource(kind, name, namespace);
      setConfirmDelete(false);
      // Stay on the page: it shows Deleting and what the delete waits for, then the object is gone.
      setDeleteRequested(true);
      refresh();
    } catch (err) {
      const txt = errorText(err, t, { kindLabel, action: 'action' });
      setActionError([txt.title, txt.body].filter(Boolean).join(' '));
    } finally {
      setDeleting(false);
    }
  };

  if (loading && !resource) {
    return <Spinner aria-label={t('pages.loadingKind', { kind: kindLabel })} />;
  }

  if (error && !resource) {
    if (deleteRequested && classifyError(error) === 'notFound') {
      return <Alert variant="info" isInline title={t('toasts.deleted', { kind: kindLabel, name })} />;
    }
    const cls = classifyError(error);
    // A forbidden page says who grants access, like a forbidden list (GT-F5).
    if (cls === 'forbidden') return <ForbiddenState kind={kind} name={name} />;
    const txt = errorText(error, t, { kindLabel, action: 'get' });
    // A gone object says so in words, without the raw API text (GT-F9).
    return (
      <Alert variant={cls === 'notFound' ? 'warning' : 'danger'} title={txt.title} isInline>
        {cls === 'notFound' ? t('errors.notFoundObject', { kind: kindLabel, name }) : txt.body || error.message}
      </Alert>
    );
  }

  if (!resource) {
    return (
      <Alert variant="warning" title={t('errors.notFound')} isInline>
        {kindLabel} <strong>{name}</strong>
      </Alert>
    );
  }

  const phase = phaseOf(resource);
  const since = resource.status?.phaseSince;
  const sinceRel = since ? formatRelativeTime(since) : undefined;
  const isDeleting = phase === 'Deleting' || deleteRequested;

  return (
    <div className="sc-detail">
      <ToastHost />
      <PageHeader
        title={name}
        subtitle={t('detail.subtitle', { kind: kindLabel, namespace })}
        breadcrumbs={[
          { label: t('nav.sovereignCloud') },
          { label: parentTitle, to: parentPath },
          { label: name },
        ]}
        actions={
          <>
            <Button variant="link" icon={<ArrowLeftIcon />} onClick={onBack}>
              {t('detail.backTo', { parent: parentTitle })}
            </Button>
            {!readOnly && (
              <GatedButton
                kind={kind}
                verb="patch"
                namespace={namespace}
                variant="secondary"
                icon={retrying ? <Spinner size="md" /> : <RedoIcon />}
                enabled={!retrying && !queued}
                disabledReason={queued ? t('detail.retryQueued') : undefined}
                onClick={() => void handleRetry()}
              >
                {queued ? t('detail.retryQueued') : t('detail.retryNow')}
              </GatedButton>
            )}
            <Button variant="secondary" icon={<SyncAltIcon />} onClick={refresh}>
              {t('common.refresh')}
            </Button>
            {!readOnly && (
              <GatedButton
                kind={kind}
                verb="delete"
                namespace={namespace}
                variant="danger"
                icon={<TrashIcon />}
                enabled={!isDeleting}
                disabledReason={isDeleting ? t('callout.deleting') : undefined}
                onClick={() => setConfirmDelete(true)}
              >
                {t('common.delete')}
              </GatedButton>
            )}
          </>
        }
      />

      <div className="sc-detail__badges" style={{ marginBottom: '1rem' }}>
        <StatusBadge item={resource} />
        {sinceRel && (
          <span className="sc-text-muted" style={{ fontSize: '0.75rem' }} title={sinceRel.title}>
            {t('callout.since', { time: sinceRel.label })}
          </span>
        )}
        {/* The command that reads this object (the API word, never a raw kind in the page text). */}
        <Label color="grey" isCompact title={t('detail.cliGetTitle')}>
          <code>{t('detail.cliGet', { resource: KIND_PLURALS[kind] ?? kind.toLowerCase(), name })}</code>
        </Label>
      </div>

      <StatusCallout kind={kind} item={resource} />

      {refreshError && <RefreshFailedAlert error={refreshError} loadedAt={loadedAt} onRetry={refresh} />}

      {actionError && (
        <Alert
          variant="danger"
          title={t('detail.actionFailed')}
          isInline
          style={{ marginBottom: '1rem' }}
          actionClose={<Button variant="plain" onClick={() => setActionError(null)} aria-label={t('common.close', { defaultValue: 'Close' })} />}
        >
          {actionError}
        </Alert>
      )}

      <Tabs activeKey={activeTab} onSelect={(_e, k) => setActiveTab(k)} className="sc-detail__tabs">
        <Tab eventKey={0} title={<TabTitleText>{t('detail.overviewTab')}</TabTitleText>}>
          <div style={{ paddingTop: '1rem' }}>
            {isEnvironmentKind(kind) && shown && <WayInPanel kind={kind} item={shown} />}
            <StatusWaitBlocks item={resource} />
            {shown && <KindStatusDetails kind={kind} item={shown} />}
            {shown && <RealizationBlock item={shown} />}
            {audience === 'admin' && <DiagnosticsBlock item={resource} />}
            <FieldGrid data={overviewFields} />
          </div>
        </Tab>
        {isEnvironmentKind(kind) && (
          <Tab eventKey="networks" title={<TabTitleText>{t('attach.networksTab')}</TabTitleText>}>
            <EnvironmentNetworksTab kind={kind} name={name} namespace={namespace} item={resource} readOnly={readOnly || isDeleting} />
          </Tab>
        )}
        {kind === 'HybridNetwork' && (
          <Tab eventKey="environments" title={<TabTitleText>{t('attach.environmentsTab')}</TabTitleText>}>
            <NetworkEnvironmentsTab network={name} namespace={namespace} readOnly={readOnly || isDeleting} />
          </Tab>
        )}
        <Tab eventKey={1} title={<TabTitleText>{t('detail.configurationTab')}</TabTitleText>}>
          <div style={{ paddingTop: '1rem' }}>
            <SpecEditor
              kind={kind}
              namespace={namespace}
              item={resource}
              onSaved={refresh}
              readOnly={readOnly || isDeleting}
              audience={audience}
            />
          </div>
        </Tab>
        <Tab eventKey={2} title={<TabTitleText>{t('detail.conditionsTab')}</TabTitleText>}>
          <div style={{ paddingTop: '1rem' }}>
            <ConditionsTab item={resource} />
          </div>
        </Tab>
        <Tab eventKey={3} title={<TabTitleText>{t('detail.jsonTab')}</TabTitleText>}>
          <div style={{ paddingTop: '1rem' }}>
            <JsonTab item={shown} />
          </div>
        </Tab>
      </Tabs>

      <DeleteConfirmModal
        isOpen={confirmDelete}
        kind={kind}
        item={resource}
        deleting={deleting}
        error={confirmDelete ? actionError : null}
        onConfirm={() => void handleDelete()}
        onCancel={() => setConfirmDelete(false)}
      />
    </div>
  );
}
