import '../consoleK8sBootstrap';
import * as React from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { PageSection } from '@patternfly/react-core';
import { ResourceDetail,
  useTranslation,
} from '@hybridsovereign/shared';
import '@hybridsovereign/shared/styles/openshift.css';

const ENTITY_NS = 'sovereign-cloud';

const AdminEntityDetailPage: React.FC = () => {
  const navigate = useNavigate();
  const { name } = useParams<{ name: string }>();
  const resourceName = name ? decodeURIComponent(name) : '';
  const listPath = '/hybridsovereign/entities';

  return (
    <PageSection className="sc-console-page">
      <div className="sc-page">
        <ResourceDetail
          kind="Entity"
          name={resourceName}
          namespace={ENTITY_NS}
          parentTitle="Entities"
          parentPath={listPath}
          onBack={() => navigate(listPath)}
          onDeleted={() => navigate(listPath)}
        />
      </div>
    </PageSection>
  );
};

export default AdminEntityDetailPage;
