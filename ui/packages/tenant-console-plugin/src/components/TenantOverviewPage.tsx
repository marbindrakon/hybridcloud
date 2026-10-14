import '../consoleK8sBootstrap';
import * as React from 'react';
import { PageSection, Title, Card, CardBody } from '@patternfly/react-core';
import {
  EntityTopology,
  PageHeader,
  NamespaceContextBar,
  InventoryCard,
  useK8sResourceList,
  useEntityNamespace,
  K8sResource,
  useTranslation,
} from '@hybridsovereign/shared';
import '@hybridsovereign/shared/styles/openshift.css';


const TenantOverviewPage: React.FC = () => {
  const { t } = useTranslation();
  const { namespace, entities, selectEntity, entity } = useEntityNamespace();

  const teams = useK8sResourceList<K8sResource>('Team', { namespace });
  const projects = useK8sResourceList<K8sResource>('Project', { namespace });
  const platforms = useK8sResourceList<K8sResource>('PlatformOpenshift', { namespace });
  const assignments = useK8sResourceList<K8sResource>('Assignment', { namespace });
  const ready = (items: K8sResource[]) => items.filter((i) => i.status?.ready).length;

  return (
    <PageSection className="sc-console-page">
      <div className="sc-page">
        <NamespaceContextBar
          namespace={namespace}
          entityName={entity?.metadata.name}
          billingId={(entity?.spec as { billingID?: string } | undefined)?.billingID}
          entities={entities}
          onSelectEntity={selectEntity}
        />
        <PageHeader
          title="Entity Overview"
          subtitle="Tenant-scoped health and live topology"
          breadcrumbs={[
            { label: t('nav.sovereignCloud') },
            { label: t('nav.tenancy') },
            { label: t('pages.overviewTitle') },
          ]}
        />
        <div className="sc-inventory-grid sc-mb">
          <InventoryCard
            title={t('nav.teams')}
            count={teams.items.length}
            hint={`${ready(teams.items)} ${t('status.ready')}`}
            kind="Team"
            href="/hybridsovereign/tenant/teams"
          />
          <InventoryCard
            title={t('nav.projects')}
            count={projects.items.length}
            hint={`${ready(projects.items)} ${t('status.ready')}`}
            kind="Project"
            href="/hybridsovereign/tenant/projects"
          />
          <InventoryCard
            title={t('nav.platformOpenshift')}
            count={platforms.items.length}
            hint={`${ready(platforms.items)} ${t('status.ready')}`}
            kind="PlatformOpenshift"
            href="/hybridsovereign/tenant/platforms"
          />
          <InventoryCard
            title={t('nav.assignments')}
            count={assignments.items.length}
            hint={`${ready(assignments.items)} ${t('status.ready')}`}
            kind="Assignment"
            href="/hybridsovereign/tenant/assignments"
          />
        </div>
        <Title headingLevel="h2" size="lg" className="sc-section-title">
          Live entity topology
        </Title>
        <Card isCompact className="sc-panel">
          <CardBody>
            <EntityTopology entityNamespace={namespace} />
          </CardBody>
        </Card>
      </div>
    </PageSection>
  );
};

export default TenantOverviewPage;
