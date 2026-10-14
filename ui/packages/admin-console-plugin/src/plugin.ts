import './consoleK8sBootstrap';

// Do not call initI18n() here — shared react-i18next would stomp console i18n.
// Page components init an isolated instance via useTranslation().

export { default as AdminOverviewPage } from './components/AdminOverviewPage';
export { default as AdminEntitiesPage } from './components/AdminEntitiesPage';
export { default as AdminPersonasPage } from './components/AdminPersonasPage';
export { default as AdminServicesPage } from './components/AdminServicesPage';
export { default as AdminOperatorsPage } from './components/AdminOperatorsPage';
export { default as AdminTeamsPage } from './components/AdminTeamsPage';
export { default as AdminProjectsPage } from './components/AdminProjectsPage';
export { default as AdminPlatformsPage } from './components/AdminPlatformsPage';
export { default as AdminCloudsPage } from './components/AdminCloudsPage';
export { default as AdminAssignmentsPage } from './components/AdminAssignmentsPage';
export { default as AdminEntityDetailPage } from './components/AdminEntityDetailPage';
export { default as AdminPersonaDetailPage } from './components/AdminPersonaDetailPage';
export { default as AdminServiceDetailPage } from './components/AdminServiceDetailPage';
export { default as AdminOperatorDetailPage } from './components/AdminOperatorDetailPage';
export { default as AdminTeamDetailPage } from './components/AdminTeamDetailPage';
export { default as AdminProjectDetailPage } from './components/AdminProjectDetailPage';
export { default as AdminPlatformDetailPage } from './components/AdminPlatformDetailPage';
export { default as AdminAssignmentDetailPage } from './components/AdminAssignmentDetailPage';
export { default as AdminCloudOSODetailPage } from './components/AdminCloudOSODetailPage';
export { default as AdminCloudAWSDetailPage } from './components/AdminCloudAWSDetailPage';
export { default as AdminCloudVirtDetailPage } from './components/AdminCloudVirtDetailPage';
export { default as AdminRbacDetailPage } from './components/AdminRbacDetailPage';
export { default as AdminRbacConfigDetailPage } from './components/AdminRbacConfigDetailPage';
export { default as AdminAAPOrgDetailPage } from './components/AdminAAPOrgDetailPage';
export { default as AdminAAPConfigDetailPage } from './components/AdminAAPConfigDetailPage';
export { default as AdminQuayOrgDetailPage } from './components/AdminQuayOrgDetailPage';
export { default as AdminQuayConfigDetailPage } from './components/AdminQuayConfigDetailPage';
export { default as AdminVaultDetailPage } from './components/AdminVaultDetailPage';
export { default as AdminVaultKVDetailPage } from './components/AdminVaultKVDetailPage';
export { default as AdminCreatePage } from './components/AdminCreatePage';
export { default as AdminCloudInfrastructuresPage } from './components/AdminCloudInfrastructuresPage';
export { default as AdminCloudInfrastructureDetailPage } from './components/AdminCloudInfrastructureDetailPage';
export { default as AdminHybridFabricsPage } from './components/AdminHybridFabricsPage';
export { default as AdminCloudGatewaysPage } from './components/AdminCloudGatewaysPage';
export { default as AdminTransportLinksPage } from './components/AdminTransportLinksPage';
export { default as AdminUIHealthPage } from './components/AdminUIHealthPage';
export { default as AdminHybridFabricDetailPage } from './components/AdminHybridFabricDetailPage';
export { default as AdminCloudGatewayDetailPage } from './components/AdminCloudGatewayDetailPage';
export { default as AdminTransportLinkDetailPage } from './components/AdminTransportLinkDetailPage';
export { default as AdminUIHealthCheckerDetailPage } from './components/AdminUIHealthCheckerDetailPage';
export { icon, getLandingPageURL, getImportRedirectURL } from './perspective';
