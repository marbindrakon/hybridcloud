/**
 * Map the API's refusal of a form's object to the form's fields (Q8, UX-040).
 *
 * The API (and the tenant dashboard server, which passes 4xx answers through)
 * names the offending fields in `details.causes[].field`, for example
 * `spec.ipv4.prefix` or `.spec.toolRbac.editRbac[0]`. Forms register which of
 * their inputs writes which spec path; a cause is shown under that input, the
 * rest in the form's error summary. Texts for whole errors come from
 * access/errorText (one table for the UI).
 */
import { isK8sApiError, type K8sStatusCause } from '../hooks/errors';

/** Normalise a cause field to a dotted path without list indexes ("spec.toolRbac.editRbac"). */
export function normaliseFieldPath(field: string | undefined): string {
  if (!field) return '';
  return field
    .trim()
    .replace(/^\./, '')
    .replace(/\[[^\]]*\]/g, '')
    .replace(/\.+/g, '.')
    .replace(/\.$/, '');
}

export interface FieldErrors {
  /** Message per form field id. */
  byField: Record<string, string>;
  /** Causes no field claims (shown in the form's summary). */
  other: K8sStatusCause[];
}

/**
 * Distribute the causes of an API error over the form's fields. `fieldMap` maps
 * a spec path prefix ("spec.ipv4", "metadata.name") to a field id; the longest
 * matching prefix wins.
 */
export function fieldErrorsFrom(err: unknown, fieldMap: Record<string, string>): FieldErrors {
  const out: FieldErrors = { byField: {}, other: [] };
  if (!isK8sApiError(err) || !err.causes?.length) return out;
  const prefixes = Object.keys(fieldMap).sort((a, b) => b.length - a.length);
  for (const cause of err.causes) {
    const path = normaliseFieldPath(cause.field);
    const hit = path ? prefixes.find((p) => path === p || path.startsWith(`${p}.`)) : undefined;
    if (hit) {
      const id = fieldMap[hit];
      out.byField[id] = out.byField[id] ? `${out.byField[id]} ${cause.message}` : cause.message;
    } else {
      out.other.push(cause);
    }
  }
  return out;
}

/** "spec.ipv4.prefix: must be /16 to /29" lines for causes no field claims. */
export function causeLines(causes: K8sStatusCause[]): string[] {
  return causes.map((c) => (c.field ? `${normaliseFieldPath(c.field)}: ${c.message}` : c.message));
}
