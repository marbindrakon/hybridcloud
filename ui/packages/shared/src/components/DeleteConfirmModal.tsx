import React, { useEffect, useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import {
  Alert,
  Button,
  Form,
  FormGroup,
  Modal,
  ModalBody,
  ModalFooter,
  ModalHeader,
  ModalVariant,
  Spinner,
  TextInput,
} from '@patternfly/react-core';
import { useTranslation } from '../i18n';
import { fetchResource, fetchResourceList } from '../hooks/k8s';
import { useRouteFor } from '../nav/useNav';
import { tenantOfNamespace } from '../scope/parseGroups';
import { useOptionalScope } from '../scope/ScopeProvider';
import type { HybridSovereignKind, K8sResource } from '../types';
import {
  PREVIEW_GROUPS,
  deletePreview,
  previewReads,
  type PreviewContext,
  type PreviewEntry,
  type PreviewSection,
} from '../status/deletePreview';

/** Namespace of the tenant objects (Entity) on the services cluster. */
const TENANT_OBJECTS_NAMESPACE = 'sovereign-cloud';

interface LoadState {
  ctx: PreviewContext;
  loading: boolean;
  /** Some objects that may be affected could not be read */
  partial: boolean;
}

/** Reads what the preview of `kind` needs, once per opening. */
function usePreviewContext(kind: string, item: K8sResource | null, isOpen: boolean, readTenant: boolean): LoadState {
  const [state, setState] = useState<LoadState>({ ctx: {}, loading: false, partial: false });
  const namespace = item?.metadata.namespace ?? '';
  const name = item?.metadata.name ?? '';
  useEffect(() => {
    const reads = isOpen && item ? previewReads(kind, item) : [];
    const tenant = tenantOfNamespace(namespace);
    // Only platform readers may read the tenant object (its role bindings); tenants are told so instead.
    const readEntity = kind === 'Rbac' && !!tenant && readTenant;
    if (!reads.length && !readEntity) {
      setState({ ctx: {}, loading: false, partial: false });
      return;
    }
    let cancelled = false;
    setState({ ctx: {}, loading: true, partial: false });
    const listJobs = reads.map((r) => fetchResourceList<K8sResource>(r.kind as HybridSovereignKind, { namespace: r.namespace }));
    const tenantJob = readEntity ? fetchResource<K8sResource>('Entity', tenant!, TENANT_OBJECTS_NAMESPACE) : Promise.resolve(null);
    Promise.allSettled([...listJobs, tenantJob]).then((results) => {
      if (cancelled) return;
      const objects: Record<string, K8sResource[]> = {};
      reads.forEach((r, i) => {
        const res = results[i];
        if (res.status === 'fulfilled') objects[r.kind] = [...(objects[r.kind] ?? []), ...(res.value as K8sResource[])];
      });
      const last = results[results.length - 1];
      const tenantObj = last.status === 'fulfilled' ? (last.value as K8sResource | null) : null;
      setState({
        ctx: { objects, tenant: tenantObj },
        loading: false,
        partial: results.some((r) => r.status === 'rejected'),
      });
    });
    return () => {
      cancelled = true;
    };
  }, [kind, namespace, name, isOpen, readTenant]); // eslint-disable-line react-hooks/exhaustive-deps
  return state;
}

function EntryText({ section, entry }: { section: PreviewSection; entry: PreviewEntry }): React.ReactElement {
  const { t } = useTranslation();
  const routeFor = useRouteFor();
  const href = entry.kind && !entry.noLink ? routeFor(entry.kind, entry.name) : undefined;
  const label = entry.kind ? `${t(`kindsOne.${entry.kind}`, { defaultValue: entry.kind })} ` : '';
  const nameNode = href ? <Link to={href}>{entry.name}</Link> : <strong>{entry.name}</strong>;
  const fieldLabel = (key: string) => t(`deletePreview.fields.${key}`, { defaultValue: key });
  switch (section.key) {
    case 'waitVmEnvironments':
    case 'waitSiteNamespace':
      return (
        <>
          {label}
          {nameNode}
          {entry.detail ? <span className="sc-text-muted"> ({t('deletePreview.usingNamespaces', { names: entry.detail })})</span> : null}
        </>
      );
    case 'waitPlacements':
    case 'keptPlacements':
    case 'waitCircuits':
    case 'waitClusters':
      return (
        <>
          {label}
          {nameNode}
          {entry.detail ? (
            <span className="sc-text-muted">
              {' '}
              ({entry.detailKind ? `${t(`kindsOne.${entry.detailKind}`, { defaultValue: entry.detailKind })} ` : ''}
              {entry.detail})
            </span>
          ) : null}
        </>
      );
    case 'cloudProject':
      return entry.detail === 'CloudAWS' ? (
        <>{entry.name ? t('deletePreview.awsResourcesIn', { region: entry.name }) : t('deletePreview.awsResources')}</>
      ) : (
        <>
          {t(entry.detail === 'CloudGCP' ? 'deletePreview.gcpProject' : 'deletePreview.openstackProject')} <strong>{entry.name}</strong>
        </>
      );
    case 'subnet':
      return (
        <>
          <code>{entry.name}</code>
          {entry.detail ? ` ${t('deletePreview.onSite', { site: entry.detail })}` : ''}
        </>
      );
    case 'external':
      return <>{t(`deletePreview.external.${entry.detail}`, { name: entry.name, ...(entry.args ?? {}) })}</>;
    case 'tenantObjects':
    case 'waitProtected':
      return (
        <>
          <span className="sc-text-muted">{t('deletePreview.step', { n: entry.detail })} </span>
          {label}
          {nameNode}
        </>
      );
    case 'tenantNamespace':
      return <>{t('deletePreview.namespaceLast', { name: entry.name })}</>;
    case 'waitAssignments':
    case 'keptSecretEngines':
      return (
        <>
          {label}
          {nameNode}
        </>
      );
    case 'tenantRoles':
      return <>{t(`roles.${entry.name}`, { defaultValue: entry.name })}</>;
    case 'toolRoles':
    case 'accessLists':
      return (
        <>
          {label}
          {nameNode}
          {entry.detail ? <span className="sc-text-muted"> ({fieldLabel(entry.detail)})</span> : null}
        </>
      );
    case 'scopeGrants':
      return (
        <>
          {t('kindsOne.Rbac')} {nameNode}
          {entry.detail ? <span className="sc-text-muted"> ({fieldLabel(entry.detail)})</span> : null}
        </>
      );
    default:
      return <code>{entry.name}</code>;
  }
}

export interface DeleteConfirmModalProps {
  isOpen: boolean;
  kind: HybridSovereignKind;
  item: K8sResource | null;
  deleting: boolean;
  /** The failed delete, already in words */
  error?: string | null;
  onConfirm: () => void;
  onCancel: () => void;
}

/**
 * The one delete dialog of list and detail pages (GT-F8, OD41): it lists what
 * the delete takes with it, read from live data, and asks for the object's
 * name when the object owns realized resources.
 */
export function DeleteConfirmModal({ isOpen, kind, item, deleting, error, onConfirm, onCancel }: DeleteConfirmModalProps): React.ReactElement | null {
  const { t } = useTranslation();
  const platformReader = !!useOptionalScope()?.identity?.platform.reader;
  const { ctx, loading, partial } = usePreviewContext(kind, item, isOpen, platformReader);
  const [typed, setTyped] = useState('');
  useEffect(() => {
    if (isOpen) setTyped('');
  }, [isOpen, item?.metadata.name]);
  const preview = useMemo(() => (item ? deletePreview(kind, item, ctx) : { sections: [], realized: false }), [kind, item, ctx]);
  if (!isOpen || !item) return null;
  const name = item.metadata.name;
  const namespace = item.metadata.namespace ?? '';
  const kindLabel = t(`kindsOne.${kind}`, { defaultValue: kind });
  const mustType = preview.realized;
  const confirmOk = !mustType || typed.trim() === name;
  const canDelete = confirmOk && !deleting && !loading;

  return (
    <Modal variant={ModalVariant.small} isOpen={isOpen} onClose={() => (!deleting ? onCancel() : undefined)} aria-labelledby="sc-delete-title">
      <ModalHeader title={t('detail.deleteTitle', { kind: kindLabel, name })} labelId="sc-delete-title" titleIconVariant="warning" />
      <ModalBody>
        {error && (
          <Alert variant="danger" title={t('deletePreview.failed')} isInline className="sc-mb">
            {error}
          </Alert>
        )}
        <p className="sc-mb">{t('detail.deleteBody', { name, namespace })}</p>
        {loading ? (
          <Spinner size="md" aria-label={t('deletePreview.checking')} />
        ) : (
          <>
            {PREVIEW_GROUPS.map((g) => {
              const sections = preview.sections.filter((s) => s.group === g);
              if (!sections.length) return null;
              return (
                <div key={g} className="sc-mb">
                  {g !== 'access' && <strong>{t(`deletePreview.group.${g}`)}</strong>}
                  {sections.map((s) => (
                    <div key={s.key} className={g === 'access' ? 'sc-mb' : undefined}>
                      {g === 'access' ? (
                        <strong>{t(`deletePreview.section.${s.key}`)}</strong>
                      ) : (
                        <div className="sc-text-muted">{t(`deletePreview.section.${s.key}`)}</div>
                      )}
                      <ul className="sc-delete-preview__list">
                        {s.entries.map((e, i) => (
                          <li key={`${e.kind ?? ''}/${e.name}/${e.detail ?? ''}/${i}`}>
                            <EntryText section={s} entry={e} />
                          </li>
                        ))}
                      </ul>
                    </div>
                  ))}
                </div>
              );
            })}
            {preview.sections.length === 0 && <p className="sc-text-muted sc-mb">{t('deletePreview.nothingElse')}</p>}
            {kind === 'Rbac' && !platformReader && <p className="sc-text-muted sc-mb">{t('deletePreview.tenantRolesHidden')}</p>}
            {partial && <p className="sc-text-muted sc-mb">{t('deletePreview.partial')}</p>}
          </>
        )}
        {mustType && (
          <Form onSubmit={(e) => { e.preventDefault(); if (canDelete) onConfirm(); }}>
            <FormGroup label={t('deletePreview.typeName', { name })} fieldId="sc-delete-confirm-name">
              <TextInput
                id="sc-delete-confirm-name"
                value={typed}
                onChange={(_e, v) => setTyped(v)}
                autoComplete="off"
                aria-describedby="sc-delete-confirm-help"
                validated={typed && !confirmOk ? 'error' : 'default'}
              />
              <p id="sc-delete-confirm-help" className="sc-text-muted">
                {t('deletePreview.typeNameHelp')}
              </p>
            </FormGroup>
          </Form>
        )}
      </ModalBody>
      <ModalFooter>
        <Button key="confirm" variant="danger" isDisabled={!canDelete} isLoading={deleting} onClick={onConfirm}>
          {deleting ? t('detail.deleting') : t('common.delete')}
        </Button>
        <Button key="cancel" variant="link" isDisabled={deleting} onClick={onCancel}>
          {t('common.cancel')}
        </Button>
      </ModalFooter>
    </Modal>
  );
}
