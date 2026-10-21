import React, { useMemo, useState } from 'react';
import {
  Divider,
  MenuSearch,
  MenuSearchInput,
  MenuToggle,
  SearchInput,
  Select,
  SelectGroup,
  SelectList,
  SelectOption,
  Tooltip,
} from '@patternfly/react-core';
import { useTranslation } from '../i18n';
import { recentTenants, useOptionalScope } from './ScopeProvider';
import { tenantLabel } from './resolveIdentity';
import type { TenantRef } from './types';

export interface TenantScopeSelectorProps {
  /** Show "Tenant namespace: entity-<e>" under the control (console label, NC-31). */
  showNamespace?: boolean;
  /** Offer the platform scope ("Platform") as well (admin hosts). */
  includePlatform?: boolean;
  className?: string;
}

const SEARCH_THRESHOLD = 7;

/**
 * "Tenant: Acme Corporation" in the masthead (design 5.3). One tenant is shown
 * as text with its id on hover; several give a searchable picker with Recent
 * and All; platform readers get "All tenants" first.
 */
export function TenantScopeSelector({
  showNamespace = false,
  includePlatform = false,
  className,
}: TenantScopeSelectorProps): React.ReactElement | null {
  const { t } = useTranslation();
  const ctx = useOptionalScope();
  const [open, setOpen] = useState(false);
  const [filter, setFilter] = useState('');

  const tenants = ctx?.tenants ?? [];
  const scope = ctx?.scope;
  const identity = ctx?.identity;
  const reader = !!identity?.platform.reader;
  const admin = !!identity?.platform.admin;

  const current: TenantRef | undefined = scope?.kind === 'tenant' ? scope.tenant : undefined;
  const currentLabel =
    scope?.kind === 'all'
      ? t('scope.allTenants')
      : scope?.kind === 'global'
        ? t('scope.platform')
        : current
          ? tenantLabel(current)
          : t('scope.choose');

  const recent = useMemo(() => recentTenants(tenants).filter((r) => r.name !== current?.name).slice(0, 3), [tenants, current]);
  const shown = useMemo(() => {
    const f = filter.trim().toLowerCase();
    return f
      ? tenants.filter((x) => x.name.includes(f) || (x.displayName ?? '').toLowerCase().includes(f))
      : tenants;
  }, [tenants, filter]);

  if (!ctx || ctx.loading || !identity) return null;
  if (!tenants.length && !reader && !(includePlatform && admin)) return null;

  const single = tenants.length === 1 && !reader && !(includePlatform && admin);
  const namespaceLine =
    showNamespace && current ? (
      <span className="sc-scope__namespace">
        {t('scope.tenantNamespace')}: {current.namespace}
      </span>
    ) : null;

  if (single) {
    const only = tenants[0];
    return (
      <span className={`sc-scope ${className ?? ''}`.trim()}>
        <span className="sc-scope__label">{t('scope.tenant')}:</span>{' '}
        <Tooltip content={only.name}>
          <strong className="sc-scope__value" tabIndex={0}>
            {tenantLabel(only)}
          </strong>
        </Tooltip>
        {namespaceLine}
      </span>
    );
  }

  const onSelect = (_e: React.MouseEvent | undefined, value: string | number | undefined) => {
    setOpen(false);
    setFilter('');
    if (typeof value === 'string') ctx.select(value);
  };

  return (
    <span className={`sc-scope ${className ?? ''}`.trim()}>
      <span className="sc-scope__label" id="sc-scope-label">
        {t('scope.tenant')}
      </span>
      <Select
        isOpen={open}
        selected={scope?.kind === 'tenant' ? current?.name : scope?.kind === 'all' ? 'all' : scope?.kind === 'global' ? 'platform' : undefined}
        onSelect={onSelect}
        onOpenChange={(o) => setOpen(o)}
        toggle={(ref) => (
          <MenuToggle
            ref={ref}
            onClick={() => setOpen((o) => !o)}
            isExpanded={open}
            aria-labelledby="sc-scope-label"
            title={current?.name}
          >
            {currentLabel}
          </MenuToggle>
        )}
        shouldFocusToggleOnSelect
      >
        {tenants.length > SEARCH_THRESHOLD && (
          <>
            <MenuSearch>
              <MenuSearchInput>
                <SearchInput
                  value={filter}
                  aria-label={t('scope.search')}
                  placeholder={t('scope.search')}
                  onChange={(_e, v) => setFilter(v)}
                  onClear={() => setFilter('')}
                />
              </MenuSearchInput>
            </MenuSearch>
            <Divider />
          </>
        )}
        {(includePlatform && admin) || reader ? (
          <SelectList>
            {includePlatform && admin && <SelectOption value="platform">{t('scope.platform')}</SelectOption>}
            {reader && <SelectOption value="all">{t('scope.allTenants')}</SelectOption>}
          </SelectList>
        ) : null}
        {recent.length > 0 && !filter && (
          <SelectGroup label={t('scope.recent')}>
            <SelectList>
              {recent.map((r) => (
                <SelectOption key={`recent-${r.name}`} value={r.name} description={r.displayName ? r.name : undefined}>
                  {tenantLabel(r)}
                </SelectOption>
              ))}
            </SelectList>
          </SelectGroup>
        )}
        <SelectGroup label={t('scope.all')}>
          <SelectList aria-label={t('scope.all')}>
            {shown.map((x) => (
              <SelectOption key={x.name} value={x.name} description={x.displayName ? x.name : undefined}>
                {tenantLabel(x)}
              </SelectOption>
            ))}
            {!shown.length && (
              <SelectOption isDisabled value="__none__">
                {t('scope.noMatch')}
              </SelectOption>
            )}
          </SelectList>
        </SelectGroup>
      </Select>
      {namespaceLine}
    </span>
  );
}
