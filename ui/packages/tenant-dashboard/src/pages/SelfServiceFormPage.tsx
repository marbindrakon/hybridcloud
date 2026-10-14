import React from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { Alert } from '@patternfly/react-core';
import { CreateResourceForm, SelfServiceFormType, useTranslation } from '@hybridsovereign/shared';

const LIST_PATH: Record<string, string> = {
  team: '/teams',
  project: '/projects',
  assignment: '/assignments',
  cloudoso: '/cloudoso',
  cloudaws: '/cloudaws',
  cloudvirt: '/cloudvirt',
  platformopenshift: '/platforms',
  migration: '/migrations',
  persona: '/personas',
  rbac: '/rbac',
  vault: '/vaults',
  vaultkv: '/vaultkvs',
  aaporg: '/aaporgs',
  quayorg: '/quayorgs',
  hybridnetwork: '/networks',
  networkplacement: '/placements',
};

interface SelfServiceFormPageProps {
  namespace: string;
}

export function SelfServiceFormPage({ namespace }: SelfServiceFormPageProps): React.ReactElement {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const { formType } = useParams<{ formType: string }>();
  const type = (formType || 'team') as SelfServiceFormType;
  const listPath = LIST_PATH[type] ?? '/';

  if (!formType || !(formType in LIST_PATH)) {
    return (
      <Alert variant="danger" isInline title={t('common.pageNotFound')}>
        Unknown form type: {formType}
      </Alert>
    );
  }

  return (
    <CreateResourceForm
      formType={type}
      namespace={namespace}
      listPath={listPath}
      onSuccess={(path) => navigate(path)}
      onCancel={() => navigate(listPath)}
    />
  );
}
