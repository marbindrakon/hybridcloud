import './consoleK8sBootstrap';
import * as React from 'react';
import { useActiveNamespace } from '@openshift-console/dynamic-plugin-sdk';
import { ConsolePage } from '@hybridsovereign/shared';
import '@hybridsovereign/shared/styles/openshift.css';

// Shared components are typed against another @types/react: render them untyped.
// eslint-disable-next-line @typescript-eslint/no-explicit-any
const Page = ConsolePage as unknown as React.ComponentType<any>;

/** Every tenant route of the Sovereign Cloud perspective (entry and scope come from the URL). */
export default function ShcConsolePage(): React.ReactElement {
  const [activeNamespace, setActiveNamespace] = useActiveNamespace();
  return React.createElement(Page, { host: 'tenant-console', activeNamespace, setActiveNamespace });
}
