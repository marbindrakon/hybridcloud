import React, { useEffect, useMemo, useState } from 'react';
import {
  Alert,
  Button,
  Form,
  FormGroup,
  FormSelect,
  FormSelectOption,
  Switch,
  TextArea,
  TextInput,
} from '@patternfly/react-core';
import { updateDashboardResource, useK8sResourceList } from '../hooks/k8s';
import { HybridSovereignKind, K8sResource } from '../types';
import { useTranslation } from '../i18n';
import { ListTextArea } from '../components/ListTextArea';
import { ObjectListEditor, type ObjectListColumn, type ObjectListRow } from '../components/ObjectListEditor';
import {
  getAtPath,
  isSpecFieldVisible,
  KIND_SPEC_META,
  setAtPath,
  SpecFieldColumn,
  SpecFieldMeta,
} from './specFieldMeta';

export interface GenericSpecEditorProps {
  kind: HybridSovereignKind;
  namespace: string;
  item: K8sResource;
  onSaved: () => void;
  /** Show the fields without allowing changes */
  readOnly?: boolean;
  /** tenant pages hide platform-only fields (adminOnly) */
  audience?: 'admin' | 'tenant';
}

/** Kinds that live in the platform namespace (reference selects list them there). */
const PLATFORM_KINDS: HybridSovereignKind[] = ['HybridFabric', 'CloudGateway', 'CloudInfrastructure', 'TransportLink'];
const PLATFORM_NS = 'sovereign-cloud';

function namedRefsToList(val: unknown): string[] {
  if (!Array.isArray(val)) return [];
  return val
    .map((item) => {
      if (typeof item === 'string') return item;
      if (item && typeof item === 'object' && 'name' in item) {
        return String((item as { name?: string }).name ?? '');
      }
      return '';
    })
    .filter(Boolean);
}

function refToText(val: unknown, refKey: string): string {
  if (val && typeof val === 'object' && refKey in val) {
    return String((val as Record<string, unknown>)[refKey] ?? '');
  }
  return '';
}

function asRows(val: unknown): ObjectListRow[] {
  return Array.isArray(val)
    ? val.filter((v): v is ObjectListRow => !!v && typeof v === 'object' && !Array.isArray(v))
    : [];
}

/** objectList field; a column with refKind becomes a select of that kind's names. */
function ObjectListField({
  field,
  id,
  label,
  value,
  original,
  namespace,
  disabled,
  help,
  onChange,
}: {
  field: SpecFieldMeta;
  id: string;
  label: string;
  value: unknown;
  original: unknown;
  namespace: string;
  disabled: boolean;
  help?: string;
  onChange: (rows: ObjectListRow[]) => void;
}): React.ReactElement {
  const { t } = useTranslation();
  const refColumn = (field.columns ?? []).find((c) => c.refKind);
  const refKind = refColumn?.refKind ?? 'HybridNetwork';
  const refList = useK8sResourceList<K8sResource>(refKind, {
    namespace: PLATFORM_KINDS.includes(refKind) ? PLATFORM_NS : namespace,
    enabled: !!refColumn && !!namespace,
  });
  const refNames = refList.items.map((i) => i.metadata.name);

  const toColumn = (c: SpecFieldColumn): ObjectListColumn => {
    if (c.refKind) {
      if (refList.error) return { key: c.key, label: t(c.labelKey), lockedOnExisting: c.lockedOnExisting };
      return {
        key: c.key,
        label: t(c.labelKey),
        type: 'select',
        lockedOnExisting: c.lockedOnExisting,
        options: [
          { value: '', label: c.emptyLabelKey ? t(c.emptyLabelKey) : '—' },
          ...refNames.map((n) => ({ value: n, label: n })),
        ],
      };
    }
    return {
      key: c.key,
      label: t(c.labelKey),
      type: c.type,
      lockedOnExisting: c.lockedOnExisting,
      options: c.options?.map((o) => ({ value: o.value, label: t(o.labelKey) })),
    };
  };

  const existingKeys = field.keyField
    ? asRows(original).map((r) => String(r[field.keyField as string] ?? ''))
    : [];

  return (
    <ObjectListEditor
      id={id}
      label={label}
      columns={(field.columns ?? []).map(toColumn)}
      value={asRows(value)}
      onChange={onChange}
      keyField={field.keyField}
      existingKeys={existingKeys}
      maxItems={field.maxItems}
      isDisabled={disabled}
      addLabel={t('form.addRow')}
      removeLabel={t('form.removeRow')}
      helperText={help}
    />
  );
}

export function GenericSpecEditor({
  kind,
  namespace,
  item,
  onSaved,
  readOnly = false,
  audience = 'tenant',
}: GenericSpecEditorProps): React.ReactElement {
  const { t } = useTranslation();
  const meta = KIND_SPEC_META[kind];
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const original = useMemo(() => (item.spec as Record<string, unknown>) ?? {}, [item]);
  const [draft, setDraft] = useState<Record<string, unknown>>(() => ({ ...original }));

  useEffect(() => {
    setDraft({ ...original });
  }, [original]);

  const allFields = useMemo(
    () => (meta?.fields ?? []).filter((f) => audience === 'admin' || !f.adminOnly),
    [meta, audience],
  );
  const fields = useMemo(
    () => allFields.filter((f) => isSpecFieldVisible(f, draft)),
    [allFields, draft],
  );

  if (!meta || allFields.length === 0) {
    return <p className="sc-text-muted">{t('form.noEditableFields')}</p>;
  }

  const isLocked = (field: SpecFieldMeta): boolean => {
    if (readOnly || field.immutable) return true;
    if (field.immutableWhenSet) {
      const before = getAtPath(original, field.path);
      return before != null && before !== '';
    }
    return false;
  };

  const setField = (field: SpecFieldMeta, value: unknown) => {
    if (isLocked(field)) return;
    // null removes the key on a merge patch (and is pruned on a full-spec replace).
    const empty = value === '' || value === undefined || (Array.isArray(value) && value.length === 0);
    const next = field.omitWhenEmpty && empty ? null : value;
    setDraft((prev) => setAtPath(prev, field.path, next));
  };

  const invalidNumber = (field: SpecFieldMeta, raw: unknown): boolean => {
    if (field.widget !== 'number' || raw == null || raw === '') return false;
    const n = Number(raw);
    if (!Number.isFinite(n)) return true;
    if (field.min !== undefined && n < field.min) return true;
    if (field.max !== undefined && n > field.max) return true;
    return false;
  };
  const hasInvalid = fields.some((f) => invalidNumber(f, getAtPath(draft, f.path)));

  const save = async () => {
    setSaving(true);
    setError(null);
    try {
      // Fields hidden by their condition (e.g. the other transport's settings) are removed.
      let spec = draft;
      for (const f of allFields) {
        if (!f.clearWhenHidden || isSpecFieldVisible(f, draft)) continue;
        const path = typeof f.clearWhenHidden === 'string' ? f.clearWhenHidden : f.path;
        spec = setAtPath(spec, path, null);
      }
      await updateDashboardResource(kind, item.metadata.name, namespace, { spec });
      onSaved();
    } catch (err) {
      setError(err instanceof Error ? err.message : t('form.updateFailed'));
    } finally {
      setSaving(false);
    }
  };

  const editable = !readOnly && fields.some((f) => !isLocked(f));

  return (
    <Form className="sc-form" onSubmit={(e) => e.preventDefault()}>
      {error && (
        <Alert variant="danger" title={t('form.updateFailed')} isInline className="sc-mb">
          {error}
        </Alert>
      )}
      {readOnly && (
        <p className="sc-text-muted" style={{ marginBottom: '0.5rem' }}>
          {t('form.readOnlyView')}
        </p>
      )}
      {fields.map((field) => {
        const id = `${kind}-${field.path}`;
        const label = t(field.labelKey);
        const raw = getAtPath(draft, field.path);
        const locked = isLocked(field);
        const help =
          field.immutable && !readOnly
            ? t('form.immutableHint')
            : field.immutableWhenSet && locked && !readOnly
              ? t('form.immutableOnceSetHint')
              : field.helpKey
                ? t(field.helpKey)
                : undefined;

        const hint = help ? <p className="sc-text-muted" style={{ marginTop: '0.25rem' }}>{help}</p> : null;

        if (field.widget === 'boolean') {
          const checked = Boolean(raw);
          return (
            <FormGroup key={field.path} label={label} fieldId={id}>
              <Switch
                id={id}
                isChecked={checked}
                isDisabled={locked}
                onChange={(_e, v) => setField(field, v)}
                label={checked ? t('common.enabled') : t('common.disabled')}
              />
              {hint}
            </FormGroup>
          );
        }

        if (field.widget === 'select' && field.options) {
          return (
            <FormGroup key={field.path} label={label} fieldId={id}>
              <FormSelect
                id={id}
                value={String(raw ?? '')}
                isDisabled={locked}
                onChange={(_e, v) => setField(field, v)}
                aria-label={label}
              >
                {field.options.map((opt) => (
                  <FormSelectOption key={opt.value} value={opt.value} label={t(opt.labelKey)} />
                ))}
              </FormSelect>
              {hint}
            </FormGroup>
          );
        }

        if (field.widget === 'json') {
          const text =
            typeof raw === 'string' ? raw : raw == null ? '' : JSON.stringify(raw, null, 2);
          return (
            <FormGroup key={field.path} label={label} fieldId={id}>
              <TextArea
                id={id}
                value={text}
                isDisabled={locked}
                onChange={(_e, v) => {
                  try {
                    setField(field, v.trim() ? JSON.parse(v) : undefined);
                    setError(null);
                  } catch {
                    setError(t('form.invalidJson', { field: label }));
                  }
                }}
                rows={6}
              />
              {hint}
            </FormGroup>
          );
        }

        if (field.widget === 'textarea') {
          return (
            <FormGroup key={field.path} label={label} fieldId={id}>
              <TextArea
                id={id}
                value={String(raw ?? '')}
                isDisabled={locked}
                onChange={(_e, v) => setField(field, v)}
                rows={3}
              />
              {hint}
            </FormGroup>
          );
        }

        if (field.widget === 'number') {
          return (
            <FormGroup key={field.path} label={label} fieldId={id}>
              <TextInput
                id={id}
                type="number"
                value={raw == null ? '' : String(raw)}
                isDisabled={locked}
                validated={invalidNumber(field, raw) ? 'error' : 'default'}
                onChange={(_e, v) => setField(field, v === '' ? undefined : Number(v))}
              />
              {hint}
            </FormGroup>
          );
        }

        if (field.widget === 'stringList' || field.widget === 'cidrList' || field.widget === 'namedRefList') {
          const list = field.widget === 'namedRefList' ? namedRefsToList(raw) : Array.isArray(raw) ? raw.map(String) : [];
          return (
            <FormGroup key={field.path} label={label} fieldId={id}>
              <ListTextArea
                id={id}
                value={list}
                isDisabled={locked}
                onChange={(next) =>
                  setField(field, field.widget === 'namedRefList' ? next.map((name) => ({ name })) : next)
                }
              />
              <p className="sc-text-muted" style={{ marginTop: '0.25rem' }}>
                {help || t('form.commaSeparated')}
              </p>
            </FormGroup>
          );
        }

        if (field.widget === 'namedRef') {
          const refKey = field.refKey ?? 'name';
          const current = refToText(raw, refKey);
          return (
            <FormGroup key={field.path} label={label} fieldId={id}>
              <TextInput
                id={id}
                value={current}
                isDisabled={locked}
                onChange={(_e, v) => {
                  if (!v.trim()) {
                    setDraft((prev) => setAtPath(prev, field.path, null));
                    return;
                  }
                  const prev = raw && typeof raw === 'object' ? (raw as Record<string, unknown>) : {};
                  setField(field, { ...prev, [refKey]: v });
                }}
              />
              {hint}
            </FormGroup>
          );
        }

        if (field.widget === 'objectList') {
          return (
            <ObjectListField
              key={field.path}
              field={field}
              id={id}
              label={label}
              value={raw}
              original={getAtPath(original, field.path)}
              namespace={namespace}
              disabled={locked}
              help={help}
              onChange={(rows) => setField(field, rows)}
            />
          );
        }

        return (
          <FormGroup key={field.path} label={label} fieldId={id}>
            <TextInput
              id={id}
              value={String(raw ?? '')}
              isDisabled={locked}
              onChange={(_e, v) => setField(field, v)}
            />
            {hint}
          </FormGroup>
        );
      })}
      {!readOnly && (
        <Button variant="primary" isDisabled={saving || !editable || hasInvalid} onClick={save}>
          {saving ? t('form.saving') : t('common.save')}
        </Button>
      )}
    </Form>
  );
}
