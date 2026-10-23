import React from 'react';
import { useK8sResourceList } from '../hooks/k8s';
import { useTranslation } from '../i18n';
import type { CloudInfrastructure } from '../types';
import { siteCatalogueLabel, sitesForTenant } from '../forms/entityFields';
import { CLOUD_INFRASTRUCTURE_NS } from './CloudInfrastructureSelect';
import { RbacMultiSelect } from './RbacMultiSelect';

export interface AllowedSitesSelectProps {
  id: string;
  /** The tenant (Entity name); sites restricted to other tenants are not offered. */
  tenant: string;
  /** Site names (Entity.spec.allowedSites); empty means every site that allows the tenant. */
  value: string[];
  onChange: (next: string[]) => void;
  label?: string;
  error?: string;
  isDisabled?: boolean;
}

/**
 * Entity.spec.allowedSites: a multi-select of the platform's sites (the site
 * catalogue, CloudInfrastructure objects) that allow this tenant. Nothing chosen
 * means every one of them; a choice narrows the list. Platform pages only.
 */
export function AllowedSitesSelect({ id, tenant, value, onChange, label, error, isDisabled }: AllowedSitesSelectProps): React.ReactElement {
  const { t } = useTranslation();
  const list = useK8sResourceList<CloudInfrastructure>('CloudInfrastructure', { namespace: CLOUD_INFRASTRUCTURE_NS });
  const { offered, notListing } = sitesForTenant(list.items, tenant);
  const offeredNames = new Set(offered.map((s) => s.metadata.name));
  const options = [
    ...offered.map((s) => ({ value: s.metadata.name, label: siteCatalogueLabel(s) })),
    // A stored choice the catalogue no longer offers stays visible so it can be removed.
    ...value.filter((v) => !offeredNames.has(v)).map((v) => ({ value: v, label: t('entityForm.siteNotOffered', { name: v }) })),
  ];
  const help = list.error
    ? t('entityForm.sitesUnavailable')
    : [
        value.length ? t('entityForm.allowedSitesSome', { count: value.length }) : t('entityForm.allowedSitesAll', { count: offered.length }),
        notListing.length ? t('entityForm.sitesNotListing', { names: notListing.join(', ') }) : '',
      ]
        .filter(Boolean)
        .join(' ');
  return (
    <RbacMultiSelect
      id={id}
      label={label ?? t('entityForm.allowedSites')}
      value={value}
      onChange={onChange}
      options={options}
      placeholder={t('entityForm.allowedSitesPlaceholder')}
      emptyText={list.loading ? t('common.loading') : t('entityForm.noSites')}
      helperText={help}
      error={error}
      isDisabled={isDisabled || !!list.error}
    />
  );
}
