import React, { useEffect, useState } from 'react';
import {
  Alert,
  FormGroup,
  MenuToggle,
  Select,
  SelectList,
  SelectOption,
  TextInput,
} from '@patternfly/react-core';
import type { MenuToggleElement } from '@patternfly/react-core';
import {
  useEntityOptions,
  entityNameForNamespace,
  namespaceForEntityName,
  type EntityOption,
} from '../hooks/entityOptions';
import { useTranslation } from '../i18n';

export interface EntitySelectProps {
  id?: string;
  label?: string;
  /** Selected Entity CR name ('' = nothing picked yet). */
  value: string;
  /**
   * Called with the Entity name and the tenant namespace it resolves to
   * (`status.entity`, else entity-<name>). Both are '' when cleared.
   */
  onChange: (entityName: string, namespace: string) => void;
  isRequired?: boolean;
  placeholder?: string;
  /**
   * Namespace the host app already scoped the user to. Preselects the matching
   * Entity and is the single option when sovereign-cloud is not listable.
   */
  fallbackNamespace?: string;
  helperText?: React.ReactNode;
  /** Pause the Entity list (e.g. the form does not need it yet). */
  enabled?: boolean;
}

/**
 * Single-select of the Entities (tenants) the user can see, used by every
 * entity-scoped create form — Team, Project, Assignment, PlatformOpenshift —
 * so the target namespace is picked rather than typed.
 *
 * Entities live in sovereign-cloud. That read is best effort: a tenant who is
 * forbidden to list it falls back to the one Entity the host app scoped them
 * to, and only an outright failure (CRD missing, API down) degrades to a text
 * input. Option state is never colour alone — "Not Ready" is written out as
 * the option description, and the whole control is a labelled PatternFly
 * Select, so it reads and keys the same in light and dark themes.
 */
export function EntitySelect({
  id = 'entity-select',
  label,
  value,
  onChange,
  isRequired,
  placeholder,
  fallbackNamespace,
  helperText,
  enabled = true,
}: EntitySelectProps): React.ReactElement {
  const { t } = useTranslation();
  const [isOpen, setIsOpen] = useState(false);
  const { options, loading, error, forbidden, namespaceOf } = useEntityOptions({
    enabled,
    fallbackNamespace,
  });
  const fieldLabel = label ?? t('fields.entity', { defaultValue: 'Entity' });
  const selected = options.find((o) => o.name === value);

  // Preselect whatever the host app already scoped us to, so switching apps
  // (tenant dashboard, console plugin) does not start from an empty form.
  useEffect(() => {
    if (value || loading || options.length === 0) return;
    const wanted = fallbackNamespace
      ? (options.find((o) => o.namespace === fallbackNamespace) ??
        options.find((o) => o.name === entityNameForNamespace(fallbackNamespace)))
      : options.length === 1
        ? options[0]
        : undefined;
    if (wanted) onChange(wanted.name, wanted.namespace);
  }, [value, loading, options, fallbackNamespace, onChange]);

  if (error) {
    // CRD missing or the API is down — accept a typed Entity name rather than
    // locking the user out of the form entirely.
    return (
      <FormGroup label={fieldLabel} isRequired={isRequired} fieldId={id}>
        <TextInput
          id={id}
          value={value}
          onChange={(_e, v) => onChange(v.trim(), namespaceForEntityName(v))}
          isRequired={isRequired}
          placeholder={t('form.entityNamePlaceholder')}
        />
        <p className="sc-text-muted" style={{ marginTop: '0.25rem' }}>
          {t('form.entityListUnavailable')}
        </p>
      </FormGroup>
    );
  }

  const optionLabel = (o: EntityOption) => `${o.name} — ${o.namespace}`;

  const toggle = (toggleRef: React.Ref<MenuToggleElement>) => (
    <MenuToggle
      ref={toggleRef}
      onClick={() => setIsOpen((o) => !o)}
      isExpanded={isOpen}
      isDisabled={loading && options.length === 0}
      style={{ width: '100%' }}
      aria-label={fieldLabel}
    >
      {selected
        ? optionLabel(selected)
        : value || placeholder || t('form.entitySelectPlaceholder')}
    </MenuToggle>
  );

  const onSelect = (_event: unknown, selection: string | number | undefined) => {
    const next = String(selection ?? '');
    if (!next) return;
    onChange(next, namespaceOf(next));
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
              {loading ? t('common.loading') : t('form.entityNone')}
            </SelectOption>
          ) : (
            options.map((o) => (
              <SelectOption
                key={o.name}
                value={o.name}
                isSelected={o.name === value}
                description={o.ready === false ? t('form.notReady') : o.description}
              >
                {optionLabel(o)}
              </SelectOption>
            ))
          )}
        </SelectList>
      </Select>
      {!loading && options.length === 0 && (
        <Alert variant="warning" isInline isPlain title={t('form.entityNone')} className="sc-mb" />
      )}
      {forbidden && (
        <p className="sc-text-muted" style={{ marginTop: '0.25rem' }}>
          {t('form.entityListRestricted')}
        </p>
      )}
      {helperText && (
        <p className="sc-text-muted" style={{ marginTop: '0.25rem' }}>
          {helperText}
        </p>
      )}
    </FormGroup>
  );
}
