import React, { useState } from 'react';
import { FormGroup, Select, SelectList, SelectOption, MenuToggle } from '@patternfly/react-core';
import type { MenuToggleElement } from '@patternfly/react-core';
import type { K8sResource } from '../types';
import { useTranslation } from '../i18n';

/** Placement backends offered in the UI (PlatformOpenshift no longer attaches to the fabric). */
export type BackendKind = 'CloudOSO' | 'CloudVirt';

export interface BackendSelectOption {
  kind: BackendKind;
  name: string;
  label: string;
  ready?: boolean;
}

export interface BackendSelectValue {
  kind: string;
  name: string;
}

export interface BackendSelectProps {
  id?: string;
  label?: string;
  value: BackendSelectValue | null;
  onChange: (backend: BackendSelectValue) => void;
  options: BackendSelectOption[];
  isRequired?: boolean;
  placeholder?: string;
}

const encode = (o: { kind: string; name: string }): string => `${o.kind}::${o.name}`;
const decode = (v: string): BackendSelectValue => {
  const [kind, name] = v.split('::');
  return { kind: kind ?? '', name: name ?? '' };
};

/**
 * Build BackendSelect options from CloudOSO / CloudVirt lists (tenant cloud projects).
 * PlatformOpenshift is not a placement backend: hosted clusters do not join the fabric; their
 * workloads attach through a CloudVirt placement instead.
 */
export function buildBackendOptions(sources: {
  cloudosos?: K8sResource<object, { ready?: boolean }>[] | null;
  cloudvirts?: K8sResource<object, { ready?: boolean }>[] | null;
}): BackendSelectOption[] {
  const cloudoso = (sources.cloudosos ?? []).map((c) => ({
    kind: 'CloudOSO' as const,
    name: c.metadata.name,
    label: c.metadata.name,
    ready: c.status?.ready,
  }));
  const cloudvirt = (sources.cloudvirts ?? []).map((c) => ({
    kind: 'CloudVirt' as const,
    name: c.metadata.name,
    label: c.metadata.name,
    ready: c.status?.ready,
  }));
  return [...cloudoso, ...cloudvirt];
}

/**
 * Single-select "backend" dropdown for `NetworkPlacement.spec.backend` — CloudOSO or CloudVirt
 * project in the entity namespace. Writes `{ kind, name }`, never a free-text backend name.
 */
export function BackendSelect({
  id = 'backend-select',
  label = 'Backend',
  value,
  onChange,
  options,
  isRequired,
  placeholder = 'Select backend…',
}: BackendSelectProps): React.ReactElement {
  const { t } = useTranslation();
  const [isOpen, setIsOpen] = useState(false);
  const selectedValue = value ? encode(value) : '';
  const selectedOption = options.find((o) => encode(o) === selectedValue);

  const toggle = (toggleRef: React.Ref<MenuToggleElement>) => (
    <MenuToggle
      ref={toggleRef}
      onClick={() => setIsOpen((o) => !o)}
      isExpanded={isOpen}
      style={{ width: '100%' }}
      aria-label={label}
    >
      {selectedOption ? `${selectedOption.kind} · ${selectedOption.label}` : placeholder}
    </MenuToggle>
  );

  const onSelect = (_event: unknown, selection: string | number | undefined) => {
    const v = String(selection ?? '');
    if (!v) return;
    onChange(decode(v));
    setIsOpen(false);
  };

  return (
    <FormGroup label={label} isRequired={isRequired} fieldId={id}>
      <Select
        id={id}
        isOpen={isOpen}
        selected={selectedValue}
        onSelect={onSelect}
        onOpenChange={(next) => setIsOpen(next)}
        toggle={toggle}
        shouldFocusToggleOnSelect={false}
      >
        <SelectList>
          {options.length === 0 ? (
            <SelectOption isDisabled value="">
              {t('form.backendNone')}
            </SelectOption>
          ) : (
            options.map((o) => (
              <SelectOption
                key={encode(o)}
                value={encode(o)}
                isSelected={encode(o) === selectedValue}
                description={o.ready === false ? t('form.notReady') : undefined}
              >
                {o.kind} · {o.label}
              </SelectOption>
            ))
          )}
        </SelectList>
      </Select>
    </FormGroup>
  );
}
