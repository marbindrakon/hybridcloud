import '../consoleK8sBootstrap';
import * as React from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { Alert, PageSection } from '@patternfly/react-core';
import {
  HybridSovereignKind,
  NamespaceContextBar,
  ResourceDetail,
  useEntityNamespace,
  configureK8sClient,
  useTranslation,
} from '@hybridsovereign/shared';
import { consoleFetch } from '@openshift-console/dynamic-plugin-sdk';
import '@hybridsovereign/shared/styles/openshift.css';

configureK8sClient({
  baseUrl: '/api/kubernetes',
  fetchFn: consoleFetch as unknown as typeof fetch,
  apiStyle: 'raw',
});

export function makeTenantDetailPage(
  kind: HybridSovereignKind,
  title: string,
  listSegment: string,
): React.FC {
  const listPath = `/hybridsovereign/tenant/${listSegment}`;
  const Page: React.FC = () => {
    const navigate = useNavigate();
    const { name } = useParams<{ name: string }>();
    const { namespace, entities, selectEntity, entity } = useEntityNamespace();
    const resourceName = name ? decodeURIComponent(name) : '';

    return (
      <PageSection className="sc-console-page">
        <div className="sc-page">
          <NamespaceContextBar
            namespace={namespace}
            entityName={entity?.metadata.name}
            entities={entities}
            onSelectEntity={selectEntity}
          />
          {!namespace ? (
            <Alert variant="info" title="Select an entity" isInline>
              Choose an entity namespace from the context bar to view this resource.
            </Alert>
          ) : !resourceName ? (
            <Alert variant="warning" title="Missing resource name" isInline>
              No resource name in the URL.
            </Alert>
          ) : (
            <ResourceDetail
              kind={kind}
              name={resourceName}
              namespace={namespace}
              parentTitle={title}
              parentPath={listPath}
              onBack={() => navigate(listPath)}
              onDeleted={() => navigate(listPath)}
            />
          )}
        </div>
      </PageSection>
    );
  };
  Page.displayName = `Tenant${kind}DetailPage`;
  return Page;
}

const TenantTeamDetailPage = makeTenantDetailPage('Team', 'Teams', 'teams');
export default TenantTeamDetailPage;
