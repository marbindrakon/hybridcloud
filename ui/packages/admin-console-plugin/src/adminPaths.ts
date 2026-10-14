import { HybridSovereignKind, K8sResource } from '@hybridsovereign/shared';

const PREFIX = '/hybridsovereign';

/** Console paths aligned with standalone admin dashboard routes. */
export const ADMIN_LIST_PATH: Partial<Record<HybridSovereignKind, string>> = {
  Entity: `${PREFIX}/entities`,
  Team: `${PREFIX}/teams`,
  Project: `${PREFIX}/projects`,
  Assignment: `${PREFIX}/assignments`,
  PlatformOpenshift: `${PREFIX}/platforms`,
  CloudOSO: `${PREFIX}/clouds/cloudoso`,
  CloudAWS: `${PREFIX}/clouds/cloudaws`,
  CloudVirt: `${PREFIX}/clouds/cloudvirt`,
  CloudInfrastructure: `${PREFIX}/clouds/infrastructure`,
  Persona: `${PREFIX}/personas`,
  Rbac: `${PREFIX}/operators/rbacs`,
  RbacConfig: `${PREFIX}/operators/rbacconfigs`,
  AAPOrg: `${PREFIX}/services/aaporgs`,
  AAPConfig: `${PREFIX}/services/aapconfigs`,
  QuayOrg: `${PREFIX}/services/quayorgs`,
  QuayConfig: `${PREFIX}/services/quayconfigs`,
  Vault: `${PREFIX}/services/vaults`,
  VaultKV: `${PREFIX}/services/vaultkvs`,
  HybridFabric: `${PREFIX}/networking/fabrics`,
  CloudGateway: `${PREFIX}/networking/gateways`,
  TransportLink: `${PREFIX}/networking/transport`,
  UIHealthChecker: `${PREFIX}/networking/uihealth`,
};

const CLUSTER_SCOPED: HybridSovereignKind[] = [
  'Entity',
  'CloudInfrastructure',
  'HybridFabric',
  'CloudGateway',
  'TransportLink',
  'UIHealthChecker',
];

export function consoleAdminDetailHref(
  listPath: string,
  kind: HybridSovereignKind,
  item: K8sResource,
): string {
  const name = encodeURIComponent(item.metadata.name);
  if (CLUSTER_SCOPED.includes(kind)) {
    return `${listPath}/${name}`;
  }
  const ns = encodeURIComponent(item.metadata.namespace ?? 'default');
  return `${listPath}/${ns}/${name}`;
}

export function overviewItemHref(item: K8sResource): string | null {
  const kind = (item.kind || '') as HybridSovereignKind;
  const listPath = ADMIN_LIST_PATH[kind];
  if (!listPath) return null;
  return consoleAdminDetailHref(listPath, kind, item);
}
