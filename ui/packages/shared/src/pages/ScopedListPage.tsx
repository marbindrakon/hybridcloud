import React, { useMemo, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { Alert } from '@patternfly/react-core';
import { PlusCircleIcon } from '@patternfly/react-icons';
import { useTranslation } from '../i18n';
import { PageHeader } from '../components/PageHeader';
import { FilterToolbar, type StatusFilter } from '../components/FilterToolbar';
import { normalizeHealth } from '../components/StatusBadge';
import { ResourceListTable, filterResourcesByQuery } from '../components/ResourceListTable';
import { GatedButton } from '../access/GatedButton';
import { useScopedResourceList } from '../hooks/scopedList';
import { classifyError } from '../hooks/errors';
import { useSharedWithYou } from '../access/sharedWithYou';
import { useBreadcrumbs, useDocumentTitle, useNav } from '../nav/useNav';
import type { K8sResource } from '../types';
import { usePageEntry } from './usePageEntry';

export interface ScopedListPageProps {
  entryId: string;
  /** Extra content under the header (for example tabs). */
  children?: React.ReactNode;
  hideHeader?: boolean;
}

/**
 * The list page of a registry entry in the current scope. In All tenants
 * scope it is read-only and shows the namespace column; detail links open the
 * object in its own tenant.
 */
export function ScopedListPage({ entryId, children, hideHeader = false }: ScopedListPageProps): React.ReactElement {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const nav = useNav();
  const { entry, kind, namespace, allTenants, detailHref } = usePageEntry(entryId);
  const [search, setSearch] = useState('');
  const [statusFilter, setStatusFilter] = useState<StatusFilter>('all');
  const crumbs = useBreadcrumbs(entryId);
  useDocumentTitle(entryId);
  // Refresh every 5 s while a row is in flight (Creating, Deleting, ...), else every 30 s (Q10).
  const list = useScopedResourceList<K8sResource>(kind ?? 'Entity', { namespace, enabled: !!kind, pollWhileInFlight: true });
  // A list the user may not read can still hold objects shared with them by name (NC-13).
  const forbidden = !!list.error && classifyError(list.error) === 'forbidden';
  const shared = useSharedWithYou(kind ?? 'Entity', namespace, forbidden && !allTenants && !!kind);
  const showShared = forbidden && shared.items.length > 0;

  const filtered = useMemo(() => {
    if (!kind) return [];
    return filterResourcesByQuery(list.items, kind, search, allTenants).filter((item) =>
      statusFilter === 'all' ? true : normalizeHealth(item.status?.ready, item.status?.status) === statusFilter,
    );
  }, [list.items, kind, search, statusFilter, allTenants]);

  if (!entry || !kind) return <Alert variant="danger" isInline title={t('common.pageNotFound')} />;
  const title = t(entry.labelKey);
  const createHref = entry.createForm ? nav.createPathFor(entry.createForm) : undefined;

  return (
    <>
      {!hideHeader && (
        <PageHeader
          title={title}
          breadcrumbs={crumbs}
          actions={
            createHref && !allTenants ? (
              <GatedButton kind={kind} verb="create" variant="primary" icon={<PlusCircleIcon />} onClick={() => navigate(createHref)}>
                {t('common.create')}
              </GatedButton>
            ) : undefined
          }
        />
      )}
      {allTenants && (
        <Alert variant="info" isInline isPlain title={t('access.readOnlyAllTenants')} className="sc-mb" />
      )}
      {children}
      {showShared && (
        <Alert variant="info" isInline isPlain title={t('access.sharedWithYou', { count: shared.items.length })} className="sc-mb" />
      )}
      <FilterToolbar
        search={search}
        onSearchChange={setSearch}
        statusFilter={statusFilter}
        onStatusFilterChange={setStatusFilter}
        onRefresh={list.refresh}
        searchPlaceholder={t('common.filterResources')}
      />
      <ResourceListTable
        kind={kind}
        items={showShared ? shared.items : filtered}
        loading={list.loading || shared.loading}
        error={showShared ? null : list.error}
        showNamespace={allTenants}
        linkMode="router"
        detailHref={detailHref}
        enableDelete={!allTenants && !showShared}
        onDeleted={list.refresh}
        onRetry={list.refresh}
        refreshError={showShared ? null : list.refreshError}
      />
    </>
  );
}
