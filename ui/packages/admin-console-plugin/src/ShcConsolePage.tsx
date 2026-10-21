import './consoleK8sBootstrap';
import * as React from 'react';
import { useActiveNamespace } from '@openshift-console/dynamic-plugin-sdk';
import { ConsolePage, IntegrationsPage } from '@hybridsovereign/shared';
import AdminOverviewPage from './components/AdminOverviewPage';
import AdminNetworkOverviewPage from './components/AdminNetworkOverviewPage';
import AdminUIHealthPage from './components/AdminUIHealthPage';
import '@hybridsovereign/shared/styles/openshift.css';

// Shared components are typed against another @types/react: render them untyped.
// eslint-disable-next-line @typescript-eslint/no-explicit-any
const Page = ConsolePage as unknown as React.ComponentType<any>;

const PAGES = {
  'platform-overview': AdminOverviewPage,
  'fabric-map': AdminNetworkOverviewPage,
  integrations: IntegrationsPage,
  'ui-health': AdminUIHealthPage,
};

/** Every platform route of the Sovereign Cloud perspective, and the redirects of the old admin paths. */
export default function ShcConsolePage(): React.ReactElement {
  const [activeNamespace, setActiveNamespace] = useActiveNamespace();
  return React.createElement(Page, { host: 'admin-console', activeNamespace, setActiveNamespace, pages: PAGES });
}
