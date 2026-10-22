import React from 'react';
import { Alert, Button, EmptyState, EmptyStateActions, EmptyStateBody, EmptyStateFooter } from '@patternfly/react-core';
import { LockIcon } from '@patternfly/react-icons';
import { useTranslation } from '../i18n';
import { KIND_PLURALS, type HybridSovereignKind } from '../types';
import { useOptionalScope } from '../scope/ScopeProvider';
import { errorText, roleHintText } from './errorText';
import { hintRoles } from './roleHints';

// eslint-disable-next-line @typescript-eslint/no-explicit-any
const iconOf = (c: unknown): any => c;

function useScopeLabels(): { tenantLabel?: string; contacts?: { name?: string; email?: string; url?: string }[] } {
  const { t } = useTranslation();
  const ctx = useOptionalScope();
  const s = ctx?.scope;
  if (s?.kind === 'tenant') return { tenantLabel: s.tenant.displayName || s.tenant.name, contacts: s.tenant.contacts };
  if (s?.kind === 'all') return { tenantLabel: t('scope.allTenants') };
  if (s?.kind === 'global') return { tenantLabel: t('scope.platform') };
  return {};
}

/**
 * "You don't have access to VM environments in Acme Corporation. ... grants it.
 * Contacts: ..." - a forbidden list is never shown as an empty one. With
 * `name`, the same state for one object's page (GT-F5): the roles that grant
 * reading that kind and the tenant's contacts.
 */
export function ForbiddenState({
  kind,
  name,
  compact = false,
}: {
  kind: HybridSovereignKind;
  /** The object a detail page could not read */
  name?: string;
  compact?: boolean;
}): React.ReactElement {
  const { t } = useTranslation();
  const { tenantLabel, contacts } = useScopeLabels();
  const kindLabel = t(`kinds.${kind}`);
  const oneLabel = t(`kindsOne.${kind}`, { defaultValue: kind });
  const title = name
    ? tenantLabel
      ? t('errors.forbiddenObject', { kind: oneLabel, name, tenant: tenantLabel })
      : t('errors.forbiddenObjectNoTenant', { kind: oneLabel, name })
    : tenantLabel
      ? t('errors.forbiddenList', { kind: kindLabel, tenant: tenantLabel })
      : t('errors.forbiddenKind', { kind: kindLabel });
  const hint = roleHintText(t, {
    roleNames: hintRoles(KIND_PLURALS[kind], name ? 'get' : 'list').map((r) => t(`roles.${r}`)),
    contacts,
  });
  if (compact) {
    return (
      <Alert variant="info" isInline title={title} className="sc-mb" customIcon={<LockIcon />}>
        {[hint, t('scope.newGroupHint')].filter(Boolean).join(' ')}
      </Alert>
    );
  }
  return (
    <EmptyState titleText={title} headingLevel="h2" icon={iconOf(LockIcon)} className="sc-forbidden">
      <EmptyStateBody>
        {hint && <p>{hint}</p>}
        <p>{t('scope.newGroupHint')}</p>
      </EmptyStateBody>
    </EmptyState>
  );
}

/** Any other failure of a list or page: the section 6 text, with Retry. */
export function UnavailableState({
  error,
  kind,
  onRetry,
}: {
  error: unknown;
  kind?: HybridSovereignKind;
  onRetry?: () => void;
}): React.ReactElement {
  const { t } = useTranslation();
  const { tenantLabel, contacts } = useScopeLabels();
  const txt = errorText(error, t, {
    kindLabel: kind ? t(`kinds.${kind}`) : undefined,
    tenantLabel,
    contacts,
    action: 'list',
  });
  const variant = txt.cls === 'notFound' || txt.cls === 'conflict' ? 'info' : 'warning';
  return (
    <Alert variant={variant} isInline title={txt.title} className="sc-mb">
      {txt.body && <p>{txt.body}</p>}
      {onRetry && (
        <Button variant="link" isInline onClick={onRetry}>
          {t('errors.retry')}
        </Button>
      )}
    </Alert>
  );
}

/** EmptyState form of UnavailableState for whole pages. */
export function UnavailablePage({ error, onRetry }: { error: unknown; onRetry?: () => void }): React.ReactElement {
  const { t } = useTranslation();
  const txt = errorText(error, t, {});
  return (
    <EmptyState titleText={txt.title} headingLevel="h1">
      {txt.body && <EmptyStateBody>{txt.body}</EmptyStateBody>}
      {onRetry && (
        <EmptyStateFooter>
          <EmptyStateActions>
            <Button variant="primary" onClick={onRetry}>
              {t('errors.retry')}
            </Button>
          </EmptyStateActions>
        </EmptyStateFooter>
      )}
    </EmptyState>
  );
}
