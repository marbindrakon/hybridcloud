import '../consoleK8sBootstrap';
import * as React from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { Alert, PageSection } from '@patternfly/react-core';
import { HybridSovereignKind, ResourceDetail } from '@hybridsovereign/shared';
import '@hybridsovereign/shared/styles/openshift.css';

const ENTITY_NS = 'sovereign-cloud';

export function makeAdminDetailPage(
  kind: HybridSovereignKind,
  title: string,
  listPath: string,
  fixedNamespace?: string,
): React.FC {
  const Page: React.FC = () => {
    const navigate = useNavigate();
    const { namespace: nsParam, name } = useParams<{ namespace?: string; name: string }>();
    const resourceName = name ? decodeURIComponent(name) : '';
    const namespace = fixedNamespace ?? (nsParam ? decodeURIComponent(nsParam) : '');

    if (!resourceName) {
      return (
        <PageSection className="sc-console-page">
          <Alert variant="warning" title="Missing resource name" isInline />
        </PageSection>
      );
    }

    if (!namespace) {
      return (
        <PageSection className="sc-console-page">
          <Alert variant="warning" title="Missing namespace" isInline />
        </PageSection>
      );
    }

    return (
      <PageSection className="sc-console-page">
        <div className="sc-page">
          <ResourceDetail
            kind={kind}
            name={resourceName}
            namespace={namespace}
            parentTitle={title}
            parentPath={listPath}
            onBack={() => navigate(listPath)}
            onDeleted={() => navigate(listPath)}
          />
        </div>
      </PageSection>
    );
  };
  Page.displayName = `Admin${kind}DetailPage`;
  return Page;
}

export default makeAdminDetailPage('Team', 'Teams', '/hybridsovereign/teams');
