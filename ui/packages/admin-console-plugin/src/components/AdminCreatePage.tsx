import '../consoleK8sBootstrap';
import * as React from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { Alert, PageSection } from '@patternfly/react-core';
import {
  CreateResourceForm,
  NamespaceContextBar,
  SelfServiceFormType,
  useEntityNamespace,
  useTranslation,
} from '@hybridsovereign/shared';
import '@hybridsovereign/shared/styles/openshift.css';

/** Forms that live in sovereign-cloud (no entity namespace picker). */
const PLATFORM_NS_FORMS = new Set<SelfServiceFormType>([
  'entity',
  'hybridfabric',
  'cloudinfrastructure',
  'cloudgateway',
  'transportlink',
  'uihealthchecker',
]);

const LIST_PATH: Partial<Record<SelfServiceFormType, string>> = {
  entity: '/hybridsovereign/entities',
  persona: '/hybridsovereign/personas',
  team: '/hybridsovereign/teams',
  project: '/hybridsovereign/projects',
  assignment: '/hybridsovereign/assignments',
  platformopenshift: '/hybridsovereign/platforms',
  cloudoso: '/hybridsovereign/tenant/cloudoso',
  cloudaws: '/hybridsovereign/tenant/cloudaws',
  cloudvirt: '/hybridsovereign/tenant/cloudvirt',
  migration: '/hybridsovereign/tenant/migrations',
  rbac: '/hybridsovereign/tenant/rbac',
  vault: '/hybridsovereign/tenant/vaults',
  vaultkv: '/hybridsovereign/tenant/vaultkvs',
  aaporg: '/hybridsovereign/tenant/aaporgs',
  quayorg: '/hybridsovereign/tenant/quayorgs',
  hybridnetwork: '/hybridsovereign/tenant/networks',
  networkplacement: '/hybridsovereign/tenant/placements',
  hybridfabric: '/hybridsovereign/networking/fabrics',
  cloudinfrastructure: '/hybridsovereign/clouds/infrastructure',
  cloudgateway: '/hybridsovereign/networking/gateways',
  transportlink: '/hybridsovereign/networking/transport',
  uihealthchecker: '/hybridsovereign/networking/uihealth',
};

const SUPPORTED_FORMS = new Set<string>(Object.keys(LIST_PATH));

const AdminCreatePage: React.FC = () => {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const { kind } = useParams<{ kind: string }>();
  const formType = (kind ?? 'entity') as SelfServiceFormType;
  const listPath = LIST_PATH[formType] ?? '/hybridsovereign/overview';
  const { namespace, entities, selectEntity, entity } = useEntityNamespace();
  const isPlatform = PLATFORM_NS_FORMS.has(formType);
  // Persona can pick entity inline in the form when namespace is empty.
  const needsEntityBar = !isPlatform && formType !== 'persona';

  if (!SUPPORTED_FORMS.has(formType)) {
    return (
      <PageSection className="sc-console-page">
        <Alert variant="warning" title="Unsupported create form" isInline>
          Unknown kind: {kind}
        </Alert>
      </PageSection>
    );
  }

  return (
    <PageSection className="sc-console-page">
      <div className="sc-page">
        {needsEntityBar && (
          <NamespaceContextBar
            namespace={namespace}
            entityName={entity?.metadata.name}
            entities={entities}
            onSelectEntity={selectEntity}
          />
        )}
        {needsEntityBar && !namespace ? (
          <Alert variant="info" title={t('common.selectEntity', { defaultValue: 'Select an entity' })} isInline>
            Choose an entity namespace before creating a resource.
          </Alert>
        ) : (
          <CreateResourceForm
            formType={formType}
            namespace={isPlatform ? 'sovereign-cloud' : formType === 'persona' ? '' : namespace}
            listPath={listPath}
            onSuccess={(path) => navigate(path)}
            onCancel={() => navigate(listPath)}
          />
        )}
      </div>
    </PageSection>
  );
};

export default AdminCreatePage;
