import '../consoleK8sBootstrap';
import * as React from 'react';
import { Component, type ReactNode } from 'react';
import { Alert, Button, PageSection } from '@patternfly/react-core';
import { SyncIcon } from '@patternfly/react-icons';
import {
  NetworkOverview,
  PageHeader,
  useOverviewCRs,
  useTranslation,
  type HybridSovereignKind,
  type K8sResource,
} from '@hybridsovereign/shared';
import { objectHref } from '../adminPaths';
import '@hybridsovereign/shared/styles/openshift.css';

/** Keep the page usable if the SVG/matrix subtree throws. */
class SectionErrorBoundary extends Component<
  { title: string; children: ReactNode },
  { error: Error | null }
> {
  state: { error: Error | null } = { error: null };

  static getDerivedStateFromError(error: Error) {
    return { error };
  }

  render() {
    if (this.state.error) {
      return (
        <Alert variant="warning" isInline title={this.props.title}>
          {this.state.error.message || 'Section failed to render'}
        </Alert>
      );
    }
    return this.props.children;
  }
}

export default function AdminNetworkOverviewPage(): React.ReactElement {
  const { t } = useTranslation();
  const { items, loading, error, refresh } = useOverviewCRs(0);

  const onSelect = (kind: HybridSovereignKind, item: K8sResource) => {
    const href = objectHref(kind, item);
    if (href) window.location.href = href;
  };

  return (
    <>
      <PageHeader
        title={t('nav.networkOverview')}
        subtitle={t('pages.networkOverviewDesc')}
        actions={
          <Button variant="secondary" icon={<SyncIcon />} onClick={refresh} isDisabled={loading}>
            {t('common.refresh')}
          </Button>
        }
      />
      <PageSection>
        <SectionErrorBoundary title="Network overview failed to render">
          <NetworkOverview
            resources={items}
            resourcesLoading={loading}
            resourcesError={error}
            onSelect={onSelect}
          />
        </SectionErrorBoundary>
      </PageSection>
    </>
  );
}
