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
  environment: string;
  controlPlaneCount?: number;
  workerCount?: number;
  controlPlaneFlavor?: string;
  workerFlavor?: string;
  externalNetwork?: string;
}

export interface PlatformOpenshiftAwsSpec {
  environment: string;
  region?: string;
  clusterType?: 'standalone' | 'ha';
  controlPlaneCount?: number;
  workerCount?: number;
  controllerFlavor?: string;
  workerFlavor?: string;
}

export interface PlatformOpenshiftHostedSpec {
  /** CloudVirt CR name providing the virt environment */
  environment: string;
  releaseImage?: string;
  nodePoolReplicas?: number;
  workerCores?: number;
  workerMemory?: string;
}

/**
 * Cluster address plan. Explicit CIDRs are used as given; omitted ones are allocated from the
 * CloudInfrastructure's hostedClusterCidrDefaults (CG-NAT). Clusters do not join a HybridFabric.
 */
export interface PlatformOpenshiftNetworkingSpec {
  clusterNetwork?: string[];
  serviceNetwork?: string[];
  allowConflict?: boolean;
}

export interface PlatformOpenshiftSpec {
  type: 'openstack' | 'aws' | 'hosted';
  openstack?: PlatformOpenshiftOpenstackSpec;
  aws?: PlatformOpenshiftAwsSpec;
  hosted?: PlatformOpenshiftHostedSpec;
  cloudRef?: string;
  networking?: PlatformOpenshiftNetworkingSpec;
}

/** Observed cluster CIDR plan; used for hub-overlap checks. */
export interface PlatformOpenshiftNetworkingStatus {
  clusterNetwork?: string[];
  serviceNetwork?: string[];
  machineNetwork?: string[];
  legacyClusterCidrs?: boolean;
  ipamCondition?: 'DefaultRange' | 'LegacyClusterCidrs' | string;
  conflictCheck?: 'passed' | 'failed' | string;
  conflictMessage?: string;
}

export interface PlatformOpenshiftStatus extends OperatorStatus {
  networking?: PlatformOpenshiftNetworkingStatus;
}

export type PlatformOpenshift = K8sResource<PlatformOpenshiftSpec, PlatformOpenshiftStatus>;

/** CloudInfrastructure — platform-owned cloud site (sovereign-cloud, shortName cinfra) */
export type CloudInfrastructureType = 'openstack' | 'openshift' | 'aws';

/** Exactly one of vaultPath or secretRef. */
export interface CloudInfrastructureCredentialsRef {
  vaultPath?: string;
  secretRef?: { name: string };
}

export interface CidrPool {
  cidr?: string;
  blockPrefixLength?: number;
}

export interface CloudInfrastructureOpenstackSpec {
  region?: string;
  managementClusterKubeconfigRef?: string;
  netConfigRef?: string;
  /** Ordered: rolled out one NodeSet at a time */
  dataplaneNodeSetRefs?: string[];
  externalNetwork?: string;
  baseDomain?: string;
  projectDomain?: string;
  designate?: { zoneId?: string; projectId?: string };
  route53VaultPath?: string;
}

export interface CloudInfrastructureOpenshiftSpec {
  clusterRef?: string;
  bootImage?: string;
  storageClass?: string;
  hostedClusterCidrDefaults?: {
    clusterNetworkPool?: CidrPool;
    serviceNetworkPool?: CidrPool;
  };
}

export interface CloudInfrastructureAwsSpec {
  accountId: string;
  region: string;
  baseDomain?: string;
}

export interface CloudInfrastructureSpec {
  /** Immutable; selects the typed section */
  type: CloudInfrastructureType;
  displayName?: string;
  credentialsRef?: CloudInfrastructureCredentialsRef;
  /** Entities allowed to reference this site; empty means every Entity */
  entityRefs?: Array<{ name: string }>;
  openstack?: CloudInfrastructureOpenstackSpec;
  openshift?: CloudInfrastructureOpenshiftSpec;
  aws?: CloudInfrastructureAwsSpec;
}

export interface CloudInfrastructureStatus extends OperatorStatus {
  /** Known keys: evpn, virtualization, frrK8s, dataplane, openstackApi */
  capabilities?: Record<string, boolean>;
  site?: {
    endpoint?: string;
    region?: string;
    version?: string;
    dataplaneNodeSets?: Array<{ name?: string; ready?: boolean }>;
  };
}

export type CloudInfrastructure = K8sResource<CloudInfrastructureSpec, CloudInfrastructureStatus>;

/** Reference from a tenant cloud project or CloudGateway to a CloudInfrastructure in sovereign-cloud. */
export interface CloudInfrastructureRef {
  kind?: 'CloudInfrastructure';
  name: string;
}

/** CloudOSO — tenant OpenStack project on a CloudInfrastructure (type openstack) */
export interface CloudOSOSpec {
  cloudRef?: CloudInfrastructureRef;
  project?: string;
  baseDomain?: string;
  /** Unset falls back to the CloudInfrastructure value */
  projectDomain?: string;
  /** Unset falls back to the CloudInfrastructure value */
  externalNetwork?: string;
  route53VaultPath?: string;
  landingzone?: string;
  designateZoneId?: string;
  designateProjectId?: string;
}

export type CloudOSO = K8sResource<CloudOSOSpec>;

/** CloudAWS — AWS account environment */
export interface CloudAWSToolRbac {
  accountAdminRbac?: string[];
  accountPoweruserRbac?: string[];
  accountViewerRbac?: string[];
}

export interface CloudAWSSpec {
  cloudRef?: CloudInfrastructureRef;
  account?: string;
  /** Ignored when cloudRef is set */
  vaultPath?: string;
  credentialsSecretRef?: { name: string };
  baseDomain?: string;
  landingzone?: string;
  toolRbac?: CloudAWSToolRbac;
}

export type CloudAWS = K8sResource<CloudAWSSpec>;

/** CloudVirt — tenant project on a CloudInfrastructure (type openshift) */
export interface CloudVirtToolRbac {
  environmentAdminRbac?: string[];
  environmentPoweruserRbac?: string[];
  environmentViewerRbac?: string[];
}

export interface CloudVirtSpec {
  cloudRef?: CloudInfrastructureRef;
  /** ResourceQuota spec.hard applied to every VM namespace a NetworkPlacement creates */
  vmNamespaceQuota?: { hard?: Record<string, string | number> };
  baseDomain?: string;
  storageClass?: string;
  networkAttachment?: string;
  toolRbac?: CloudVirtToolRbac;
}

export type CloudVirt = K8sResource<CloudVirtSpec>;

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

/** Transport between a site and the border gateway */
export type FabricTransportType = 'none' | 'wireguard';

/** HybridFabric — platform EVPN fabric (one per hub) */
export interface HybridFabricSpec {
  enabled?: boolean;
  domainAsn?: number;
  /** Entities that may place HybridNetworks on this fabric — tagged via EntityMultiSelect. */
  entityRefs?: Array<{ name: string }>;
  vniPool?: { start: number; end: number };
  underlay?: {
    /** How the border gateway's underlay NIC attaches on the hub */
    type?: 'ovn-layer2' | 'localnet';
    /** localnet only — OVN bridge mapping name */
    physicalNetworkName?: string;
    nadName?: string;
    cidr?: string;
    /** ovn-layer2 only */
    gatewayAddress?: string;
    /** localnet only — CIDR inside cidr for hub node VTEPs and the gateway's hub leg */
    hubVtepBlock?: string;
    /** localnet only — border gateway address with prefix length */
    hubLegAddress?: string;
    /** ovn-layer2 only */
    dhcpRange?: { start?: string; end?: string; leaseTime?: string };
    mtu?: number;
  };
  borderGateway?: {
    name?: string;
    loopback?: string;
    vaultCredentialRef?: string;
    wireguard?: { address?: string; listenPort?: number; mtu?: number };
    ingressHost?: string;
    vmSize?: { cpu?: number; memory?: string };
    image?: string;
    storageClass?: string;
  };
  bgp?: { authentication?: { secretRef?: { name: string } } };
  transportDefaults?: {
    mtu?: number;
    defaultTunnelType?: FabricTransportType;
  };
}

export interface HybridFabricStatus extends OperatorStatus {
  bgwEndpoint?: string;
  bgwAddress?: string;
  bgwPeerCount?: number;
  peers?: Array<{ address?: string; site?: string; state?: string; prefixesReceived?: number }>;
  allocatedVniCount?: number;
  availableVniCount?: number;
}
export type HybridFabric = K8sResource<HybridFabricSpec, HybridFabricStatus>;

/** CloudGateway — one per site attached to the fabric (sovereign-cloud) */
export interface CloudGatewaySpec {
  enabled?: boolean;
  fabricRef?: string;
  /** CloudInfrastructure in sovereign-cloud; its type selects the gateway flavour */
  cloudRef?: CloudInfrastructureRef;
  /** Unset type falls back to the fabric's transportDefaults.defaultTunnelType */
  transport?: { type?: FabricTransportType; vaultPeerConfigRef?: string };
  /** Site gateway tunnel address; allocated when omitted */
  wireguard?: { address?: string };
  /** OpenStack sites only */
  siteUnderlay?: {
    interface?: string;
    computeInterface?: string;
    nodeSetInterfaces?: Record<string, string>;
    cidr?: string;
    gatewayAddress?: string;
    mtu?: number;
  };
  /** Test environments only (hypervisor source-MAC filtering) */
  macNatShim?: { enabled?: boolean; interface?: string };
}

export interface CloudGatewayStatus extends OperatorStatus {
  landingZoneReady?: boolean;
  gatewayAddress?: string;
  transportReady?: boolean;
  peerCount?: number;
  peerState?: string;
  vtep?: string;
  importedRts?: string[];
  edpmNodeSets?: Array<{
    name?: string;
    fingerprint?: string;
    deployment?: string;
    state?: 'Converged' | 'Ready' | 'Failed' | 'NotAttempted';
  }>;
}
export type CloudGateway = K8sResource<CloudGatewaySpec, CloudGatewayStatus>;

/** TransportLink — border gateway ↔ CloudGateway tunnel (operator-generated, one per CloudGateway) */
export interface TransportLinkSpec {
  enabled?: boolean;
  fabricRef?: string;
  cloudGatewayRef?: string;
  /** Copied from CloudGateway.spec.transport.type */
  tunnelType?: FabricTransportType;
}
export type TransportLink = K8sResource<TransportLinkSpec>;

/** HybridNetwork — tenant network identity (VRF: VNI + RT) */
export interface HybridNetworkSpec {
  description?: string;
  /** Disambiguates fabric when Entity is tagged on >1 Ready HybridFabric */
  fabricRef?: string;
  /** Overlay MTU (CUDN mtu, Neutron network MTU); defaults from the fabric transport MTU */
  overlayMtu?: number;
  networkViewerRbac?: string[];
}

export interface HybridNetworkStatus extends OperatorStatus {
  overlayMtu?: number;
  placements?: Array<{
    name?: string;
    backendKind?: string;
    backendName?: string;
    prefixes?: string[];
    ready?: boolean;
  }>;
}
export type HybridNetwork = K8sResource<HybridNetworkSpec, HybridNetworkStatus>;

/** NetworkPlacement backends (decision 6: no PlatformOpenshift attachment) */
export type NetworkPlacementBackendKind = 'CloudOSO' | 'CloudAWS' | 'CloudVirt';

/** NetworkPlacement — places a HybridNetwork on one tenant cloud project */
export interface NetworkPlacementSpec {
  network: string;
  backend: { kind: NetworkPlacementBackendKind; name: string };
  prefixes?: string[];
  /** Backend kind CloudVirt only — hub namespaces the operator creates (DNS labels) */
  vmNamespaces?: string[];
}

export interface NetworkPlacementStatus extends OperatorStatus {
  vmNamespaces?: string[];
  backendIds?: Record<string, string>;
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
