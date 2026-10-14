import React, { useCallback, useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import {
  Alert,
  Button,
  Label,
  Modal,
  ModalBody,
  ModalFooter,
  ModalHeader,
  ModalVariant,
  Spinner,
  Tooltip,
} from '@patternfly/react-core';
import { ExternalLinkAltIcon, TrashIcon } from '@patternfly/react-icons';
import { Table, Thead, Tr, Th, Tbody, Td, ExpandableRowContent } from '@patternfly/react-table';
import { useTranslation } from '../i18n';
import type { HybridSovereignKind, K8sResource } from '../types/crds';
import { KindIcon } from '../icons/kindIcons';
import { StatusBadge } from './StatusBadge';
import { HealthStrip } from './StatTile';
import type { StatVariant } from './StatTile';
import { HealthDonut } from './HealthDonut';
import { normalizeHealth } from './StatusBadge';
import {
  CellMarker,
  getExpandRows,
  getListColumns,
  ListLinkMode,
  resourceSearchBlob,
} from '../list/listColumns';
import { deleteDashboardResource } from '../hooks/k8s';

export interface ResourceListTableProps {
  kind: HybridSovereignKind;
  items: K8sResource[];
  loading: boolean;
  error: Error | null;
  detailHref: (item: K8sResource) => string;
  linkMode?: ListLinkMode;
  /** Admin cluster-wide lists show namespace column */
  showNamespace?: boolean;
  emptyMessage?: string;
  /** Pre-filtered items; when set, skip internal search blob (caller already filtered) */
  searchQuery?: string;
  /** Show a Delete action per row (console + standalone list pages) */
  enableDelete?: boolean;
  /** Called after a successful delete so the parent can refresh */
  onDeleted?: () => void;
}

function isMarker(v: unknown): v is CellMarker {
  return !!v && typeof v === 'object' && '__kind' in (v as object);
}

function CopyMono({ text }: { text: string }): React.ReactElement {
  const [copied, setCopied] = useState(false);
  if (!text) return <>—</>;
  return (
    <Tooltip content={copied ? 'Copied' : 'Click to copy'}>
      <button
        type="button"
        className="sc-mono-copy"
        onClick={() => {
          void navigator.clipboard?.writeText(text);
          setCopied(true);
          window.setTimeout(() => setCopied(false), 1200);
        }}
      >
        {text}
      </button>
    </Tooltip>
  );
}

function renderMarker(node: CellMarker): React.ReactNode {
  switch (node.__kind) {
    case 'externalLink':
      if (!node.href) return '—';
      return (
        <a
          className="sc-ext-link"
          href={node.href}
          target="_blank"
          rel="noopener noreferrer"
          onClick={(e) => e.stopPropagation()}
        >
          <span className="sc-ext-link__text">{node.href.replace(/^https?:\/\//, '')}</span>
          <ExternalLinkAltIcon />
        </a>
      );
    case 'truncate': {
      const max = node.max ?? 48;
      const text = node.text || '';
      if (!text) return '—';
      const short = text.length > max ? `${text.slice(0, max)}…` : text;
      return (
        <Tooltip content={text}>
          <span className="sc-truncate">{short}</span>
        </Tooltip>
      );
    }
    case 'chips':
      if (!node.items.length) return '—';
      return (
        <span className="sc-chip-row">
          {node.items.map((c) => (
            <Label key={c.label} color={(c.color as 'blue') || 'grey'} isCompact>
              {c.label}
            </Label>
          ))}
          {node.overflow && node.overflow > 0 ? (
            <Label color="grey" isCompact>
              +{node.overflow}
            </Label>
          ) : null}
        </span>
      );
    case 'chip':
      if (!node.label || node.label === '—') return '—';
      return (
        <Label color={(node.color as 'blue') || 'grey'} isCompact>
          {node.label}
        </Label>
      );
    case 'countChip':
      return (
        <Tooltip content={node.tip || `${node.count} ${node.label || ''}`}>
          <Label color={node.count > 0 ? 'blue' : 'grey'} isCompact>
            {node.count} {node.label || ''}
          </Label>
        </Tooltip>
      );
    case 'mono':
      return <CopyMono text={node.text} />;
    case 'relativeTime':
      return (
        <Tooltip content={node.title || node.label}>
          <time className="sc-rel-time" dateTime={node.title || undefined}>
            {node.label}
          </time>
        </Tooltip>
      );
    default:
      return '—';
  }
}

export function ResourceListTable({
  kind,
  items,
  loading,
  error,
  detailHref,
  linkMode = 'router',
  showNamespace = false,
  emptyMessage,
  enableDelete = false,
  onDeleted,
}: ResourceListTableProps): React.ReactElement {
  const { t } = useTranslation();
  const columns = useMemo(() => getListColumns(kind, { showNamespace }), [kind, showNamespace]);
  const [sortId, setSortId] = useState<string>('name');
  const [sortDir, setSortDir] = useState<'asc' | 'desc'>('asc');
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});
  const [pendingDelete, setPendingDelete] = useState<K8sResource | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [deleteError, setDeleteError] = useState<string | null>(null);

  const colSpan = columns.length + 1 + (enableDelete ? 1 : 0);

  const confirmDelete = async () => {
    if (!pendingDelete) return;
    setDeleting(true);
    setDeleteError(null);
    try {
      await deleteDashboardResource(
        kind,
        pendingDelete.metadata.name,
        pendingDelete.metadata.namespace,
      );
      setPendingDelete(null);
      onDeleted?.();
    } catch (err) {
      setDeleteError(err instanceof Error ? err.message : 'Delete failed');
    } finally {
      setDeleting(false);
    }
  };

  const healthCounts = useMemo(() => {
    const counts = { ready: 0, failed: 0, pending: 0, reconciling: 0 };
    for (const item of items) {
      const h = normalizeHealth(item.status?.ready, item.status?.status);
      if (h in counts) counts[h as keyof typeof counts] += 1;
    }
    return counts;
  }, [items]);

  const healthTiles = useMemo(() => {
    const total = Math.max(
      1,
      healthCounts.ready + healthCounts.failed + healthCounts.pending + healthCounts.reconciling,
    );
    const mk = (key: StatVariant, value: number) => ({
      key,
      label: t(`status.${key}`),
      value,
      variant: key,
      share: Math.round((value / total) * 100),
    });
    return [
      mk('ready', healthCounts.ready),
      mk('reconciling', healthCounts.reconciling),
      mk('pending', healthCounts.pending),
      mk('failed', healthCounts.failed),
    ];
  }, [healthCounts, t]);

  const sorted = useMemo(() => {
    const col = columns.find((c) => c.id === sortId) ?? columns[0];
    const dir = sortDir === 'asc' ? 1 : -1;
    return [...items].sort((a, b) => {
      const av = col.getSortValue(a);
      const bv = col.getSortValue(b);
      if (typeof av === 'number' && typeof bv === 'number') return (av - bv) * dir;
      return String(av).localeCompare(String(bv), undefined, { sensitivity: 'base' }) * dir;
    });
  }, [items, columns, sortId, sortDir]);

  const onSort = useCallback(
    (colId: string) => {
      if (sortId === colId) setSortDir((d) => (d === 'asc' ? 'desc' : 'asc'));
      else {
        setSortId(colId);
        setSortDir('asc');
      }
    },
    [sortId],
  );

  const toggleExpand = (key: string) => {
    setExpanded((prev) => ({ ...prev, [key]: !prev[key] }));
  };

  const ctx = useMemo(
    () => ({ kind, detailHref, showNamespace, t, linkMode }),
    [kind, detailHref, showNamespace, t, linkMode],
  );

  if (loading && items.length === 0) {
    return <Spinner aria-label={t('pages.loadingKind', { kind })} />;
  }

  return (
    <>
      {error && (
        <Alert variant="warning" title={t('common.k8sUnavailable')} isInline className="sc-mb">
          {error.message}. {t('common.k8sUnavailableHint')}
        </Alert>
      )}

      {items.length > 0 && (
        <div className="sc-list-summary">
          <div className="sc-list-summary__viz">
            <HealthDonut
              size="sm"
              ready={healthCounts.ready}
              failed={healthCounts.failed}
              pending={healthCounts.pending}
              reconciling={healthCounts.reconciling}
              title={t('common.status')}
            />
          </div>
          <div className="sc-list-summary__kpis">
            <HealthStrip tiles={healthTiles} />
            <span className="sc-list-summary__count">
              {t('list.showing', { count: items.length, defaultValue: `${items.length} resources` })}
            </span>
          </div>
        </div>
      )}

      <div className="sc-table-wrap">
        <Table aria-label={`${kind} table`} variant="compact" className="sc-resource-list">
          <Thead>
            <Tr>
              <Th screenReaderText={t('common.details')} />
              {columns.map((col) => (
                <Th
                  key={col.id}
                  sort={
                    col.sortable
                      ? {
                          sortBy: {
                            index: columns.findIndex((c) => c.id === sortId),
                            direction: sortDir,
                          },
                          onSort: (_e, _idx, direction) => {
                            setSortId(col.id);
                            setSortDir(direction === 'desc' ? 'desc' : 'asc');
                          },
                          columnIndex: columns.findIndex((c) => c.id === col.id),
                        }
                      : undefined
                  }
                  onClick={col.sortable ? () => onSort(col.id) : undefined}
                  className={col.sortable ? 'sc-th-sortable' : undefined}
                >
                  {t(col.labelKey, { defaultValue: col.id })}
                </Th>
              ))}
              {enableDelete ? <Th screenReaderText={t('common.actions', { defaultValue: 'Actions' })} /> : null}
            </Tr>
          </Thead>
          {sorted.length === 0 ? (
            <Tbody>
              <Tr>
                <Td colSpan={colSpan}>
                  {emptyMessage ?? t('pages.noMatch', { kind })}
                </Td>
              </Tr>
            </Tbody>
          ) : (
            sorted.map((item, rowIndex) => {
              const key = `${item.metadata.namespace ?? ''}/${item.metadata.name}`;
              const isOpen = !!expanded[key];
              const href = detailHref(item);
              const expandRows = getExpandRows(item);
              return (
                <Tbody key={key} isExpanded={isOpen}>
                  <Tr>
                    <Td
                      expand={{
                        rowIndex,
                        isExpanded: isOpen,
                        onToggle: () => toggleExpand(key),
                        expandId: `expand-${key}`,
                      }}
                    />
                    {columns.map((col) => {
                      if (col.id === 'name') {
                        const nameNode =
                          linkMode === 'anchor' ? (
                            <a className="sc-resource-link" href={href}>
                              <KindIcon kind={kind} size="sm" />
                              {item.metadata.name}
                            </a>
                          ) : (
                            <Link className="sc-resource-link" to={href}>
                              <KindIcon kind={kind} size="sm" />
                              {item.metadata.name}
                            </Link>
                          );
                        return (
                          <Td key={col.id} dataLabel={t(col.labelKey)}>
                            {nameNode}
                          </Td>
                        );
                      }
                      if (col.id === 'status') {
                        return (
                          <Td key={col.id} dataLabel={t(col.labelKey)}>
                            <StatusBadge
                              status={item.status?.status}
                              ready={item.status?.ready}
                              message={item.status?.message}
                              lastTransition={item.status?.lastReconciledAt}
                            />
                          </Td>
                        );
                      }
                      const raw = col.render(item, ctx);
                      return (
                        <Td key={col.id} dataLabel={t(col.labelKey)}>
                          {isMarker(raw) ? renderMarker(raw) : raw}
                        </Td>
                      );
                    })}
                    {enableDelete ? (
                      <Td dataLabel={t('common.actions', { defaultValue: 'Actions' })} isActionCell>
                        <Button
                          variant="plain"
                          icon={<TrashIcon />}
                          aria-label={`Delete ${item.metadata.name}`}
                          onClick={(e) => {
                            e.preventDefault();
                            e.stopPropagation();
                            setDeleteError(null);
                            setPendingDelete(item);
                          }}
                        />
                      </Td>
                    ) : null}
                  </Tr>
                  <Tr isExpanded={isOpen}>
                    <Td colSpan={colSpan} noPadding>
                      {isOpen ? (
                        <ExpandableRowContent>
                          <div className="sc-list-expand">
                            <div className="sc-list-expand__header">
                              <StatusBadge
                                status={item.status?.status}
                                ready={item.status?.ready}
                                message={item.status?.message}
                                lastTransition={item.status?.lastReconciledAt}
                              />
                              <span className="sc-list-expand__title">{item.metadata.name}</span>
                            </div>
                            {expandRows.length === 0 ? (
                              <span className="sc-muted">{t('list.noExtraDetails')}</span>
                            ) : (
                              <div className="sc-list-expand__grid">
                                {expandRows.map((r) => (
                                  <div key={r.label} className="sc-list-expand__card">
                                    <div className="sc-list-expand__card-label">{r.label}</div>
                                    <div className="sc-list-expand__card-value">{r.value}</div>
                                  </div>
                                ))}
                              </div>
                            )}
                            <div className="sc-list-expand__actions">
                              {linkMode === 'anchor' ? (
                                <a className="sc-resource-link" href={href}>
                                  {t('common.view')} →
                                </a>
                              ) : (
                                <Link className="sc-resource-link" to={href}>
                                  {t('common.view')} →
                                </Link>
                              )}
                              {enableDelete ? (
                                <Button
                                  variant="link"
                                  isDanger
                                  icon={<TrashIcon />}
                                  onClick={() => {
                                    setDeleteError(null);
                                    setPendingDelete(item);
                                  }}
                                >
                                  {t('common.delete', { defaultValue: 'Delete' })}
                                </Button>
                              ) : null}
                            </div>
                          </div>
                        </ExpandableRowContent>
                      ) : null}
                    </Td>
                  </Tr>
                </Tbody>
              );
            })
          )}
        </Table>
      </div>

      <Modal
        variant={ModalVariant.small}
        isOpen={!!pendingDelete}
        onClose={() => (!deleting ? setPendingDelete(null) : undefined)}
      >
        <ModalHeader title={`Delete ${kind}?`} />
        <ModalBody>
          {deleteError && (
            <Alert variant="danger" title="Delete failed" isInline className="sc-mb">
              {deleteError}
            </Alert>
          )}
          This will permanently delete <strong>{pendingDelete?.metadata.name}</strong>
          {pendingDelete?.metadata.namespace
            ? <> from namespace <strong>{pendingDelete.metadata.namespace}</strong></>
            : null}
          .
        </ModalBody>
        <ModalFooter>
          <Button key="confirm" variant="danger" isDisabled={deleting} onClick={() => void confirmDelete()}>
            {deleting ? 'Deleting…' : 'Delete'}
          </Button>
          <Button key="cancel" variant="link" isDisabled={deleting} onClick={() => setPendingDelete(null)}>
            Cancel
          </Button>
        </ModalFooter>
      </Modal>
    </>
  );
}

/** Filter helper for list pages — searches name, namespace, status, and kind columns. */
export function filterResourcesByQuery(
  items: K8sResource[],
  kind: HybridSovereignKind,
  search: string,
  showNamespace: boolean,
): K8sResource[] {
  const q = search.trim().toLowerCase();
  if (!q) return items;
  const columns = getListColumns(kind, { showNamespace });
  return items.filter((item) => resourceSearchBlob(item, columns).includes(q));
}
