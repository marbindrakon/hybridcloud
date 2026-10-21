import React from 'react';
import { useNavigate } from 'react-router-dom';
import { Alert } from '@patternfly/react-core';
import { useTranslation } from '../i18n';
import { ResourceDetail } from '../components/ResourceDetail';
import { useDocumentTitle } from '../nav/useNav';
import { usePageEntry, useObjectName } from './usePageEntry';

/** The detail page of an object of a registry entry, in the current tenant (or the platform). */
export function ScopedDetailPage({ entryId }: { entryId: string }): React.ReactElement {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const name = useObjectName();
  const { host, entry, kind, namespace, listHref } = usePageEntry(entryId);
  useDocumentTitle(entryId, name);
  if (!entry || !kind || !name) return <Alert variant="danger" isInline title={t('common.pageNotFound')} />;
  if (!namespace) return <Alert variant="info" isInline title={t('scope.choose')} />;
  return (
    <ResourceDetail
      key={`${namespace}/${name}`}
      kind={kind}
      name={name}
      namespace={namespace}
      parentTitle={t(entry.labelKey)}
      parentPath={listHref}
      onBack={() => navigate(listHref)}
      onDeleted={() => navigate(listHref)}
      audience={host === 'admin-dashboard' || host === 'admin-console' ? 'admin' : 'tenant'}
    />
  );
}
