import { consoleFetch } from '@openshift-console/dynamic-plugin-sdk';
import { configureK8sClient, configurePermissionsClient, configureUiConfigUrl } from '@hybridsovereign/shared';

/**
 * Must run in every exposed console page module.
 * OpenShift loads codeRefs as separate MF chunks — plugin.ts side effects do not
 * run for those chunks, so without this the client stays on default `/api/k8s`
 * and Overview KPIs come back empty.
 */
configureK8sClient({
  baseUrl: '/api/kubernetes',
  fetchFn: consoleFetch as unknown as typeof fetch,
  apiStyle: 'raw',
});

configurePermissionsClient('/api/kubernetes', {
  style: 'ssar',
  fetchFn: consoleFetch as unknown as typeof fetch,
});

// The installation's settings (help pages, the automation job link) come from the admin dashboard's
// server, through the console's proxy to it (the alias the plugin's chart declares).
configureUiConfigUrl('/api/proxy/plugin/sovereign-admin-plugin/uihealth/api/ui-config');
