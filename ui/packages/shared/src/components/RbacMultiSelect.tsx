import React, { useState } from 'react';
import {
  FormGroup,
  Select,
  SelectList,
  SelectOption,
  MenuToggle,
  Label,
  LabelGroup,
  FormHelperText,
  HelperText,
  HelperTextItem,
} from '@patternfly/react-core';
import { useTranslation } from '../i18n';
import type { MenuToggleElement } from '@patternfly/react-core';

export interface RbacMultiSelectProps {
  id: string;
  label: string;
  value: string[];
  onChange: (next: string[]) => void;
  options: { value: string; label: string }[];
  isRequired?: boolean;
  placeholder?: string;
  /** One line under the picker (what members of these groups can do). */
  helperText?: string;
  /** Field error (for example from the API's refusal). */
  error?: string;
}

/** Multi-select dropdown of Rbac CR names (chip + checkbox PatternFly Select). */
export function RbacMultiSelect({
  id,
  label,
  value,
  onChange,
  options,
  isRequired,
  placeholder,
  helperText,
  error,
}: RbacMultiSelectProps): React.ReactElement {
  const { t } = useTranslation();
  const [isOpen, setIsOpen] = useState(false);
  const shownPlaceholder = placeholder ?? t('form.accessGroupsPlaceholder');
  const selected = value ?? [];

  const toggle = (toggleRef: React.Ref<MenuToggleElement>) => (
    <MenuToggle
      ref={toggleRef}
      onClick={() => setIsOpen((o) => !o)}
      isExpanded={isOpen}
      style={{ width: '100%' }}
      aria-label={label}
    >
      {selected.length === 0
        ? shownPlaceholder
        : selected.length === 1
          ? selected[0]
          : t('form.nSelected', { count: selected.length })}
    </MenuToggle>
  );

  const onSelect = (_event: unknown, selection: string | number | undefined) => {
    const v = String(selection ?? '');
    if (!v) return;
    onChange(selected.includes(v) ? selected.filter((s) => s !== v) : [...selected, v]);
  };

  return (
    <FormGroup label={label} isRequired={isRequired} fieldId={id}>
      {selected.length > 0 && (
        <LabelGroup numLabels={12} className="pf-v6-u-mb-sm">
          {selected.map((name) => (
            <Label key={name} color="blue" onClose={() => onChange(selected.filter((s) => s !== name))}>
              {name}
            </Label>
          ))}
        </LabelGroup>
      )}
      <Select
        id={id}
        isOpen={isOpen}
        selected={selected}
        onSelect={onSelect}
        onOpenChange={(next) => setIsOpen(next)}
        toggle={toggle}
        shouldFocusToggleOnSelect={false}
      >
        <SelectList>
          {options.length === 0 ? (
            <SelectOption isDisabled value="">
              {t('form.noAccessGroups')}
            </SelectOption>
          ) : (
            options.map((o) => (
              <SelectOption
                key={o.value}
                value={o.value}
                hasCheckbox
                isSelected={selected.includes(o.value)}
              >
                {o.label}
              </SelectOption>
            ))
          )}
        </SelectList>
      </Select>
      {(helperText || error) && (
        <FormHelperText>
          <HelperText id={`${id}-help`}>
            {error ? <HelperTextItem variant="error">{error}</HelperTextItem> : null}
            {helperText ? <HelperTextItem>{helperText}</HelperTextItem> : null}
          </HelperText>
        </FormHelperText>
      )}
    </FormGroup>
  );
}
