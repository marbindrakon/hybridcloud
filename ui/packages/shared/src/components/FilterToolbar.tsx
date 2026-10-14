import React from 'react';
import { Button, SearchInput } from '@patternfly/react-core';
import { SyncAltIcon } from '@patternfly/react-icons';
import { useTranslation } from '../i18n';

export type StatusFilter = 'all' | 'ready' | 'failed' | 'pending' | 'reconciling';

export interface FilterToolbarProps {
  search: string;
  onSearchChange: (value: string) => void;
  statusFilter: StatusFilter;
  onStatusFilterChange: (value: StatusFilter) => void;
  /** Manual refresh — always shown; status tabs never trigger API calls */
  onRefresh: () => void;
  searchPlaceholder?: string;
}

const FILTERS: StatusFilter[] = ['all', 'ready', 'failed', 'pending', 'reconciling'];

export function FilterToolbar({
  search,
  onSearchChange,
  statusFilter,
  onStatusFilterChange,
  onRefresh,
  searchPlaceholder,
}: FilterToolbarProps): React.ReactElement {
  const { t } = useTranslation();
  return (
    <div className="sc-filter-bar" role="toolbar" aria-label={t('common.statusFilter')}>
      <div className="sc-filter-bar__search">
        <SearchInput
          aria-label={t('common.filterResources')}
          placeholder={searchPlaceholder ?? t('common.filterByName')}
          value={search}
          onChange={(_e, v) => onSearchChange(v)}
          onClear={() => onSearchChange('')}
        />
      </div>
      <div className="sc-filter-bar__chips" role="group" aria-label={t('common.statusFilter')}>
        {FILTERS.map((f) => {
          const selected = statusFilter === f;
          return (
            <button
              key={f}
              type="button"
              id={`status-${f}`}
              className={`sc-status-chip sc-status-chip--${f}${selected ? ' sc-status-chip--selected' : ''}`}
              aria-pressed={selected}
              onClick={() => onStatusFilterChange(f)}
            >
              {f !== 'all' && <span className="sc-status-chip__dot" aria-hidden />}
              <span className="sc-status-chip__label">{t(`status.${f}`)}</span>
            </button>
          );
        })}
      </div>
      <div className="sc-filter-bar__actions">
        <Button
          variant="secondary"
          className="sc-filter-bar__refresh"
          icon={<SyncAltIcon />}
          onClick={onRefresh}
        >
          {t('common.refresh')}
        </Button>
      </div>
    </div>
  );
}
