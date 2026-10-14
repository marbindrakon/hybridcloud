import '../consoleK8sBootstrap';
import { makeTenantDetailPage } from './TenantTeamDetailPage';

export const TenantProjectDetailPage = makeTenantDetailPage('Project', 'Projects', 'projects');
export const TenantPlatformDetailPage = makeTenantDetailPage(
  'PlatformOpenshift',
  'Platform Openshift',
  'platforms',
);
export const TenantAssignmentDetailPage = makeTenantDetailPage(
  'Assignment',
  'Assignments',
  'assignments',
);
export const TenantCloudOSODetailPage = makeTenantDetailPage('CloudOSO', 'Cloud OSO', 'cloudoso');
export const TenantCloudAWSDetailPage = makeTenantDetailPage('CloudAWS', 'Cloud AWS', 'cloudaws');
export const TenantCloudVirtDetailPage = makeTenantDetailPage('CloudVirt', 'Cloud Virt', 'cloudvirt');
export const TenantMigrationDetailPage = makeTenantDetailPage(
  'OpenStackMigration',
  'Migrate to OpenStack',
  'migrations',
);
export const TenantVaultDetailPage = makeTenantDetailPage('Vault', 'Vaults', 'vaults');
export const TenantVaultKVDetailPage = makeTenantDetailPage('VaultKV', 'Vault KVs', 'vaultkvs');
export const TenantAAPOrgDetailPage = makeTenantDetailPage('AAPOrg', 'AAP Orgs', 'aaporgs');
export const TenantQuayOrgDetailPage = makeTenantDetailPage('QuayOrg', 'Quay Orgs', 'quayorgs');
export const TenantPersonaDetailPage = makeTenantDetailPage('Persona', 'Personas', 'personas');
export const TenantRbacDetailPage = makeTenantDetailPage('Rbac', 'RBAC', 'rbac');
export const TenantHybridNetworkDetailPage = makeTenantDetailPage(
  'HybridNetwork',
  'Hybrid Networks',
  'networks',
);
export const TenantNetworkPlacementDetailPage = makeTenantDetailPage(
  'NetworkPlacement',
  'Network Placements',
  'placements',
);

export { default as TenantTeamDetailPage } from './TenantTeamDetailPage';
