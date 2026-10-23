import React from 'react';
import { useLocation, useNavigate } from 'react-router-dom';
import { Alert } from '@patternfly/react-core';
import { useTranslation } from '../i18n';
import { ResourceDetail } from '../components/ResourceDetail';
import { useDocumentTitle, useNav } from '../nav/useNav';
import { usePageEntry, useObjectName } from './usePageEntry';
import { kindTerm } from '../i18n/vocabulary';

/** The detail page of an object of a registry entry, in the current tenant (or the platform). */
export function ScopedDetailPage({ entryId }: { entryId: string }): React.ReactElement {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const name = useObjectName();
  const nav = useNav();
  const from = new URLSearchParams(useLocation().search).get('from') ?? undefined;
  const { host, entry, kind, namespace, listHref } = usePageEntry(entryId);
  useDocumentTitle(entryId, name);
  if (!entry || !kind || !name) return <Alert variant="danger" isInline title={t('common.pageNotFound')} />;
  if (!namespace) return <Alert variant="info" isInline title={t('scope.choose')} />;
  // A network placement opened from its network or environment leads back there (N5, OD25).
  const parent = (() => {
    const [fromKind, fromName] = (from ?? '').split('/');
    const fromHref = fromKind && fromName ? nav.routeFor(fromKind, fromName) : undefined;
    return fromHref
      ? { title: `${kindTerm(t, fromKind)} ${fromName}`, path: fromHref }
      : { title: t(entry.labelKey), path: listHref };
  })();
  return (
    <ResourceDetail
      key={`${namespace}/${name}`}
      kind={kind}
      name={name}
      namespace={namespace}
      parentTitle={parent.title}
      parentPath={parent.path}
      onBack={() => navigate(parent.path)}
      onDeleted={() => navigate(parent.path)}
      audience={host === 'admin-dashboard' || host === 'admin-console' ? 'admin' : 'tenant'}
    />
  );
}
