import React from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { Alert } from '@patternfly/react-core';
import { useTranslation } from '../i18n';
import { CreateResourceForm, type SelfServiceFormType } from '../components/CreateResourceForm';
import { usePermission } from '../access/PermissionProvider';
import { roleHintText } from '../access/errorText';
import { hintRoles } from '../access/roleHints';
import { KIND_PLURALS } from '../types';
import { entryForCreateForm } from '../nav/registry';
import { useDocumentTitle, useNav, useNavHost } from '../nav/useNav';
import { useOptionalScope } from '../scope/ScopeProvider';
import { isPlatformEntry } from './usePageEntry';

/**
 * The create form of a registry entry. The tenant comes from the scope (the
 * form does not ask for it again); platform forms create in the platform
 * namespace. Forms of other personas or unknown forms answer not found.
 */
export function ScopedCreatePage({ persona }: { persona: 'tenant' | 'platform' }): React.ReactElement {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const nav = useNav();
  const host = useNavHost();
  const ctx = useOptionalScope();
  const { formType } = useParams<{ formType: string }>();
  const entry = formType ? entryForCreateForm(formType) : undefined;
  const platform = isPlatformEntry(entry);
  const kind = entry?.kinds?.[0];
  const namespace = platform ? 'sovereign-cloud' : ctx?.scope.kind === 'tenant' ? ctx.scope.tenant.namespace : undefined;
  const perm = usePermission(kind ?? 'Entity', 'create', { namespace });
  useDocumentTitle(entry?.id ?? 'create');

  if (!entry || !kind || !formType || platform !== (persona === 'platform')) {
    return (
      <Alert variant="danger" isInline title={t('common.pageNotFound')}>
        {formType}
      </Alert>
    );
  }
  if (!namespace) return <Alert variant="info" isInline title={t('scope.choose')} />;
  const listPath = nav.pathFor(entry.id);
  if (!perm.loading && !perm.allowed) {
    return (
      <Alert variant="warning" isInline title={t('access.notAllowed')}>
        {roleHintText(t, {
          roleNames: hintRoles(KIND_PLURALS[kind], 'create').map((r) => t(`roles.${r}`)),
          contacts: perm.reason?.contacts,
        })}
      </Alert>
    );
  }
  return (
    <CreateResourceForm
      formType={formType as SelfServiceFormType}
      namespace={namespace}
      listPath={listPath}
      onSuccess={(path) => navigate(path)}
      onCancel={() => navigate(listPath)}
      audience={host === 'admin-dashboard' || host === 'admin-console' ? 'admin' : 'tenant'}
    />
  );
}
