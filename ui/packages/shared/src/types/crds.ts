/** API group for all Hybrid Sovereign CRDs */
export const API_GROUP = 'hybridsovereign.redhat';
export const API_VERSION = 'v1alpha1';
export const API_VERSION_FULL = `${API_GROUP}/${API_VERSION}`;

/** Standard operator status fields shared across CRDs */
export interface OperatorStatus {
  status?: 'pending' | 'reconciling' | 'ready' | 'failed';
  ready?: boolean;
  observedGeneration?: number;
  lastReconciledAt?: string;
  message?: string;
  conditions?: StatusCondition[];
  /** Entity CR: provisioned tenant namespace (e.g. entity-acme-corp) */
  entity?: string;
  /** What a pending object waits for */
  waitingFor?: string[];
  /** What a requested delete waits for */
  deletionBlockedBy?: string[];
  deletionComplete?: boolean;
  edaJobs?: EdaJobStatus[];
}

/** One automation job run recorded on the object */
export interface EdaJobStatus {
  name?: string;
  jobId?: string;
  url?: string;
  timestamp?: string;
  status?: 'running' | 'waiting' | 'success' | 'failed' | string;
}

export interface StatusCondition {
  type: string;
  status: 'True' | 'False' | 'Unknown';
  lastTransitionTime?: string;
  reason?: string;
  message?: string;
}

export interface ObjectMeta {
  name: string;
  namespace?: string;
  uid?: string;
  generation?: number;
  creationTimestamp?: string;
  labels?: Record<string, string>;
  annotations?: Record<string, string>;
}

export interface K8sResource<TSpec = object, TStatus = OperatorStatus> {
  apiVersion: string;
  kind: string;
  metadata: ObjectMeta;
  spec: TSpec;
  status?: TStatus;
}

/** Entity — top-level tenant */
export interface EntityNamespaceRbac {
  entityAdmin?: string[];
  auditor?: string[];
  cloudAWSAdmin?: string[];
  cloudOSOAdmin?: string[];
  identityAdmin?: string[];
}

export interface EntitySpec {
  description?: string;
  billingID?: string;
  websiteLink?: string;
  namespaceRbac?: EntityNamespaceRbac;
}

export type Entity = K8sResource<EntitySpec>;

/** Team — entity-scoped team */
export interface TeamSpec {
  rbacConfig?: string;
  features?: {
    argo?: boolean;
    istio?: boolean;
  };
  teamAdmin?: string[];
}

export type Team = K8sResource<TeamSpec>;

/** Assignment — binds team to projects and platforms */
export interface AssignmentSpec {
  team: string;
  projects?: string[];
  openshift?: string;
  aws?: string;
}

export type Assignment = K8sResource<AssignmentSpec>;

/** Project — entity-scoped project namespace */
export interface ProjectSpec {
  description?: string;
}

export type Project = K8sResource<ProjectSpec>;

/** Persona — RBAC persona binding */
export interface PersonaSpec {
  rbac: string;
  type: string;
}

export type Persona = K8sResource<PersonaSpec>;

/** PlatformOpenshift — managed OCP cluster */
export interface PlatformOpenshiftOpenstackSpec {
  /** CloudOSO name providing the OpenStack environment */
  environment?: string;
  controlPlaneCount?: number;
  workerCount?: number;
  controlPlaneFlavor?: string;
  workerFlavor?: string;
  externalNetwork?: string;
  ocpImageSet?: string;
  pullSecretVaultPath?: string;
  pullSecretVaultProperty?: string;
  sshKeyVaultPath?: string;
  glanceImageOverride?: string;
  trustBundle?: string;
}

export interface PlatformOpenshiftAwsSpec {
  /** CloudAWS name providing the AWS environment */
  environment: string;
  region?: string;
  clusterType?: 'standalone' | 'ha';
  controlPlaneCount?: number;
  workerCount?: number;
  controllerFlavor?: string;
  workerFlavor?: string;
}

export interface PlatformOpenshiftToolRbac {
  clusterAdminRbac?: string[];
  clusterOperatorRbac?: string[];
  clusterDeveloperRbac?: string[];
  clusterViewerRbac?: string[];
}

export interface PlatformOpenshiftSpec {
  type?: 'openstack' | 'aws';
  openstack?: PlatformOpenshiftOpenstackSpec;
  aws?: PlatformOpenshiftAwsSpec;
  toolRbac?: PlatformOpenshiftToolRbac;
  clusterViewerRbac?: string[];
}

export type PlatformOpenshift = K8sResource<PlatformOpenshiftSpec>;

/** Vault KV reference held by platform objects (never the secret itself) */
export interface VaultPathRef {
  vaultPath: string;
}

/** Reference to an object by name */
export interface NameRef {
  name: string;
}

/** CloudInfrastructure — platform-owned cloud site (sovereign-cloud) */
export type CloudInfrastructureType = 'openstack' | 'openshift' | 'aws' | 'external';

export interface CloudInfrastructureOpenstackSpec {
  region?: string;
  projectDomain?: string;
  createProjectDomain?: boolean;
  externalNetwork?: string;
}

export interface CloudInfrastructureOpenshiftSpec {
  /** local is the services cluster itself (CRD default) */
  clusterRef?: string;
  bootImage?: string;
  storageClass?: string;
  fabricCredentialsRef?: VaultPathRef;
}

export interface CloudInfrastructureAwsSpec {
  accountId: string;
  region: string;
  baseDomain?: string;
}

export interface CloudInfrastructureExternalSpec {
  /** CRD default true */
  evpn?: boolean;
}

export interface CloudInfrastructureDnsSpec {
  /** CRD default platform */
  mode?: 'native' | 'platform';
  project?: string;
  projectDomain?: string;
  createProject?: boolean;
  zoneQuota?: number;
}

export interface CloudInfrastructureSpec {
  /** Immutable; selects the typed section */
  type: CloudInfrastructureType;
  displayName?: string;
  credentialsRef?: VaultPathRef;
  /** Entities allowed to reference this site; empty means every Entity */
  entityRefs?: NameRef[];
  openstack?: CloudInfrastructureOpenstackSpec;
  openshift?: CloudInfrastructureOpenshiftSpec;
  aws?: CloudInfrastructureAwsSpec;
  external?: CloudInfrastructureExternalSpec;
  dns?: CloudInfrastructureDnsSpec;
}

export interface CloudInfrastructureStatus extends OperatorStatus {
  /** Known keys: evpn, api, virtualization, dns */
  capabilities?: Record<string, boolean>;
  /** Observed site details (informational) */
  site?: {
    endpoint?: string;
    region?: string;
    version?: string;
    [key: string]: unknown;
  };
}

export type CloudInfrastructure = K8sResource<CloudInfrastructureSpec, CloudInfrastructureStatus>;

/** CloudOSO — tenant OpenStack project, on a CloudInfrastructure (type openstack) when cloudRef is in effect */
export interface CloudOSOSpec {
  /** Immutable once set; default: the installation's default site */
  cloudRef?: NameRef;
  project?: string;
  /** Ignored with a cloudRef in effect */
  vaultPath?: string;
  baseDomain?: string;
  projectDomain?: string;
  externalNetwork?: string;
  regionName?: string;
  dnsProvider?: 'designate' | 'route53' | 'none';
  route53VaultPath?: string;
  landingzone?: string;
  designateZoneId?: string;
  designateProjectId?: string;
  enableVRF?: boolean;
  vrfId?: string;
}

export type CloudOSO = K8sResource<CloudOSOSpec>;

/** CloudAWS — AWS account environment */
export interface CloudAWSSpec {
  account: string;
  vaultPath: string;
  baseDomain: string;
  landingzone?: string;
}

export type CloudAWS = K8sResource<CloudAWSSpec>;

/** CloudVirt — tenant VM environment on a CloudInfrastructure (type openshift) */
export interface CloudVirtNamespace {
  name: string;
  /** HybridNetwork in the same namespace; omitted = the site's default network. Fixed once created. */
  network?: string;
}

export interface CloudVirtQuota {
  cpu?: string | number;
  memory?: string | number;
  storage?: string | number;
  virtualMachines?: number;
}

export interface CloudVirtToolRbac {
  editRbac?: string[];
  viewRbac?: string[];
}

export interface CloudVirtSpec {
  description?: string;
  /** Immutable */
  cloudRef: NameRef;
  /** CRD default [{name: vms}] */
  namespaces?: CloudVirtNamespace[];
  storageClass?: string;
  quota?: CloudVirtQuota;
  toolRbac?: CloudVirtToolRbac;
}

export interface CloudVirtNamespaceStatus {
  name?: string;
  /** Namespace on the site */
  namespace?: string;
  network?: string;
  ready?: boolean;
  message?: string;
}

export interface CloudVirtStatus extends OperatorStatus {
  site?: { name?: string; endpoint?: string; console?: string; [key: string]: unknown };
  storageClass?: string;
  namespaces?: CloudVirtNamespaceStatus[];
  /** The environment's DNS zone */
  dns?: { zone?: string; nameservers?: string[]; [key: string]: unknown };
}

export type CloudVirt = K8sResource<CloudVirtSpec, CloudVirtStatus>;

/** OpenStackMigration — VMware to CloudOSO migration */
export interface OpenStackMigrationSpec {
  source?: string;
  vmName?: string;
  cloudoso?: string;
}

export interface OpenStackMigrationStatus extends OperatorStatus {
  source?: string;
  vmName?: string;
  cloudoso?: string;
}

export type OpenStackMigration = K8sResource<OpenStackMigrationSpec, OpenStackMigrationStatus>;

/** Rbac — entity-scoped RBAC role */
export interface RbacSpec {
  config: string;
  description?: string;
}

export interface RbacStatus extends OperatorStatus {
  group?: string;
}

export type Rbac = K8sResource<RbacSpec, RbacStatus>;

/** RbacConfig — platform RBAC backend config */
export interface RbacConfigSpec {
  type: 'keycloak' | string;
  secret: string;
}

export type RbacConfig = K8sResource<RbacConfigSpec>;

/** AAPOrg — Ansible Automation Platform organization */
export interface AAPOrgSpec {
  aapConfig: string;
  aapAdminRbac?: string[];
  aapJobExecutorRbac?: string[];
}

export interface AAPOrgStatus extends OperatorStatus {
  orgName?: string;
  orgId?: number;
  adminGroups?: string[];
}

export type AAPOrg = K8sResource<AAPOrgSpec, AAPOrgStatus>;

/** AAPConfig — platform AAP connection config */
export interface AAPConfigSpec {
  secret: string;
  rbacConfig: string;
}

export type AAPConfig = K8sResource<AAPConfigSpec>;

/** QuayOrg — Quay organization */
export interface QuayOrgSpec {
  quayConfig: string;
  quayAdminRbac?: string[];
  quayCreatorRbac?: string[];
  quayMemberRbac?: string[];
}

export interface QuayOrgStatus extends OperatorStatus {
  orgName?: string;
}

export type QuayOrg = K8sResource<QuayOrgSpec, QuayOrgStatus>;

/** QuayConfig — platform Quay connection config */
export interface QuayConfigSpec {
  secret: string;
  rbacConfig: string;
}

export type QuayConfig = K8sResource<QuayConfigSpec>;

/** Vault — entity Vault instance */
export interface VaultSpec {
  ha?: boolean;
  rbacConfig: string;
}

export type Vault = K8sResource<VaultSpec>;

/** VaultKV — Vault KV mount and RBAC */
export interface VaultKVSpec {
  vault: string;
  vaultAdminRbac?: string[];
  vaultReaderRbac?: string[];
}

export type VaultKV = K8sResource<VaultKVSpec>;

/** HybridFabric — platform network fabric (sovereign-cloud) */
export interface VniPool {
  start: number;
  end: number;
}

export interface BorderGatewayNode {
  /** DNS label; referenced by CloudGateway siteUnderlay.addresses[].node */
  name: string;
  /** Management address or host name */
  address: string;
}

export interface HybridFabricSpec {
  /** Immutable */
  domainAsn: number;
  overlayMtu: number;
  vniPool: VniPool;
  l2vniPool: VniPool;
  borderGateway: {
    nodes: BorderGatewayNode[];
    credentialsRef: VaultPathRef;
  };
  bgp?: { passwordRef?: VaultPathRef };
  /** Entities whose HybridNetworks may use this fabric; empty means every Entity */
  entityRefs?: NameRef[];
}

export interface HybridFabricNodeStatus {
  name?: string;
  ready?: boolean;
  message?: string;
  lastAppliedAt?: string;
}

export interface HybridFabricStatus extends OperatorStatus {
  ledger?: string;
  /** Allocated numbers per pool */
  allocated?: Record<string, number>;
  /** Free numbers per pool */
  available?: Record<string, number>;
  nodes?: HybridFabricNodeStatus[];
  /** Informational; depends on the platform */
  realization?: Record<string, unknown>;
}

export type HybridFabric = K8sResource<HybridFabricSpec, HybridFabricStatus>;

/** How a site's underlay reaches the border gateway nodes */
export type GatewayTransport = 'none' | 'wireguard';

export interface WireguardPeer {
  publicKey: string;
  endpoint?: string;
  allowedIPs: string[];
  persistentKeepalive?: number;
}

/** CloudGateway — attaches one site (CloudInfrastructure) to a HybridFabric (sovereign-cloud) */
export interface CloudGatewaySpec {
  /** Immutable */
  fabricRef: NameRef;
  /** Immutable */
  cloudRef: NameRef;
  /** CRD default none */
  transport?: GatewayTransport;
  /** Required with transport wireguard, and only then */
  wireguard?: {
    listenPort?: number;
    peers: WireguardPeer[];
  };
  siteUnderlay: {
    /** Required with transport none */
    vlan?: number;
    cidr: string;
    addresses: Array<{ node: string; address: string }>;
    mtu?: number;
    gateway?: string;
    upstreamRouteReflectors?: string[];
  };
  /** CRD default true */
  bfd?: boolean;
  bgp?: { passwordRef?: VaultPathRef };
}

export interface CloudGatewayNodeStatus {
  name?: string;
  address?: string;
  ready?: boolean;
  siteSessions?: number;
  coreSessions?: number;
  upstreamSessions?: number;
  message?: string;
  /** transport wireguard only */
  tunnel?: 'up' | 'down' | string;
}

export interface CloudGatewayWireguardPeerStatus {
  publicKey?: string;
  endpoint?: string;
  /** Unix time (seconds) of the last handshake; 0 = never */
  latestHandshake?: number;
  up?: boolean;
}

export interface CloudGatewayWireguardNodeStatus {
  name?: string;
  publicKey?: string;
  listenPort?: number;
  address?: string;
  peers?: CloudGatewayWireguardPeerStatus[];
}

export interface CloudGatewayStatus extends OperatorStatus {
  site?: { name?: string; type?: string; evpn?: boolean; [key: string]: unknown };
  nodes?: CloudGatewayNodeStatus[];
  wireguard?: { nodes?: CloudGatewayWireguardNodeStatus[] };
  /** Informational; depends on the platform */
  realization?: Record<string, unknown>;
}

export type CloudGateway = K8sResource<CloudGatewaySpec, CloudGatewayStatus>;

/** TransportLink — fabric ↔ cloud gateway tunnel */
export interface TransportLinkSpec {
  enabled?: boolean;
  fabricRef?: string;
  cloudGatewayRef?: string;
  tunnelType?: 'wireguard' | 'ipsec' | 'macsec' | 'none';
  vaultConfigRef?: string;
}
export type TransportLink = K8sResource<TransportLinkSpec>;

/** HybridNetwork — a tenant's routed private network across cloud sites */
export interface HybridNetworkSpec {
  description?: string;
  /** Default: the platform's only fabric. Immutable once set; cannot be removed. */
  fabricRef?: NameRef;
  /** Default and maximum: the fabric's overlayMtu */
  mtu?: number;
  networkViewerRbac?: string[];
  egress?: { internet?: boolean };
}

export type EgressState = 'pending' | 'ready' | 'removing' | 'unavailable';

export interface HybridNetworkStatus extends OperatorStatus {
  fabric?: string;
  mtu?: number;
  placementCount?: number;
  placementsReady?: number;
  egress?: { internet?: EgressState | string; sourceAddresses?: string[] | null };
  /** Informational; depends on the platform */
  realization?: Record<string, unknown>;
}

export type HybridNetwork = K8sResource<HybridNetworkSpec, HybridNetworkStatus>;

/** NetworkPlacement backends */
export type NetworkPlacementBackendKind = 'CloudOSO' | 'CloudVirt';

/** NetworkPlacement — puts a HybridNetwork on one cloud environment (all of spec is immutable) */
export interface NetworkPlacementSpec {
  network: string;
  backend: { kind: NetworkPlacementBackendKind; name: string };
  prefixes: string[];
}

export interface NetworkPlacementStatus extends OperatorStatus {
  /** CloudInfrastructure the backend runs on */
  site?: string;
  cloudGateway?: string;
  l3Vni?: number;
  l2Vni?: number;
  mtu?: number;
  realizedPrefixes?: string[];
  /** CloudVirt site namespaces the network serves */
  namespaces?: string[];
  backendReady?: boolean;
  gatewayReady?: boolean;
  validated?: boolean;
  lastValidatedAt?: string;
  /** Informational; depends on the platform */
  realization?: Record<string, unknown>;
}

export type NetworkPlacement = K8sResource<NetworkPlacementSpec, NetworkPlacementStatus>;

/** UIHealthChecker — URL probe target (dashboard pod HTTP check) */
export interface UIHealthCheckerSpec {
  url: string;
  displayName?: string;
  description?: string;
  group?: string;
  expectedStatus?: number;
  timeoutSeconds?: number;
}
export type UIHealthChecker = K8sResource<UIHealthCheckerSpec>;

/** Union of all Hybrid Sovereign CR kinds */
export type HybridSovereignKind =
  | 'Entity'
  | 'Team'
  | 'Assignment'
  | 'Project'
  | 'Persona'
  | 'PlatformOpenshift'
  | 'CloudOSO'
  | 'CloudAWS'
  | 'CloudVirt'
  | 'OpenStackMigration'
  | 'Rbac'
  | 'RbacConfig'
  | 'AAPOrg'
  | 'AAPConfig'
  | 'QuayOrg'
  | 'QuayConfig'
  | 'Vault'
  | 'VaultKV'
  | 'CloudInfrastructure'
  | 'HybridFabric'
  | 'CloudGateway'
  | 'TransportLink'
  | 'HybridNetwork'
  | 'NetworkPlacement'
  | 'UIHealthChecker';

/** Plural resource names for K8s API paths */
export const KIND_PLURALS: Record<HybridSovereignKind, string> = {
  Entity: 'entities',
  Team: 'teams',
  Assignment: 'assignments',
  Project: 'projects',
  Persona: 'personas',
  PlatformOpenshift: 'platformopenshifts',
  CloudOSO: 'cloudosos',
  CloudAWS: 'cloudawss',
  CloudVirt: 'cloudvirts',
  OpenStackMigration: 'openstackmigrations',
  Rbac: 'rbacs',
  RbacConfig: 'rbacconfigs',
  AAPOrg: 'aaporgs',
  AAPConfig: 'aapconfigs',
  QuayOrg: 'quayorgs',
  QuayConfig: 'quayconfigs',
  Vault: 'vaults',
  VaultKV: 'vaultkvs',
  CloudInfrastructure: 'cloudinfrastructures',
  HybridFabric: 'hybridfabrics',
  CloudGateway: 'cloudgateways',
  TransportLink: 'transportlinks',
  HybridNetwork: 'hybridnetworks',
  NetworkPlacement: 'networkplacements',
  UIHealthChecker: 'uihealthcheckers',
};

/** Namespaced kinds shown on the tenancy Overview */
export const TENANT_OVERVIEW_KINDS: HybridSovereignKind[] = [
  'Team',
  'Project',
  'PlatformOpenshift',
  'Assignment',
  'CloudOSO',
  'CloudAWS',
  'CloudVirt',
  'OpenStackMigration',
  'Persona',
  'Rbac',
  'Vault',
  'VaultKV',
  'AAPOrg',
  'QuayOrg',
  'HybridNetwork',
  'NetworkPlacement',
];

export type HybridSovereignResource =
  | Entity
  | Team
  | Assignment
  | Project
  | Persona
  | PlatformOpenshift
  | CloudOSO
  | CloudAWS
  | CloudVirt
  | OpenStackMigration
  | Rbac
  | RbacConfig
  | AAPOrg
  | AAPConfig
  | QuayOrg
  | QuayConfig
  | Vault
  | VaultKV
  | CloudInfrastructure
  | HybridFabric
  | CloudGateway
  | TransportLink
  | HybridNetwork
  | NetworkPlacement
  | UIHealthChecker;
