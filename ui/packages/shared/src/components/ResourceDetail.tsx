import React, { useMemo, useState } from 'react';
import {
  Alert,
  Button,
  Label,
  Modal,
  ModalHeader,
  ModalBody,
  ModalFooter,
  ModalVariant,
  Spinner,
  Tab,
  Tabs,
  TabTitleText,
} from '@patternfly/react-core';
import { ArrowLeftIcon, SyncAltIcon, TrashIcon } from '@patternfly/react-icons';
import {
  deleteDashboardResource,
  forceReconcile,
  useK8sResource,
} from '../hooks/k8s';
import { HybridSovereignKind, K8sResource } from '../types';
import { GenericSpecEditor } from '../forms/GenericSpecEditor';
import { PageHeader } from './PageHeader';
import { StatusBadge } from './StatusBadge';
import { RefreshFailedAlert } from './ResourceListTable';
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
  onDeleted?: () => void;
  /** Show the object without edit, reconcile or delete actions */
  readOnly?: boolean;
  /** tenant pages hide platform credentials and platform-only fields (default tenant) */
  audience?: 'admin' | 'tenant';
}

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
  const pairs = flattenObject(data);
  if (pairs.length === 0) {
    return <p className="sc-text-muted">No data available.</p>;
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

function ConditionsTab({ item }: { item: K8sResource }): React.ReactElement {
  const conditions = (item.status as { conditions?: Array<Record<string, string>> } | undefined)
    ?.conditions;
  if (!conditions?.length) {
    return <p className="sc-text-muted">No conditions reported.</p>;
  }
  return (
    <div className="sc-conditions">
      {conditions.map((c, i) => {
        const isTrue = c.status === 'True';
        const color = c.type === 'Ready' ? (isTrue ? 'green' : 'red') : c.type === 'Failure' ? 'red' : 'blue';
        return (
          <div key={`${c.type}-${i}`} className="sc-condition">
            <Label color={color as 'green' | 'red' | 'blue'} isCompact>
              {c.type}
            </Label>
            <div>
              <div className="sc-condition__type">{isTrue ? 'True' : c.status || 'Unknown'}</div>
              {c.message && <div className="sc-condition__message">{c.message}</div>}
              {c.reason && (
                <div className="sc-condition__message">
                  Reason: <code>{c.reason}</code>
                </div>
              )}
            </div>
            {c.lastTransitionTime && (
              <div className="sc-condition__time">{new Date(c.lastTransitionTime).toLocaleString()}</div>
            )}
          </div>
        );
      })}
    </div>
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
  const [activeTab, setActiveTab] = useState<string | number>(0);
  const [actionError, setActionError] = useState<string | null>(null);
  const [reconciling, setReconciling] = useState(false);
  const [cooldown, setCooldown] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);

  const { resource, loading, error, refreshError, loadedAt, refresh } = useK8sResource<K8sResource>(kind, name, {
    namespace,
    pollWhileInFlight: true,
  });

  const readyStatus =
    resource?.status?.status ||
    (resource?.status?.ready === true ? 'ready' : resource?.status?.ready === false ? 'failed' : 'pending');

  /** What this page shows: tenant pages never see platform credential references. */
  const shown = useMemo(
    () => (resource && audience === 'tenant' ? redactForTenant(kind, resource) : resource),
    [resource, audience, kind],
  );

  const overviewFields = useMemo(() => {
    if (!resource || !shown) return {};
    const statusRaw = { ...(shown.status ?? {}) } as Record<string, unknown>;
    for (const key of [...COMMON_STATUS_KEYS, ...(KIND_STATUS_KEYS[kind] ?? [])]) {
      delete statusRaw[key];
    }
    return {
      Name: resource.metadata.name,
      Namespace: resource.metadata.namespace ?? namespace,
      ...(resource.metadata.labels ? { Labels: resource.metadata.labels } : {}),
      ...statusRaw,
    };
  }, [resource, shown, namespace, kind]);

  const handleReconcile = async () => {
    if (reconciling || cooldown || !namespace || !name) return;
    setReconciling(true);
    setActionError(null);
    try {
      await forceReconcile(kind, name, namespace);
      setCooldown(true);
      setTimeout(() => setCooldown(false), 10000);
      refresh();
    } catch (err) {
      setActionError(err instanceof Error ? err.message : 'Reconcile failed');
    } finally {
      setReconciling(false);
    }
  };

  const handleDelete = async () => {
    setDeleting(true);
    setActionError(null);
    try {
      await deleteDashboardResource(kind, name, namespace);
      setConfirmDelete(false);
      onDeleted?.();
      onBack();
    } catch (err) {
      setActionError(err instanceof Error ? err.message : 'Delete failed');
    } finally {
      setDeleting(false);
    }
  };

  if (loading && !resource) {
    return <Spinner aria-label={`Loading ${kind}`} />;
  }

  if (error && !resource) {
    return (
      <Alert variant="danger" title={`Unable to load ${kind}`} isInline>
        {error.message}
      </Alert>
    );
  }

  if (!resource) {
    return (
      <Alert variant="warning" title="Resource not found" isInline>
        {kind} <strong>{name}</strong> was not found in {namespace}.
      </Alert>
    );
  }

  return (
    <div className="sc-detail">
      <PageHeader
        title={name}
        subtitle={`${kind} in ${namespace}`}
        breadcrumbs={[
          { label: 'Sovereign Cloud' },
          { label: parentTitle, to: parentPath },
          { label: name },
        ]}
        actions={
          <>
            <Button variant="link" icon={<ArrowLeftIcon />} onClick={onBack}>
              Back to {parentTitle}
            </Button>
            {!readOnly && (
              <Button
                variant="secondary"
                icon={reconciling ? <Spinner size="md" /> : <SyncAltIcon />}
                isDisabled={reconciling || cooldown}
                onClick={handleReconcile}
              >
                {cooldown ? 'Reconcile queued' : 'Reconcile now'}
              </Button>
            )}
            <Button variant="secondary" icon={<SyncAltIcon />} onClick={refresh}>
              Refresh
            </Button>
            {!readOnly && (
              <Button variant="danger" icon={<TrashIcon />} onClick={() => setConfirmDelete(true)}>
                Delete
              </Button>
            )}
          </>
        }
      />

      <div className="sc-detail__badges" style={{ marginBottom: '1rem' }}>
        <StatusBadge
          status={resource.status?.status}
          ready={resource.status?.ready}
          message={resource.status?.message}
          lastTransition={resource.status?.lastReconciledAt}
        />
        <Label color="grey" isCompact>
          {kind}
        </Label>
        {resource.status?.lastReconciledAt && (
          <span className="sc-text-muted" style={{ fontSize: '0.75rem' }}>
            Reconciled {new Date(resource.status.lastReconciledAt).toLocaleString()}
          </span>
        )}
        <span className="sc-text-muted" style={{ fontSize: '0.75rem' }}>
          Sync: {readyStatus}
        </span>
      </div>

      {refreshError && <RefreshFailedAlert error={refreshError} loadedAt={loadedAt} onRetry={refresh} />}

      {actionError && (
        <Alert
          variant="danger"
          title="Action failed"
          isInline
          style={{ marginBottom: '1rem' }}
          actionClose={<Button variant="plain" onClick={() => setActionError(null)} aria-label="Close" />}
        >
          {actionError}
        </Alert>
      )}

      <Tabs
        activeKey={activeTab}
        onSelect={(_e, k) => setActiveTab(k)}
        className="sc-detail__tabs"
      >
        <Tab eventKey={0} title={<TabTitleText>Overview</TabTitleText>}>
          <div style={{ paddingTop: '1rem' }}>
            <StatusWaitBlocks item={resource} />
            {shown && <KindStatusDetails kind={kind} item={shown} />}
            <RealizationBlock item={resource} />
            <FieldGrid data={overviewFields} />
          </div>
        </Tab>
        <Tab eventKey={1} title={<TabTitleText>Configuration</TabTitleText>}>
          <div style={{ paddingTop: '1rem' }}>
            <SpecEditor
              kind={kind}
              namespace={namespace}
              item={resource}
              onSaved={refresh}
              readOnly={readOnly}
              audience={audience}
            />
          </div>
        </Tab>
        <Tab eventKey={2} title={<TabTitleText>Events / Conditions</TabTitleText>}>
          <div style={{ paddingTop: '1rem' }}>
            <ConditionsTab item={resource} />
          </div>
        </Tab>
        <Tab eventKey={3} title={<TabTitleText>YAML</TabTitleText>}>
          <div style={{ paddingTop: '1rem' }}>
            <pre className="sc-yaml">{JSON.stringify(shown, null, 2)}</pre>
          </div>
        </Tab>
      </Tabs>

      <Modal
        variant={ModalVariant.small}
        isOpen={confirmDelete}
        onClose={() => setConfirmDelete(false)}
      >
        <ModalHeader title={`Delete ${kind}?`} />
        <ModalBody>
          This will permanently delete <strong>{name}</strong> from namespace{' '}
          <strong>{namespace}</strong>.
        </ModalBody>
        <ModalFooter>
          <Button key="confirm" variant="danger" isDisabled={deleting} onClick={handleDelete}>
            {deleting ? 'Deleting…' : 'Delete'}
          </Button>
          <Button key="cancel" variant="link" onClick={() => setConfirmDelete(false)}>
            Cancel
          </Button>
        </ModalFooter>
      </Modal>
    </div>
  );
}
