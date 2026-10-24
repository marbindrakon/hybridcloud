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
import { RbacMultiSelect } from '../components/RbacMultiSelect';
import { AllowedSitesSelect } from '../components/AllowedSitesSelect';
import { selfSubjectAccessReview } from '../access/ssar';
import { errorText } from '../access/errorText';
import { causeLines, fieldErrorsFrom } from './apiErrors';
import { useServerCheckWith } from './useServerCheck';
import { HybridSovereignKind, K8sResource, KIND_PLURALS } from '../types';
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
    const re = c.pattern ? new RegExp(c.pattern) : null;
    const filled = (v: unknown) => String(v ?? '').trim() !== '';
    return {
      key: c.key,
      label: t(c.labelKey),
      type: c.type,
      lockedOnExisting: c.lockedOnExisting,
      placeholder: c.placeholder,
      options: c.options?.map((o) => ({ value: o.value, label: t(o.labelKey) })),
      validate:
        c.required || re
          ? (v: unknown, row: ObjectListRow) => {
              if (c.required && !filled(v) && Object.keys(row).some((k) => k !== c.key && filled(row[k]))) return false;
              return !re || !filled(v) || re.test(String(v).trim());
            }
          : undefined,
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

/** Select of the names of a kind in the object's namespace (typed name when the list cannot be read). */
function RefNameField({
  id,
  label,
  refKind,
  namespace,
  value,
  disabled,
  emptyLabel,
  hint,
  error,
  onChange,
}: {
  id: string;
  label: string;
  refKind: HybridSovereignKind;
  namespace: string;
  value: string;
  disabled: boolean;
  emptyLabel?: string;
  hint: React.ReactNode;
  error?: string;
  onChange: (v: string) => void;
}): React.ReactElement {
  const list = useK8sResourceList<K8sResource>(refKind, {
    namespace: PLATFORM_KINDS.includes(refKind) ? PLATFORM_NS : namespace,
    enabled: !!namespace && !disabled,
  });
  const names = list.items.map((i) => i.metadata.name);
  return (
    <FormGroup label={label} fieldId={id}>
      {list.error || disabled ? (
        <TextInput id={id} value={value} isDisabled={disabled} onChange={(_e, v) => onChange(v.trim())} validated={error ? 'error' : 'default'} />
      ) : (
        <FormSelect id={id} value={value} onChange={(_e, v) => onChange(v)} aria-label={label} validated={error ? 'error' : 'default'}>
          <FormSelectOption value="" label={emptyLabel ?? '—'} />
          {value && !names.includes(value) && <FormSelectOption value={value} label={value} />}
          {names.map((n) => (
            <FormSelectOption key={n} value={n} label={n} />
          ))}
        </FormSelect>
      )}
      {error && <p className="pf-v6-c-helper-text__item-text sc-field-error" style={{ color: 'var(--pf-t--global--text--color--status--danger--default)' }}>{error}</p>}
      {hint}
    </FormGroup>
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
  const [saved, setSaved] = useState(false);
  const original = useMemo(() => (item.spec as Record<string, unknown>) ?? {}, [item]);
  const [draft, setDraft] = useState<Record<string, unknown>>(() => ({ ...original }));
  /** JSON fields keep the raw text while typing; parsed on every change, the error stays on the field. */
  const [jsonText, setJsonText] = useState<Record<string, string>>({});
  const [jsonErrors, setJsonErrors] = useState<Record<string, string>>({});
  const rbacs = useK8sResourceList<K8sResource>('Rbac', {
    namespace,
    enabled: !!namespace && (meta?.fields ?? []).some((f) => f.widget === 'rbacRefList' || f.widget === 'rbacRef'),
  });
  const rbacOptions = rbacs.items.map((i) => ({ value: i.metadata.name, label: i.metadata.name }));

  // A polled object refreshes `item`: reset the draft only when the stored spec really changed.
  const originalKey = JSON.stringify(original);
  useEffect(() => {
    setDraft({ ...original });
    setJsonText({});
    setJsonErrors({});
  }, [originalKey]); // eslint-disable-line react-hooks/exhaustive-deps

  const allFields = useMemo(
    () => (meta?.fields ?? []).filter((f) => audience === 'admin' || !f.adminOnly),
    [meta, audience],
  );

  // Access lists: only users allowed to delete this object may change them (admission rule); the Assignment's
  // access groups need namespace-wide edit (a team admin views them). Unknown counts as not allowed.
  const objectName = item.metadata.name;
  const wantsDeleteCheck = !readOnly && allFields.some((f) => f.accessList);
  const wantsNsEditCheck = !readOnly && allFields.some((f) => f.namespaceEditOnly);
  const [mayDelete, setMayDelete] = useState<boolean | undefined>(undefined);
  const [mayEditAll, setMayEditAll] = useState<boolean | undefined>(undefined);
  useEffect(() => {
    let off = false;
    const resource = KIND_PLURALS[kind];
    if (wantsDeleteCheck) {
      void selfSubjectAccessReview({ namespace, resource, name: objectName, verb: 'delete' }).then((ok) => {
        if (!off) setMayDelete(ok);
      });
    }
    if (wantsNsEditCheck) {
      void selfSubjectAccessReview({ namespace, resource, verb: 'update' }).then((ok) => {
        if (!off) setMayEditAll(ok);
      });
    }
    return () => {
      off = true;
    };
  }, [kind, namespace, objectName, wantsDeleteCheck, wantsNsEditCheck]);
  const accessLocked = (field: SpecFieldMeta): boolean =>
    (!!field.accessList && mayDelete !== true) || (!!field.namespaceEditOnly && mayEditAll !== true);
  const fields = useMemo(
    () => allFields.filter((f) => isSpecFieldVisible(f, draft)),
    [allFields, draft],
  );

  if (!meta || allFields.length === 0) {
    return <p className="sc-text-muted">{t('form.noEditableFields')}</p>;
  }

  // A field fixed while a status count is above zero (e.g. a network's IPv6 source while it has placements).
  const statusLocked = (field: SpecFieldMeta): boolean => {
    if (!field.lockedWhileStatus) return false;
    const st = ((item as { status?: unknown }).status ?? {}) as Record<string, unknown>;
    const n = Number(getAtPath(st, field.lockedWhileStatus.path));
    return Number.isFinite(n) && n > 0;
  };
  const isLocked = (field: SpecFieldMeta): boolean => {
    if (readOnly || field.immutable) return true;
    if (accessLocked(field)) return true;
    if (statusLocked(field)) return true;
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

  /** The spec that Save sends: fields hidden by their condition (e.g. the other transport's settings) removed. */
  const specToSend = (): Record<string, unknown> => {
    let spec = draft;
    for (const f of allFields) {
      if (!f.clearWhenHidden || isSpecFieldVisible(f, draft)) continue;
      const path = typeof f.clearWhenHidden === 'string' ? f.clearWhenHidden : f.path;
      spec = setAtPath(spec, path, null);
    }
    return spec;
  };
  const dirty = JSON.stringify(draft) !== originalKey;
  const hasJsonErrors = Object.keys(jsonErrors).length > 0;
  const editable = !readOnly && fields.some((f) => !isLocked(f));

  // Server dry run of the change before Save is offered (UX-049): refusals land on their fields.
  const checkKey = editable && dirty && !hasInvalid && !hasJsonErrors ? `${kind}/${item.metadata.name}:${JSON.stringify(specToSend())}` : null;
  const check = useServerCheckWith(checkKey, (opts) =>
    updateDashboardResource(kind, item.metadata.name, namespace, { spec: specToSend() }, opts),
  );
  const fieldMap = Object.fromEntries(fields.map((f) => [`spec.${f.path}`, f.path]));
  const fieldErrors = fieldErrorsFrom(check.error, fieldMap);
  const checkText = check.error ? errorText(check.error, t, { action: 'action' }) : null;

  const save = async () => {
    if (!check.passed) return;
    setSaving(true);
    setError(null);
    setSaved(false);
    try {
      await updateDashboardResource(kind, item.metadata.name, namespace, { spec: specToSend() });
      setSaved(true);
      onSaved();
    } catch (err) {
      const txt = errorText(err, t, { action: 'action' });
      setError([txt.title, txt.body].filter(Boolean).join(' '));
    } finally {
      setSaving(false);
    }
  };

  const reset = () => {
    setDraft({ ...original });
    setJsonText({});
    setJsonErrors({});
    setError(null);
  };

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
        const base = field.helpKey ? t(field.helpKey) : undefined;
        const help =
          field.lockedWhileStatus && !readOnly && statusLocked(field)
            ? t(field.lockedWhileStatus.hintKey)
            : field.immutable && !readOnly
            ? t('form.immutableHint')
            : field.immutableWhenSet && locked && !readOnly
              ? t('form.immutableOnceSetHint')
              : field.accessList
                ? [base, !readOnly && accessLocked(field) ? t('form.roles.accessListLocked') : t('form.roles.accessListRule')]
                    .filter(Boolean)
                    .join(' ')
                : field.namespaceEditOnly && !readOnly && accessLocked(field)
                  ? [base, t('form.roles.assignmentAccessLocked')].filter(Boolean).join(' ')
                  : base;

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
          // The raw text stays while typing (it used to snap back on every invalid keystroke, UX-049).
          const text =
            jsonText[field.path] ?? (typeof raw === 'string' ? raw : raw == null ? '' : JSON.stringify(raw, null, 2));
          return (
            <FormGroup key={field.path} label={label} fieldId={id}>
              <TextArea
                id={id}
                value={text}
                isDisabled={locked}
                validated={jsonErrors[field.path] ? 'error' : 'default'}
                onChange={(_e, v) => {
                  setJsonText((prev) => ({ ...prev, [field.path]: v }));
                  try {
                    setField(field, v.trim() ? JSON.parse(v) : undefined);
                    setJsonErrors(({ [field.path]: _drop, ...rest }) => rest);
                  } catch {
                    setJsonErrors((prev) => ({ ...prev, [field.path]: t('form.invalidJson', { field: label }) }));
                  }
                }}
                rows={6}
              />
              {jsonErrors[field.path] && (
                <p className="sc-field-error" style={{ color: 'var(--pf-t--global--text--color--status--danger--default)' }}>
                  {jsonErrors[field.path]}
                </p>
              )}
              {hint}
            </FormGroup>
          );
        }

        if (field.widget === 'rbacRefList') {
          const list = Array.isArray(raw) ? raw.map(String) : [];
          return (
            <RbacMultiSelect
              key={field.path}
              id={id}
              label={label}
              value={list}
              onChange={(next) => setField(field, next)}
              options={rbacOptions}
              helperText={help}
              error={fieldErrors.byField[field.path]}
              isDisabled={locked}
            />
          );
        }

        if (field.widget === 'allowedSites') {
          const list = Array.isArray(raw) ? raw.map(String) : [];
          return (
            <AllowedSitesSelect
              key={field.path}
              id={id}
              tenant={item.metadata.name}
              label={label}
              value={list}
              onChange={(next) => setField(field, next)}
              error={fieldErrors.byField[field.path]}
              isDisabled={locked}
            />
          );
        }

        if (field.widget === 'rbacRef' || field.widget === 'refName') {
          return (
            <RefNameField
              key={field.path}
              id={id}
              label={label}
              refKind={field.widget === 'rbacRef' ? 'Rbac' : (field.refKind ?? 'Rbac')}
              namespace={namespace}
              value={String(raw ?? '')}
              disabled={locked}
              emptyLabel={t('form.optionalNone')}
              hint={hint}
              error={fieldErrors.byField[field.path]}
              onChange={(v) => setField(field, v)}
            />
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
      {checkText && (
        <Alert variant="danger" isInline isPlain className="sc-mb" title={t('form.serverRefused')}>
          {[checkText.body || checkText.title, ...causeLines(fieldErrors.other)].filter(Boolean).join(' ')}
        </Alert>
      )}
      {check.warnings.length > 0 && (
        <Alert variant="warning" isInline isPlain className="sc-mb" title={t('form.apiWarnings')}>
          <ul>
            {check.warnings.map((w) => (
              <li key={w}>{w}</li>
            ))}
          </ul>
        </Alert>
      )}
      {saved && !dirty && <Alert variant="success" isInline isPlain className="sc-mb" title={t('form.saved')} />}
      {!readOnly && (
        <div style={{ display: 'flex', gap: '0.5rem', alignItems: 'center' }}>
          <Button
            variant="primary"
            isAriaDisabled={saving || !editable || hasInvalid || hasJsonErrors || !dirty || !check.passed}
            isLoading={saving || (dirty && check.checking)}
            onClick={() => void save()}
          >
            {saving ? t('form.saving') : t('common.save')}
          </Button>
          <Button variant="link" isDisabled={!dirty || saving} onClick={reset}>
            {t('form.reset')}
          </Button>
          {dirty && check.checking && <span className="sc-text-muted">{t('form.checking')}</span>}
        </div>
      )}
    </Form>
  );
}
