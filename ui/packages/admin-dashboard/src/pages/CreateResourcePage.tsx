import React from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { Alert } from '@patternfly/react-core';
import { CreateResourceForm, SelfServiceFormType, useTranslation } from '@hybridsovereign/shared';

const LIST_PATH: Record<string, string> = {
  entity: '/entities',
  persona: '/personas',
  hybridfabric: '/networking/fabrics',
  cloudinfrastructure: '/clouds/infrastructure',
  cloudgateway: '/networking/gateways',
  transportlink: '/networking/transport',
  uihealthchecker: '/networking/uihealth',
};

export function CreateResourcePage(): React.ReactElement {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const { kind } = useParams<{ kind: string }>();
  const type = (kind || 'entity') as SelfServiceFormType;
  const listPath = LIST_PATH[type] ?? '/';

  if (!kind || !(kind in LIST_PATH)) {
    return (
      <Alert variant="danger" isInline title={t('common.pageNotFound')}>
        Unknown create kind: {kind}
      </Alert>
    );
  }

  return (
    <CreateResourceForm
      formType={type}
      namespace={type === 'persona' ? '' : 'sovereign-cloud'}
      listPath={listPath}
      onSuccess={(path) => navigate(path)}
      onCancel={() => navigate(listPath)}
    />
  );
}
