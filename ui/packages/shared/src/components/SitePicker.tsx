import React, { useEffect } from 'react';
import { FormGroup, FormHelperText, FormSelect, FormSelectOption, HelperText, HelperTextItem, Label, TextInput } from '@patternfly/react-core';
import { useTranslation } from '../i18n';
import { useTenantProfile } from '../hooks/tenantProfile';
import type { TenantProfileSite, TenantSiteType } from '../types';
import { CloudInfrastructureSelect } from './CloudInfrastructureSelect';

export interface SitePickerProps {
  id?: string;
  label?: string;
  /** Site name (written as spec.cloudRef.name) */
  value: string;
  onChange: (next: string) => void;
  /** Site types this environment can live on */
  types: TenantSiteType[];
  /** Tenant namespace (entity-<tenant>) whose profile lists the sites */
  namespace: string;
  /** Platform pages list the sites themselves (CloudInfrastructure); tenant pages use the profile. */
  audience?: 'admin' | 'tenant';
  isRequired?: boolean;
  /** Offer an explicit "platform default" choice (optional references) */
  allowEmpty?: boolean;
  emptyLabel?: string;
  helperText?: string;
  /** Called with the profile entry of the chosen site (tenant pages), e.g. for storage classes. */
  onSite?: (site: TenantProfileSite | undefined) => void;
  isDisabled?: boolean;
}

const CAPABILITY_KEYS: Record<string, string> = {
  Virtualization: 'sites.capVirtualization',
  HybridNetworking: 'sites.capNetworking',
  DNS: 'sites.capDns',
};

export function siteOptionLabel(site: TenantProfileSite, t: (k: string, o?: Record<string, unknown>) => string): string {
  const where = site.location || site.region;
  const parts = [site.displayName || site.name, where ? `(${where})` : ''].filter(Boolean).join(' ');
  const extra = [
    site.billable ? t('sites.billable') : '',
    site.state !== 'Available' ? t('sites.unavailable') : '',
  ].filter(Boolean);
  return extra.length ? `${parts}: ${extra.join(', ')}` : parts;
}

/**
 * The site of an environment. Tenant pages offer the sites of the tenant's
 * profile (display name, location, Billable), preselected when there is only
 * one; without a profile they accept a typed site name. Platform pages pick
 * from the platform's sites.
 */
export function SitePicker({
  id = 'site',
  label,
  value,
  onChange,
  types,
  namespace,
  audience = 'tenant',
  isRequired,
  allowEmpty,
  emptyLabel,
  helperText,
  onSite,
  isDisabled,
}: SitePickerProps): React.ReactElement {
  const { t } = useTranslation();
  const tenantProfile = useTenantProfile(namespace, { enabled: audience === 'tenant' });
  const sites = tenantProfile.sites(types);
  const available = sites.filter((s) => s.state === 'Available');
  const fieldLabel = label ?? t('fields.site');
  const chosen = sites.find((s) => s.name === value);

  // Preselect the only site (never over a value the user or the object already has).
  useEffect(() => {
    if (audience !== 'tenant' || allowEmpty || value || available.length !== 1) return;
    onChange(available[0].name);
  }, [audience, allowEmpty, value, available.length, available[0]?.name]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    onSite?.(chosen);
  }, [chosen?.name]); // eslint-disable-line react-hooks/exhaustive-deps

  if (audience === 'admin') {
    const entityName = namespace.startsWith('entity-') ? namespace.slice('entity-'.length) : undefined;
    return (
      <CloudInfrastructureSelect
        id={id}
        label={fieldLabel}
        value={value}
        onChange={onChange}
        types={types}
        entityName={entityName}
        isRequired={isRequired}
        allowEmpty={allowEmpty}
        placeholder={allowEmpty ? emptyLabel : undefined}
      />
    );
  }

  if (!tenantProfile.available && !tenantProfile.loading) {
    // No profile to read: today's behaviour, a typed site name.
    return (
      <FormGroup label={fieldLabel} isRequired={isRequired} fieldId={id}>
        <TextInput id={id} value={value} onChange={(_e, v) => onChange(v.trim())} isRequired={isRequired} isDisabled={isDisabled} />
        <FormHelperText>
          <HelperText>
            <HelperTextItem>{helperText ?? t('sites.typeName')}</HelperTextItem>
          </HelperText>
        </FormHelperText>
      </FormGroup>
    );
  }

  return (
    <FormGroup label={fieldLabel} isRequired={isRequired} fieldId={id}>
      <FormSelect
        id={id}
        value={value}
        onChange={(_e, v) => onChange(v)}
        isRequired={isRequired}
        isDisabled={isDisabled || tenantProfile.loading}
        aria-describedby={`${id}-help`}
      >
        {(allowEmpty || !value) && (
          <FormSelectOption
            value=""
            label={tenantProfile.loading ? t('common.loading') : allowEmpty ? emptyLabel ?? t('sites.platformDefault') : t('sites.choose')}
            isDisabled={!allowEmpty}
          />
        )}
        {value && !chosen && <FormSelectOption value={value} label={value} />}
        {sites.map((s) => (
          <FormSelectOption key={s.name} value={s.name} label={siteOptionLabel(s, t)} isDisabled={s.state !== 'Available' && s.name !== value} />
        ))}
      </FormSelect>
      <FormHelperText>
        <HelperText id={`${id}-help`}>
          {sites.length === 0 && !tenantProfile.loading ? (
            <HelperTextItem variant="warning">{t('sites.noneOfType')}</HelperTextItem>
          ) : (
            <HelperTextItem>
              {chosen?.capabilities?.length ? (
                <span className="sc-chip-row">
                  {chosen.capabilities.map((c) => (
                    <Label key={c} isCompact color="blue">
                      {t(CAPABILITY_KEYS[c] ?? c, { defaultValue: c })}
                    </Label>
                  ))}
                  {chosen.billable ? (
                    <Label isCompact color="orange">
                      {t('sites.billable')}
                    </Label>
                  ) : null}
                </span>
              ) : (
                helperText ?? ''
              )}
            </HelperTextItem>
          )}
        </HelperText>
      </FormHelperText>
    </FormGroup>
  );
}
