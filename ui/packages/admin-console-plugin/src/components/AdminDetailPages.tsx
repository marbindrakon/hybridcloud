import '../consoleK8sBootstrap';
import { makeAdminDetailPage } from './AdminResourceDetailPage';

export const AdminPersonaDetailPage = makeAdminDetailPage(
  'Persona',
  'Personas',
  '/hybridsovereign/personas',
);
export const AdminTeamDetailPage = makeAdminDetailPage('Team', 'Teams', '/hybridsovereign/teams');
export const AdminProjectDetailPage = makeAdminDetailPage(
  'Project',
  'Projects',
  '/hybridsovereign/projects',
);
export const AdminPlatformDetailPage = makeAdminDetailPage(
  'PlatformOpenshift',
  'Platforms',
  '/hybridsovereign/platforms',
);
export const AdminAssignmentDetailPage = makeAdminDetailPage(
  'Assignment',
  'Assignments',
  '/hybridsovereign/assignments',
);
export const AdminCloudOSODetailPage = makeAdminDetailPage(
  'CloudOSO',
  'Cloud Environments',
  '/hybridsovereign/clouds',
);
export const AdminCloudAWSDetailPage = makeAdminDetailPage(
  'CloudAWS',
  'Cloud Environments',
  '/hybridsovereign/clouds',
);
export const AdminCloudVirtDetailPage = makeAdminDetailPage(
  'CloudVirt',
  'Cloud Environments',
  '/hybridsovereign/clouds',
);
export const AdminRbacDetailPage = makeAdminDetailPage('Rbac', 'Operators', '/hybridsovereign/operators');
export const AdminRbacConfigDetailPage = makeAdminDetailPage(
  'RbacConfig',
  'Operators',
  '/hybridsovereign/operators',
);
export const AdminAAPOrgDetailPage = makeAdminDetailPage(
  'AAPOrg',
  'Service URLs',
  '/hybridsovereign/services',
);
export const AdminAAPConfigDetailPage = makeAdminDetailPage(
  'AAPConfig',
  'Service URLs',
  '/hybridsovereign/services',
);
export const AdminQuayOrgDetailPage = makeAdminDetailPage(
  'QuayOrg',
  'Service URLs',
  '/hybridsovereign/services',
);
export const AdminQuayConfigDetailPage = makeAdminDetailPage(
  'QuayConfig',
  'Service URLs',
  '/hybridsovereign/services',
);
export const AdminVaultDetailPage = makeAdminDetailPage(
  'Vault',
  'Service URLs',
  '/hybridsovereign/services',
);
export const AdminVaultKVDetailPage = makeAdminDetailPage(
  'VaultKV',
  'Service URLs',
  '/hybridsovereign/services',
);

export default AdminPersonaDetailPage;
