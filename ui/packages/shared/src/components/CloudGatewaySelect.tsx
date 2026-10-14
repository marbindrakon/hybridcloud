import React, { useState } from 'react';
import { FormGroup, Select, SelectList, SelectOption, MenuToggle } from '@patternfly/react-core';
import type { MenuToggleElement } from '@patternfly/react-core';

export interface CloudGatewaySelectOption {
  value: string;
  label: string;
  ready?: boolean;
}

export interface CloudGatewaySelectProps {
  id?: string;
  label?: string;
  value: string;
  onChange: (next: string) => void;
  options: CloudGatewaySelectOption[];
  isRequired?: boolean;
  placeholder?: string;
}

/**
 * Single-select dropdown of `CloudGateway` CRs for the currently selected Fabric — Ready
 * landing zone. Writes `TransportLink.spec.cloudGatewayRef` (design/fabric.md §18.2 / §18.5).
 * Options should be pre-filtered by the caller to gateways whose `spec.fabricRef` matches the
 * selected FabricSelect value; re-filter when Fabric changes (§18.11).
 */
export function CloudGatewaySelect({
  id = 'cloudgateway-select',
  label = 'Cloud Gateway',
  value,
  onChange,
  options,
  isRequired,
  placeholder = 'Select Cloud Gateway…',
}: CloudGatewaySelectProps): React.ReactElement {
  const [isOpen, setIsOpen] = useState(false);
  const selectedOption = options.find((o) => o.value === value);

  const toggle = (toggleRef: React.Ref<MenuToggleElement>) => (
    <MenuToggle
      ref={toggleRef}
      onClick={() => setIsOpen((o) => !o)}
      isExpanded={isOpen}
      style={{ width: '100%' }}
      aria-label={label}
    >
      {selectedOption ? selectedOption.label : placeholder}
    </MenuToggle>
  );

  const onSelect = (_event: unknown, selection: string | number | undefined) => {
    onChange(String(selection ?? ''));
    setIsOpen(false);
  };

  return (
    <FormGroup label={label} isRequired={isRequired} fieldId={id}>
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
              No Cloud Gateways for this fabric
            </SelectOption>
          ) : (
            options.map((o) => (
              <SelectOption
                key={o.value}
                value={o.value}
                isSelected={o.value === value}
                description={o.ready === false ? 'Not Ready' : undefined}
              >
                {o.label}
              </SelectOption>
            ))
          )}
        </SelectList>
      </Select>
    </FormGroup>
  );
}
