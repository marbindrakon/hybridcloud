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
import { updateDashboardResource } from '../hooks/k8s';
import { HybridSovereignKind, K8sResource } from '../types';
import { useTranslation } from '../i18n';
import { getAtPath, isSpecFieldVisible, KIND_SPEC_META, setAtPath, SpecFieldMeta } from './specFieldMeta';

export interface GenericSpecEditorProps {
  kind: HybridSovereignKind;
  namespace: string;
  item: K8sResource;
  onSaved: () => void;
}

function listToText(val: unknown): string {
  if (Array.isArray(val)) return val.map(String).join(', ');
  if (val == null) return '';
  return String(val);
}

function textToList(val: string): string[] {
  return val
    .split(/[,\n]/)
    .map((s) => s.trim())
    .filter(Boolean);
}

function namedRefsToText(val: unknown): string {
  if (Array.isArray(val)) {
    return val
      .map((item) => {
        if (typeof item === 'string') return item;
        if (item && typeof item === 'object' && 'name' in item) {
          return String((item as { name?: string }).name ?? '');
        }
        return '';
      })
      .filter(Boolean)
      .join(', ');
  }
  if (val == null) return '';
  return String(val);
}

function textToNamedRefs(val: string): Array<{ name: string }> {
  return textToList(val).map((name) => ({ name }));
}

function namedRefToText(val: unknown): string {
  if (val && typeof val === 'object' && 'name' in val) {
    return String((val as { name?: string }).name ?? '');
  }
  return '';
}

export function GenericSpecEditor({
  kind,
  namespace,
  item,
  onSaved,
}: GenericSpecEditorProps): React.ReactElement {
  const { t } = useTranslation();
  const meta = KIND_SPEC_META[kind];
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [draft, setDraft] = useState<Record<string, unknown>>(
    () => ({ ...(item.spec as Record<string, unknown>) }),
  );

  useEffect(() => {
    setDraft({ ...((item.spec as Record<string, unknown>) ?? {}) });
  }, [item]);

  const allFields = useMemo(() => meta?.fields ?? [], [meta]);
  const fields = useMemo(
    () => allFields.filter((f) => isSpecFieldVisible(f, draft)),
    [allFields, draft],
  );

  if (!meta || allFields.length === 0) {
    return (
      <p className="sc-text-muted">{t('form.noEditableFields')}</p>
    );
  }

  const setField = (field: SpecFieldMeta, value: unknown) => {
    if (field.immutable) return;
    // null removes the key on a merge patch (and is pruned on a full-spec replace).
    const next = field.omitWhenEmpty && value === '' ? null : value;
    setDraft((prev) => setAtPath(prev, field.path, next));
  };

  const save = async () => {
    setSaving(true);
    setError(null);
    try {
      await updateDashboardResource(kind, item.metadata.name, namespace, { spec: draft });
      onSaved();
    } catch (err) {
      setError(err instanceof Error ? err.message : t('form.updateFailed'));
    } finally {
      setSaving(false);
    }
  };

  return (
    <Form className="sc-form" onSubmit={(e) => e.preventDefault()}>
      {error && (
        <Alert variant="danger" title={t('form.updateFailed')} isInline className="sc-mb">
          {error}
        </Alert>
      )}
      {fields.map((field) => {
        const id = `${kind}-${field.path}`;
        const label = t(field.labelKey);
        const raw = getAtPath(draft, field.path);
        const help = field.immutable
          ? t('form.immutableHint')
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
                isDisabled={field.immutable}
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
                isDisabled={field.immutable}
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
                isDisabled={field.immutable}
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
                isDisabled={field.immutable}
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
                isDisabled={field.immutable}
                onChange={(_e, v) => setField(field, v === '' ? undefined : Number(v))}
              />
              {hint}
            </FormGroup>
          );
        }

        if (field.widget === 'stringList' || field.widget === 'cidrList') {
          return (
            <FormGroup key={field.path} label={label} fieldId={id}>
              <TextArea
                id={id}
                value={listToText(raw)}
                isDisabled={field.immutable}
                onChange={(_e, v) => setField(field, textToList(v))}
                rows={2}
              />
              <p className="sc-text-muted" style={{ marginTop: '0.25rem' }}>
                {help || t('form.commaSeparated')}
              </p>
            </FormGroup>
          );
        }

        if (field.widget === 'namedRef') {
          const current = namedRefToText(raw);
          return (
            <FormGroup key={field.path} label={label} fieldId={id}>
              <TextInput
                id={id}
                value={current}
                isDisabled={field.immutable}
                onChange={(_e, v) => {
                  const nextName = v.trim();
                  if (!nextName) {
                    setField(field, null);
                    return;
                  }
                  const prev = raw && typeof raw === 'object' ? (raw as Record<string, unknown>) : {};
                  setField(field, { ...prev, name: v });
                }}
              />
              {hint}
            </FormGroup>
          );
        }

        if (field.widget === 'namedRefList') {
          return (
            <FormGroup key={field.path} label={label} fieldId={id}>
              <TextArea
                id={id}
                value={namedRefsToText(raw)}
                isDisabled={field.immutable}
                onChange={(_e, v) => setField(field, textToNamedRefs(v))}
                rows={2}
              />
              <p className="sc-text-muted" style={{ marginTop: '0.25rem' }}>
                {help || t('form.commaSeparated')}
              </p>
            </FormGroup>
          );
        }

        return (
          <FormGroup key={field.path} label={label} fieldId={id}>
            <TextInput
              id={id}
              value={String(raw ?? '')}
              isDisabled={field.immutable}
              onChange={(_e, v) => setField(field, v)}
            />
            {hint}
          </FormGroup>
        );
      })}
      <Button variant="primary" isDisabled={saving || fields.every((f) => f.immutable)} onClick={save}>
        {saving ? t('form.saving') : t('common.save')}
      </Button>
    </Form>
  );
}
