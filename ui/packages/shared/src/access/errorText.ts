/**
 * What the user reads for an error (design section 6). Pure: callers pass
 * `t` and the context (kind label, tenant label, contacts, role names).
 */
import { classifyError, isK8sApiError, type ErrorClass } from '../hooks/errors';

export { classifyError };
export type { ErrorClass };

export interface ErrorTextContext {
  /** Plural label of the kind ("VM environments"). */
  kindLabel?: string;
  /** Tenant display name, or "All tenants". */
  tenantLabel?: string;
  /** Plain role names that grant the missing permission. */
  roleNames?: string[];
  /** Tenant contacts (from the TenantProfile). */
  contacts?: { name?: string; email?: string; url?: string }[];
  /** What the user tried: 'list' (a page of objects) or 'action' (a button). */
  action?: 'list' | 'action' | 'get';
}

type T = (key: string, opts?: Record<string, unknown>) => string;

export function contactsText(contacts: ErrorTextContext['contacts']): string {
  return (contacts ?? [])
    .map((c) => [c.name, c.email].filter(Boolean).join(' '))
    .filter(Boolean)
    .join(', ');
}

/** One sentence naming the roles that grant the permission and the contacts. */
export function roleHintText(t: T, ctx: ErrorTextContext): string {
  const parts: string[] = [];
  if (ctx.roleNames?.length) {
    parts.push(t('access.grantedBy', { roles: ctx.roleNames.join(t('access.or')) }));
  }
  const contacts = contactsText(ctx.contacts);
  if (contacts) parts.push(t('access.contacts', { contacts }));
  return parts.join(' ');
}

export interface ErrorTexts {
  cls: ErrorClass;
  title: string;
  body: string;
}

export function errorText(err: unknown, t: T, ctx: ErrorTextContext = {}): ErrorTexts {
  const cls = classifyError(err);
  const tenant = ctx.tenantLabel ?? '';
  switch (cls) {
    case 'unauthenticated':
      return { cls, title: t('errors.sessionEnded'), body: t('errors.sessionEndedBody') };
    case 'forbidden': {
      const title =
        ctx.action === 'list' || ctx.action === 'get'
          ? ctx.kindLabel && tenant
            ? t('errors.forbiddenList', { kind: ctx.kindLabel, tenant })
            : ctx.kindLabel
              ? t('errors.forbiddenKind', { kind: ctx.kindLabel })
              : t('errors.forbidden')
          : t('errors.forbiddenAction');
      const body = [roleHintText(t, ctx), t('scope.newGroupHint')].filter(Boolean).join(' ');
      return { cls, title, body };
    }
    case 'notFound':
      return { cls, title: t('errors.notFound'), body: '' };
    case 'conflict':
      return { cls, title: t('errors.conflict'), body: '' };
    case 'invalid': {
      const detail = isK8sApiError(err) ? err.detail : err instanceof Error ? err.message : String(err);
      return { cls, title: t('errors.invalid'), body: detail };
    }
    case 'unavailable':
      return { cls, title: t('errors.unavailable'), body: t('errors.unavailableBody') };
    default: {
      const ref = isK8sApiError(err) ? err.reference : undefined;
      return {
        cls,
        title: t('errors.server'),
        body: ref ? t('errors.serverBodyRef', { ref }) : t('errors.serverBody'),
      };
    }
  }
}
