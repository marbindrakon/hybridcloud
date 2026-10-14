import React, { useState } from 'react';
import { FormGroup, Select, SelectList, SelectOption, MenuToggle } from '@patternfly/react-core';
import type { MenuToggleElement } from '@patternfly/react-core';

export interface FabricSelectOption {
  value: string;
  label: string;
  /** HybridFabric CR readiness — options not Ready are shown but flagged */
  ready?: boolean;
  /** Entity not tagged on this fabric — shown greyed with tooltip per §18.4 */
  isDisabled?: boolean;
}

export interface FabricSelectProps {
  id?: string;
  label?: string;
  value: string;
  onChange: (next: string) => void;
  options: FabricSelectOption[];
  isRequired?: boolean;
  placeholder?: string;
  emptyStateMessage?: string;
}

/**
 * Single-select dropdown of Ready `HybridFabric` CR names (PatternFly Select).
 * Writes `HybridNetwork.spec.fabricRef`, gateway `fabricRef`, link `fabricRef` — never a free-text
 * fabric name.
 */
export function FabricSelect({
  id = 'fabric-select',
  label = 'Fabric',
  value,
  onChange,
  options,
  isRequired,
  placeholder = 'Select fabric…',
  emptyStateMessage = 'Ask platform admin to tag your Entity on a HybridFabric',
}: FabricSelectProps): React.ReactElement {
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
              {emptyStateMessage}
            </SelectOption>
          ) : (
            options.map((o) => (
              <SelectOption
                key={o.value}
                value={o.value}
                isSelected={o.value === value}
                isDisabled={o.isDisabled}
                description={
                  o.isDisabled
                    ? 'Entity not tagged on this fabric'
                    : o.ready === false
                      ? 'Not Ready'
                      : undefined
                }
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
