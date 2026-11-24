/** Types of tenantStatusAllow.js (the status fields tenant pages show, per kind). */
export type TenantStatusEntry = string | [string, TenantStatusEntry[]];
export const COMMON_STATUS_KEYS: string[];
export const TENANT_STATUS_ALLOW: Record<string, TenantStatusEntry[]>;
export const SECRET_KEY_RE: RegExp;
export function scrubSecrets<T>(value: T): T;
export function hasTenantStatusAllow(kind: string): boolean;
export function filterTenantStatus<T>(kind: string, status: T): T;
