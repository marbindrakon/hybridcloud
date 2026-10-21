import React, { useEffect } from 'react';
import { Button, EmptyState, EmptyStateBody, EmptyStateFooter, EmptyStateActions, Spinner } from '@patternfly/react-core';
import { LockIcon, UserIcon } from '@patternfly/react-icons';
import { useTranslation } from '../i18n';
import { useOptionalScope } from './ScopeProvider';
import { tenantLabel } from './resolveIdentity';

// EmptyState takes an icon component; the console plugins compile shared code
// against another @types/react, so the component types are passed untyped.
// eslint-disable-next-line @typescript-eslint/no-explicit-any
const iconOf = (c: unknown): any => c;

/** Signed in, but no group points at a tenant (design 5.3). */
export function NoRoleState(): React.ReactElement {
  const { t } = useTranslation();
  const ctx = useOptionalScope();
  return (
    <EmptyState titleText={t('scope.noRoleTitle')} headingLevel="h1" icon={iconOf(UserIcon)}>
      <EmptyStateBody>
        <p>{t('scope.noRoleBody')}</p>
        <p>{t('scope.signedInAs', { user: ctx?.identity?.username ?? '' })}</p>
        <p>{t('scope.newGroupHint')}</p>
      </EmptyStateBody>
    </EmptyState>
  );
}

/** A link to a tenant (or scope) the user may not open (UX-005). */
export function NoAccessState({ requested }: { requested?: string }): React.ReactElement {
  const { t } = useTranslation();
  const ctx = useOptionalScope();
  const name = requested ?? (ctx?.scope.kind === 'none' ? ctx.scope.requested : undefined) ?? '';
  const label =
    name === 'all' ? t('scope.allTenants') : name === 'platform' ? t('scope.platform') : name;
  useEffect(() => {
    if (!(window as { SERVER_FLAGS?: unknown }).SERVER_FLAGS) {
      document.title = [t('scope.noAccessTitle', { tenant: label }), t('nav.sovereignCloud')].join(' · ');
    }
  }, [t, label]);
  const others = ctx?.tenants ?? [];
  return (
    <EmptyState titleText={t('scope.noAccessTitle', { tenant: label })} headingLevel="h1" icon={iconOf(LockIcon)}>
      <EmptyStateBody>
        <p>{t('scope.noAccessBody')}</p>
        <p>{t('scope.newGroupHint')}</p>
      </EmptyStateBody>
      {others.length > 0 && (
        <EmptyStateFooter>
          <EmptyStateActions>
            {others.slice(0, 5).map((o) => (
              <Button key={o.name} variant="link" onClick={() => ctx?.select(o.name)}>
                {t('scope.openTenant', { tenant: tenantLabel(o) })}
              </Button>
            ))}
          </EmptyStateActions>
        </EmptyStateFooter>
      )}
    </EmptyState>
  );
}

/** Several tenants and nothing chosen yet. */
export function ChooseTenantState(): React.ReactElement {
  const { t } = useTranslation();
  const ctx = useOptionalScope();
  return (
    <EmptyState titleText={t('scope.chooseTitle')} headingLevel="h1">
      <EmptyStateBody>{t('scope.chooseBody')}</EmptyStateBody>
      <EmptyStateFooter>
        <EmptyStateActions>
          {ctx?.identity?.platform.reader && (
            <Button variant="secondary" onClick={() => ctx.select('all')}>
              {t('scope.allTenants')}
            </Button>
          )}
          {(ctx?.tenants ?? []).map((o) => (
            <Button key={o.name} variant="link" onClick={() => ctx?.select(o.name)}>
              {tenantLabel(o)}
            </Button>
          ))}
        </EmptyStateActions>
      </EmptyStateFooter>
    </EmptyState>
  );
}

/** Identity still resolving. */
export function ScopeLoadingState(): React.ReactElement {
  const { t } = useTranslation();
  return (
    <EmptyState titleText={t('scope.loading')} headingLevel="h1" icon={iconOf(Spinner)}>
      <EmptyStateBody>{t('scope.loadingBody')}</EmptyStateBody>
    </EmptyState>
  );
}

/**
 * Render children only when the scope is usable; otherwise the matching state
 * (loading, no role, no access, choose a tenant).
 */
export function ScopeGate({ children }: { children?: React.ReactNode }): React.ReactElement {
  const ctx = useOptionalScope();
  if (!ctx) return <>{children}</>;
  const { scope } = ctx;
  if (scope.kind !== 'none') return <>{children}</>;
  if (scope.reason === 'loading') return <ScopeLoadingState />;
  if (scope.reason === 'no-role') return <NoRoleState />;
  if (scope.reason === 'no-access') return <NoAccessState requested={scope.requested} />;
  return <ChooseTenantState />;
}
