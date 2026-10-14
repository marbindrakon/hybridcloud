import React from 'react';
import { Button, FormGroup, FormSelect, FormSelectOption, TextInput } from '@patternfly/react-core';
import { MinusCircleIcon, PlusCircleIcon } from '@patternfly/react-icons';
import { ListTextArea } from './ListTextArea';

export type ObjectListRow = Record<string, unknown>;

export interface ObjectListColumn {
  key: string;
  label: string;
  /** text (default), number, list (comma-separated strings) or select */
  type?: 'text' | 'number' | 'list' | 'select';
  /** select options; value '' means the key is omitted */
  options?: Array<{ value: string; label: string }>;
  placeholder?: string;
  /** Return false to flag the cell as invalid */
  validate?: (value: unknown, row: ObjectListRow) => boolean;
  /** Disabled on rows that existed before editing started (see existingKeys) */
  lockedOnExisting?: boolean;
}

export interface ObjectListEditorProps {
  id: string;
  label: string;
  columns: ObjectListColumn[];
  value: ObjectListRow[];
  onChange: (next: ObjectListRow[]) => void;
  addLabel: string;
  removeLabel: string;
  /** Field that identifies a row (list-map key) */
  keyField?: string;
  /** Keys of the rows that existed before editing; their lockedOnExisting cells are disabled */
  existingKeys?: string[];
  isRequired?: boolean;
  isDisabled?: boolean;
  maxItems?: number;
  /** Template for a new row */
  newRow?: () => ObjectListRow;
  helperText?: React.ReactNode;
}

function cellValue(row: ObjectListRow, col: ObjectListColumn): string {
  const v = row[col.key];
  if (v == null) return '';
  return String(v);
}

/**
 * Editor for an array of small objects (one row per item, one input per field). Empty inputs drop
 * the key from the item, so optional fields stay unset.
 */
export function ObjectListEditor({
  id,
  label,
  columns,
  value,
  onChange,
  addLabel,
  removeLabel,
  keyField,
  existingKeys = [],
  isRequired,
  isDisabled,
  maxItems,
  newRow,
  helperText,
}: ObjectListEditorProps): React.ReactElement {
  const rows = Array.isArray(value) ? value : [];

  const setCell = (index: number, col: ObjectListColumn, raw: unknown) => {
    const next = rows.map((r, i) => {
      if (i !== index) return r;
      const copy: ObjectListRow = { ...r };
      const empty =
        raw === '' || raw == null || (Array.isArray(raw) && raw.length === 0) || Number.isNaN(raw);
      if (empty) delete copy[col.key];
      else copy[col.key] = raw;
      return copy;
    });
    onChange(next);
  };

  const isExisting = (row: ObjectListRow): boolean =>
    !!keyField && existingKeys.includes(String(row[keyField] ?? ''));

  return (
    <FormGroup label={label} isRequired={isRequired} fieldId={`${id}-0-${columns[0]?.key ?? 'row'}`}>
      <div className="sc-objlist" role="group" aria-label={label}>
        {rows.map((row, index) => {
          const locked = isExisting(row);
          return (
            <div
              key={index}
              className="sc-objlist__row"
              style={{ display: 'flex', gap: '0.5rem', alignItems: 'flex-start', marginBottom: '0.5rem' }}
            >
              {columns.map((col) => {
                const cellId = `${id}-${index}-${col.key}`;
                const disabled = isDisabled || (locked && col.lockedOnExisting);
                const invalid = col.validate ? !col.validate(row[col.key], row) : false;
                const validated = invalid ? 'error' : 'default';
                let input: React.ReactNode;
                if (col.type === 'select') {
                  input = (
                    <FormSelect
                      id={cellId}
                      value={cellValue(row, col)}
                      isDisabled={disabled}
                      validated={validated}
                      aria-label={col.label}
                      onChange={(_e, v) => setCell(index, col, v)}
                    >
                      {(col.options ?? []).map((o) => (
                        <FormSelectOption key={o.value} value={o.value} label={o.label} />
                      ))}
                    </FormSelect>
                  );
                } else if (col.type === 'list') {
                  const list = Array.isArray(row[col.key]) ? (row[col.key] as unknown[]).map(String) : [];
                  input = (
                    <ListTextArea
                      id={cellId}
                      value={list}
                      isDisabled={disabled}
                      placeholder={col.placeholder}
                      rows={1}
                      validated={validated}
                      aria-label={col.label}
                      onChange={(next) => setCell(index, col, next)}
                    />
                  );
                } else {
                  input = (
                    <TextInput
                      id={cellId}
                      type={col.type === 'number' ? 'number' : 'text'}
                      value={cellValue(row, col)}
                      isDisabled={disabled}
                      placeholder={col.placeholder}
                      validated={validated}
                      aria-label={col.label}
                      onChange={(_e, v) =>
                        setCell(index, col, col.type === 'number' ? (v === '' ? '' : Number(v)) : v)
                      }
                    />
                  );
                }
                return (
                  <div key={col.key} style={{ flex: 1, minWidth: 0 }}>
                    {index === 0 && (
                      <label htmlFor={cellId} className="sc-text-muted" style={{ fontSize: '0.75rem' }}>
                        {col.label}
                      </label>
                    )}
                    {input}
                  </div>
                );
              })}
              <Button
                variant="plain"
                aria-label={removeLabel}
                isDisabled={isDisabled}
                onClick={() => onChange(rows.filter((_r, i) => i !== index))}
                style={{ marginTop: index === 0 ? '1.1rem' : 0 }}
                icon={<MinusCircleIcon />}
              />
            </div>
          );
        })}
        <Button
          variant="link"
          icon={<PlusCircleIcon />}
          isInline
          isDisabled={isDisabled || (maxItems !== undefined && rows.length >= maxItems)}
          onClick={() => onChange([...rows, newRow ? newRow() : {}])}
        >
          {addLabel}
        </Button>
      </div>
      {helperText ? (
        <p className="sc-text-muted" style={{ marginTop: '0.25rem' }}>
          {helperText}
        </p>
      ) : null}
    </FormGroup>
  );
}
