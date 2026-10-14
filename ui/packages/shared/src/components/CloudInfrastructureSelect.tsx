import React, { useState } from 'react';
import { FormGroup, Select, SelectList, SelectOption, MenuToggle, TextInput } from '@patternfly/react-core';
import type { MenuToggleElement } from '@patternfly/react-core';
import { useK8sResourceList } from '../hooks/k8s';
import { useTranslation } from '../i18n';
import type { CloudInfrastructure, CloudInfrastructureType } from '../types';

/** CloudInfrastructure objects are platform-owned and live in this namespace. */
export const CLOUD_INFRASTRUCTURE_NS = 'sovereign-cloud';

export interface CloudInfrastructureFilter {
  /** Keep only these site types */
  types?: CloudInfrastructureType[];
  /** Keep only sites this Entity may use (empty spec.entityRefs means every Entity) */
  entityName?: string;
}

/** Filter CloudInfrastructure objects by type and entityRefs. */
export function filterCloudInfrastructures(
  items: CloudInfrastructure[] | undefined | null,
  filter: CloudInfrastructureFilter = {},
): CloudInfrastructure[] {
  return (items ?? []).filter((item) => {
    const spec = item?.spec;
    if (!spec) return false;
    if (filter.types?.length && !filter.types.includes(spec.type)) return false;
    if (filter.entityName) {
      const refs = (spec.entityRefs ?? []).map((r) => r.name);
      if (refs.length > 0 && !refs.includes(filter.entityName)) return false;
    }
    return true;
  });
}

export interface CloudInfrastructureSelectProps extends CloudInfrastructureFilter {
  id?: string;
  label?: string;
  /** CloudInfrastructure name (writes spec.cloudRef.name) */
  value: string;
  onChange: (next: string) => void;
  isRequired?: boolean;
  placeholder?: string;
  /** Called with the selected object when the list is readable (e.g. to derive the site type) */
  onSelectInfrastructure?: (item: CloudInfrastructure | undefined) => void;
}

/**
 * Single-select of CloudInfrastructure sites in sovereign-cloud, filtered by type and entityRefs.
 * Writes `spec.cloudRef.name` on CloudGateway / CloudOSO / CloudVirt / CloudAWS. Tenants usually
 * cannot list the platform namespace (403): the picker then degrades to a text input.
 */
export function CloudInfrastructureSelect({
  id = 'cloudinfrastructure-select',
  label,
  value,
  onChange,
  isRequired,
  placeholder,
  types,
  entityName,
  onSelectInfrastructure,
}: CloudInfrastructureSelectProps): React.ReactElement {
  const { t } = useTranslation();
  const [isOpen, setIsOpen] = useState(false);
  const list = useK8sResourceList<CloudInfrastructure>('CloudInfrastructure', {
    namespace: CLOUD_INFRASTRUCTURE_NS,
  });
  const fieldLabel = label ?? t('fields.cloudRef');
  const options = filterCloudInfrastructures(list.items, { types, entityName });
  const selected = options.find((o) => o.metadata.name === value);

  const optionLabel = (item: CloudInfrastructure) =>
    item.spec.displayName
      ? `${item.metadata.name} — ${item.spec.displayName} (${item.spec.type})`
      : `${item.metadata.name} (${item.spec.type})`;

  if (list.error) {
    // Not allowed to list sovereign-cloud (tenant) or CRD missing: accept a typed name.
    return (
      <FormGroup label={fieldLabel} isRequired={isRequired} fieldId={id}>
        <TextInput
          id={id}
          value={value}
          onChange={(_e, v) => onChange(v.trim())}
          isRequired={isRequired}
          placeholder={placeholder ?? t('form.cloudInfraNamePlaceholder')}
        />
        <p className="sc-text-muted" style={{ marginTop: '0.25rem' }}>
          {t('form.cloudInfraListUnavailable')}
        </p>
      </FormGroup>
    );
  }

  const toggle = (toggleRef: React.Ref<MenuToggleElement>) => (
    <MenuToggle
      ref={toggleRef}
      onClick={() => setIsOpen((o) => !o)}
      isExpanded={isOpen}
      style={{ width: '100%' }}
      aria-label={fieldLabel}
    >
      {selected ? optionLabel(selected) : value || placeholder || t('form.cloudInfraSelectPlaceholder')}
    </MenuToggle>
  );

  const onSelect = (_event: unknown, selection: string | number | undefined) => {
    const next = String(selection ?? '');
    onChange(next);
    onSelectInfrastructure?.(options.find((o) => o.metadata.name === next));
    setIsOpen(false);
  };

  return (
    <FormGroup label={fieldLabel} isRequired={isRequired} fieldId={id}>
      <Select
        id={id}
        isOpen={isOpen}
        selected={value}
        onSelect={onSelect}
        onOpenChange={(next) => setIsOpen(next)}
        toggle={toggle}
        shouldFocusToggleOnSelect={false}
      >
        <SelectList>
          {options.length === 0 ? (
            <SelectOption isDisabled value="">
              {list.loading ? t('common.loading') : t('form.cloudInfraNone')}
            </SelectOption>
          ) : (
            options.map((o) => (
              <SelectOption
                key={o.metadata.name}
                value={o.metadata.name}
                isSelected={o.metadata.name === value}
                description={o.status?.ready === false ? t('form.notReady') : undefined}
              >
                {optionLabel(o)}
              </SelectOption>
            ))
          )}
        </SelectList>
      </Select>
    </FormGroup>
  );
}
