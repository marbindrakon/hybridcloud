import React, { useState } from 'react';
import { FormGroup, Select, SelectList, SelectOption, MenuToggle } from '@patternfly/react-core';
import type { MenuToggleElement } from '@patternfly/react-core';

export interface CloudVirtSelectOption {
  value: string;
  label: string;
  ready?: boolean;
}

export interface CloudVirtSelectProps {
  id?: string;
  label?: string;
  value: string;
  onChange: (next: string) => void;
  options: CloudVirtSelectOption[];
  isRequired?: boolean;
  placeholder?: string;
}

/**
 * Single-select dropdown of Ready `CloudVirt` CRs — fabric-capable HCP/CNV backend.
 * Writes a NetworkPlacement backend name.
 */
export function CloudVirtSelect({
  id = 'cloudvirt-select',
  label = 'CloudVirt backend',
  value,
  onChange,
  options,
  isRequired,
  placeholder = 'Select CloudVirt…',
}: CloudVirtSelectProps): React.ReactElement {
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
              No CloudVirt environments available
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
