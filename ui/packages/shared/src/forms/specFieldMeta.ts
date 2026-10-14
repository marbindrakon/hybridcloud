import { HybridSovereignKind } from '../types';

export type SpecFieldWidget =
  | 'text'
  | 'textarea'
  | 'number'
  | 'boolean'
  | 'stringList'
  | 'cidrList'
  | 'namedRefList'
  /** `{ name }` object reference edited as one text input; empty removes the reference */
  | 'namedRef'
  | 'select'
  | 'json';

/** Show a field only when another spec path matches (type-keyed sections). */
export interface SpecFieldVisibleWhen {
  path: string;
  /** Unset values compare as '' */
  equals?: string | string[];
  /** true: visible only when path has a value; false: only when it has none */
  isSet?: boolean;
}

export interface SpecFieldMeta {
  /** Dot path under spec, e.g. features.istio or backend.name */
  path: string;
  labelKey: string;
  widget: SpecFieldWidget;
  immutable?: boolean;
  options?: Array<{ value: string; labelKey: string }>;
  helpKey?: string;
  visibleWhen?: SpecFieldVisibleWhen;
  /** Empty input removes the key (sent as null) instead of storing '' */
  omitWhenEmpty?: boolean;
}

export interface KindSpecMeta {
  fields: SpecFieldMeta[];
}

/** Only none and wireguard are offered (ipsec/macsec are being removed from the API). */
const tunnelOptions = [
  { value: 'none', labelKey: 'fields.tunnelNone' },
  { value: 'wireguard', labelKey: 'fields.tunnelWireguard' },
];

const cloudRefField: SpecFieldMeta = {
  path: 'cloudRef',
  labelKey: 'fields.cloudRef',
  widget: 'namedRef',
  helpKey: 'fields.cloudRefHelp',
};

const whenType = (path: string, equals: string | string[]): SpecFieldVisibleWhen => ({ path, equals });

const cinfraOpenstack = whenType('type', 'openstack');
const cinfraOpenshift = whenType('type', 'openshift');
const cinfraAws = whenType('type', 'aws');
const cinfraCreds = whenType('type', ['openstack', 'aws']);
const poOpenstack = whenType('type', 'openstack');
const poAws = whenType('type', 'aws');
const poHosted = whenType('type', 'hosted');
const underlayLocalnet = whenType('underlay.type', 'localnet');
const underlayLayer2 = whenType('underlay.type', ['ovn-layer2', '']);

/** Editable / immutable field registry for tenancy + admin detail editors */
export const KIND_SPEC_META: Partial<Record<HybridSovereignKind, KindSpecMeta>> = {
  Entity: {
    fields: [
      { path: 'description', labelKey: 'fields.description', widget: 'textarea' },
      { path: 'billingID', labelKey: 'fields.billingID', widget: 'text', immutable: true },
      { path: 'websiteLink', labelKey: 'fields.websiteLink', widget: 'text' },
    ],
  },
  Team: {
    fields: [
      { path: 'features.istio', labelKey: 'fields.istio', widget: 'boolean' },
      { path: 'features.argo', labelKey: 'fields.argo', widget: 'boolean' },
    ],
  },
  Project: {
    fields: [
      { path: 'displayName', labelKey: 'fields.displayName', widget: 'text' },
      { path: 'description', labelKey: 'fields.description', widget: 'textarea' },
    ],
  },
  Assignment: {
    fields: [
      { path: 'team', labelKey: 'fields.team', widget: 'text', immutable: true },
      { path: 'projects', labelKey: 'fields.projects', widget: 'stringList' },
      { path: 'openshift', labelKey: 'fields.openshift', widget: 'text' },
      { path: 'toolRbac.assignmentAdmin', labelKey: 'fields.assignmentAdmin', widget: 'text' },
      { path: 'toolRbac.assignmentDeveloper', labelKey: 'fields.assignmentDeveloper', widget: 'text' },
      { path: 'toolRbac.assignmentViewer', labelKey: 'fields.assignmentViewer', widget: 'text' },
      { path: 'toolRbac.assignmentOps', labelKey: 'fields.assignmentOps', widget: 'text' },
    ],
  },
  PlatformOpenshift: {
    fields: [
      {
        path: 'type',
        labelKey: 'fields.platformType',
        widget: 'select',
        immutable: true,
        options: [
          { value: 'openstack', labelKey: 'fields.platformOpenstack' },
          { value: 'aws', labelKey: 'fields.platformAws' },
          { value: 'hosted', labelKey: 'fields.platformHosted' },
        ],
      },
      { path: 'openstack.environment', labelKey: 'fields.cloudOsoEnv', widget: 'text', immutable: true, visibleWhen: poOpenstack },
      { path: 'openstack.controlPlaneCount', labelKey: 'fields.controlPlaneCount', widget: 'number', visibleWhen: poOpenstack },
      { path: 'openstack.workerCount', labelKey: 'fields.workerCount', widget: 'number', visibleWhen: poOpenstack },
      { path: 'openstack.controlPlaneFlavor', labelKey: 'fields.controlPlaneFlavor', widget: 'text', visibleWhen: poOpenstack },
      { path: 'openstack.workerFlavor', labelKey: 'fields.workerFlavor', widget: 'text', visibleWhen: poOpenstack },
      { path: 'openstack.externalNetwork', labelKey: 'fields.externalNetwork', widget: 'text', visibleWhen: poOpenstack },
      { path: 'aws.environment', labelKey: 'fields.cloudAwsEnv', widget: 'text', immutable: true, visibleWhen: poAws },
      { path: 'aws.region', labelKey: 'fields.region', widget: 'text', visibleWhen: poAws },
      {
        path: 'aws.clusterType',
        labelKey: 'fields.clusterType',
        widget: 'select',
        visibleWhen: poAws,
        options: [
          { value: 'standalone', labelKey: 'fields.clusterStandalone' },
          { value: 'ha', labelKey: 'fields.clusterHa' },
        ],
      },
      { path: 'aws.controlPlaneCount', labelKey: 'fields.controlPlaneCount', widget: 'number', visibleWhen: poAws },
      { path: 'aws.workerCount', labelKey: 'fields.workerCount', widget: 'number', visibleWhen: poAws },
      { path: 'aws.controllerFlavor', labelKey: 'fields.controllerFlavor', widget: 'text', visibleWhen: poAws },
      { path: 'aws.workerFlavor', labelKey: 'fields.workerFlavor', widget: 'text', visibleWhen: poAws },
      { path: 'hosted.environment', labelKey: 'fields.cloudVirtEnv', widget: 'text', immutable: true, visibleWhen: poHosted },
      { path: 'hosted.releaseImage', labelKey: 'fields.releaseImage', widget: 'text', visibleWhen: poHosted, omitWhenEmpty: true },
      { path: 'hosted.nodePoolReplicas', labelKey: 'fields.nodePoolReplicas', widget: 'number', visibleWhen: poHosted },
      { path: 'hosted.workerCores', labelKey: 'fields.workerCores', widget: 'number', visibleWhen: poHosted },
      { path: 'hosted.workerMemory', labelKey: 'fields.workerMemory', widget: 'text', visibleWhen: poHosted },
      {
        path: 'networking.clusterNetwork',
        labelKey: 'fields.clusterNetwork',
        widget: 'cidrList',
        helpKey: 'fields.clusterCidrsHelp',
      },
      {
        path: 'networking.serviceNetwork',
        labelKey: 'fields.serviceNetwork',
        widget: 'cidrList',
        helpKey: 'fields.clusterCidrsHelp',
      },
      {
        path: 'networking.allowConflict',
        labelKey: 'fields.allowConflict',
        widget: 'boolean',
        helpKey: 'fields.allowConflictHelp',
      },
      { path: 'toolRbac.clusterAdminRbac', labelKey: 'fields.clusterAdminRbac', widget: 'stringList' },
      { path: 'toolRbac.clusterOperatorRbac', labelKey: 'fields.clusterOperatorRbac', widget: 'stringList' },
      { path: 'toolRbac.clusterDeveloperRbac', labelKey: 'fields.clusterDeveloperRbac', widget: 'stringList' },
      { path: 'toolRbac.clusterViewerRbac', labelKey: 'fields.clusterViewerRbacTool', widget: 'stringList' },
      { path: 'clusterViewerRbac', labelKey: 'fields.clusterViewerRbac', widget: 'stringList' },
    ],
  },
  Persona: {
    fields: [
      { path: 'rbac', labelKey: 'fields.rbac', widget: 'text', immutable: true },
      { path: 'type', labelKey: 'fields.personaType', widget: 'text', immutable: true },
    ],
  },
  Rbac: {
    fields: [
      { path: 'config', labelKey: 'fields.rbacConfig', widget: 'text', immutable: true },
      { path: 'description', labelKey: 'fields.description', widget: 'textarea' },
    ],
  },
  CloudInfrastructure: {
    fields: [
      {
        path: 'type',
        labelKey: 'fields.cloudInfraType',
        widget: 'select',
        immutable: true,
        options: [
          { value: 'openstack', labelKey: 'fields.cloudTypeOpenstack' },
          { value: 'openshift', labelKey: 'fields.cloudTypeOpenshift' },
          { value: 'aws', labelKey: 'fields.cloudTypeAws' },
        ],
      },
      { path: 'displayName', labelKey: 'fields.displayName', widget: 'text' },
      {
        path: 'credentialsRef.vaultPath',
        labelKey: 'fields.credentialsVaultPath',
        widget: 'text',
        helpKey: 'fields.credentialsRefHelp',
        visibleWhen: cinfraCreds,
        omitWhenEmpty: true,
      },
      {
        path: 'credentialsRef.secretRef',
        labelKey: 'fields.credentialsSecretName',
        widget: 'namedRef',
        helpKey: 'fields.credentialsRefHelp',
        visibleWhen: cinfraCreds,
      },
      {
        path: 'entityRefs',
        labelKey: 'fields.entityRefs',
        widget: 'namedRefList',
        helpKey: 'fields.cloudInfraEntityRefsHelp',
      },
      { path: 'openstack.region', labelKey: 'fields.region', widget: 'text', visibleWhen: cinfraOpenstack },
      {
        path: 'openstack.managementClusterKubeconfigRef',
        labelKey: 'fields.managementClusterKubeconfigRef',
        widget: 'text',
        helpKey: 'fields.managementClusterKubeconfigRefHelp',
        visibleWhen: cinfraOpenstack,
        omitWhenEmpty: true,
      },
      { path: 'openstack.netConfigRef', labelKey: 'fields.netConfigRef', widget: 'text', visibleWhen: cinfraOpenstack },
      {
        path: 'openstack.dataplaneNodeSetRefs',
        labelKey: 'fields.dataplaneNodeSetRefs',
        widget: 'stringList',
        helpKey: 'fields.dataplaneNodeSetRefsHelp',
        visibleWhen: cinfraOpenstack,
      },
      { path: 'openstack.externalNetwork', labelKey: 'fields.externalNetwork', widget: 'text', visibleWhen: cinfraOpenstack },
      { path: 'openstack.baseDomain', labelKey: 'fields.baseDomain', widget: 'text', visibleWhen: cinfraOpenstack },
      { path: 'openstack.projectDomain', labelKey: 'fields.projectDomain', widget: 'text', visibleWhen: cinfraOpenstack },
      { path: 'openstack.designate.zoneId', labelKey: 'fields.designateZoneId', widget: 'text', visibleWhen: cinfraOpenstack },
      { path: 'openstack.designate.projectId', labelKey: 'fields.designateProjectId', widget: 'text', visibleWhen: cinfraOpenstack },
      {
        path: 'openstack.route53VaultPath',
        labelKey: 'fields.route53VaultPath',
        widget: 'text',
        visibleWhen: cinfraOpenstack,
        omitWhenEmpty: true,
      },
      { path: 'openshift.clusterRef', labelKey: 'fields.clusterRef', widget: 'text', helpKey: 'fields.clusterRefHelp', visibleWhen: cinfraOpenshift },
      { path: 'openshift.bootImage', labelKey: 'fields.bootImage', widget: 'text', visibleWhen: cinfraOpenshift },
      { path: 'openshift.storageClass', labelKey: 'fields.storageClass', widget: 'text', visibleWhen: cinfraOpenshift },
      {
        path: 'openshift.hostedClusterCidrDefaults.clusterNetworkPool.cidr',
        labelKey: 'fields.clusterNetworkPoolCidr',
        widget: 'text',
        helpKey: 'fields.hostedClusterCidrDefaultsHelp',
        visibleWhen: cinfraOpenshift,
      },
      {
        path: 'openshift.hostedClusterCidrDefaults.clusterNetworkPool.blockPrefixLength',
        labelKey: 'fields.clusterNetworkPoolBlock',
        widget: 'number',
        visibleWhen: cinfraOpenshift,
      },
      {
        path: 'openshift.hostedClusterCidrDefaults.serviceNetworkPool.cidr',
        labelKey: 'fields.serviceNetworkPoolCidr',
        widget: 'text',
        visibleWhen: cinfraOpenshift,
      },
      {
        path: 'openshift.hostedClusterCidrDefaults.serviceNetworkPool.blockPrefixLength',
        labelKey: 'fields.serviceNetworkPoolBlock',
        widget: 'number',
        visibleWhen: cinfraOpenshift,
      },
      { path: 'aws.accountId', labelKey: 'fields.awsAccountId', widget: 'text', visibleWhen: cinfraAws },
      { path: 'aws.region', labelKey: 'fields.region', widget: 'text', visibleWhen: cinfraAws },
      { path: 'aws.baseDomain', labelKey: 'fields.baseDomain', widget: 'text', visibleWhen: cinfraAws },
    ],
  },
  CloudOSO: {
    fields: [
      cloudRefField,
      { path: 'project', labelKey: 'fields.osoProject', widget: 'text' },
      { path: 'baseDomain', labelKey: 'fields.baseDomain', widget: 'text' },
      { path: 'projectDomain', labelKey: 'fields.projectDomain', widget: 'text', helpKey: 'fields.inheritsFromCloudInfra' },
      { path: 'externalNetwork', labelKey: 'fields.externalNetwork', widget: 'text', helpKey: 'fields.inheritsFromCloudInfra' },
      { path: 'route53VaultPath', labelKey: 'fields.route53VaultPath', widget: 'text' },
      { path: 'landingzone', labelKey: 'fields.landingzone', widget: 'text' },
      { path: 'designateZoneId', labelKey: 'fields.designateZoneId', widget: 'text', helpKey: 'fields.inheritsFromCloudInfra' },
      { path: 'designateProjectId', labelKey: 'fields.designateProjectId', widget: 'text', helpKey: 'fields.inheritsFromCloudInfra' },
    ],
  },
  CloudAWS: {
    fields: [
      cloudRefField,
      { path: 'account', labelKey: 'fields.account', widget: 'text' },
      {
        path: 'vaultPath',
        labelKey: 'fields.vaultPath',
        widget: 'text',
        visibleWhen: { path: 'cloudRef.name', isSet: false },
      },
      { path: 'baseDomain', labelKey: 'fields.baseDomain', widget: 'text' },
      { path: 'landingzone', labelKey: 'fields.landingzone', widget: 'text' },
    ],
  },
  CloudVirt: {
    fields: [
      cloudRefField,
      { path: 'baseDomain', labelKey: 'fields.baseDomain', widget: 'text' },
      { path: 'storageClass', labelKey: 'fields.storageClass', widget: 'text' },
      { path: 'networkAttachment', labelKey: 'fields.networkAttachment', widget: 'text' },
      {
        path: 'vmNamespaceQuota.hard',
        labelKey: 'fields.vmNamespaceQuotaHard',
        widget: 'json',
        helpKey: 'fields.vmNamespaceQuotaHardHelp',
      },
    ],
  },
  OpenStackMigration: {
    fields: [
      { path: 'source', labelKey: 'fields.source', widget: 'text', immutable: true },
      { path: 'vmName', labelKey: 'fields.vmName', widget: 'text', immutable: true },
      { path: 'cloudoso', labelKey: 'fields.cloudOso', widget: 'text', immutable: true },
      { path: 'providerNamespace', labelKey: 'fields.providerNamespace', widget: 'text' },
      { path: 'plan', labelKey: 'fields.migrationPlan', widget: 'json' },
    ],
  },
  Vault: {
    fields: [
      { path: 'ha', labelKey: 'fields.ha', widget: 'boolean' },
      { path: 'rbacConfig', labelKey: 'fields.rbacConfig', widget: 'text', immutable: true },
    ],
  },
  VaultKV: {
    fields: [
      { path: 'vault', labelKey: 'fields.vault', widget: 'text', immutable: true },
      { path: 'vaultAdminRbac', labelKey: 'fields.vaultAdminRbac', widget: 'stringList' },
      { path: 'vaultReaderRbac', labelKey: 'fields.vaultReaderRbac', widget: 'stringList' },
      { path: 'vaultOpsRbac', labelKey: 'fields.vaultOpsRbac', widget: 'stringList' },
      { path: 'vaultDeveloperRbac', labelKey: 'fields.vaultDeveloperRbac', widget: 'stringList' },
    ],
  },
  AAPOrg: {
    fields: [
      { path: 'aapConfig', labelKey: 'fields.aapConfig', widget: 'text', immutable: true },
      { path: 'aapAdminRbac', labelKey: 'fields.aapAdminRbac', widget: 'stringList' },
      { path: 'aapJobExecutorRbac', labelKey: 'fields.aapJobExecutorRbac', widget: 'stringList' },
      { path: 'aapViewerRbac', labelKey: 'fields.aapViewerRbac', widget: 'stringList' },
    ],
  },
  QuayOrg: {
    fields: [
      { path: 'quayConfig', labelKey: 'fields.quayConfig', widget: 'text', immutable: true },
      { path: 'quayAdminRbac', labelKey: 'fields.quayAdminRbac', widget: 'stringList' },
      { path: 'quayCreatorRbac', labelKey: 'fields.quayCreatorRbac', widget: 'stringList' },
      { path: 'quayMemberRbac', labelKey: 'fields.quayMemberRbac', widget: 'stringList' },
    ],
  },
  HybridNetwork: {
    fields: [
      { path: 'description', labelKey: 'fields.description', widget: 'textarea' },
      { path: 'fabricRef', labelKey: 'fields.fabricRef', widget: 'text', immutable: true },
      {
        path: 'overlayMtu',
        labelKey: 'fields.overlayMtu',
        widget: 'number',
        helpKey: 'fields.overlayMtuHelp',
      },
      { path: 'networkViewerRbac', labelKey: 'fields.networkViewerRbac', widget: 'stringList' },
    ],
  },
  NetworkPlacement: {
    fields: [
      { path: 'network', labelKey: 'fields.network', widget: 'text', immutable: true },
      { path: 'backend.kind', labelKey: 'fields.backendKind', widget: 'text', immutable: true },
      { path: 'backend.name', labelKey: 'fields.backendName', widget: 'text', immutable: true },
      { path: 'prefixes', labelKey: 'fields.prefixes', widget: 'cidrList' },
      {
        path: 'vmNamespaces',
        labelKey: 'fields.vmNamespaces',
        widget: 'stringList',
        helpKey: 'fields.vmNamespacesHelp',
        visibleWhen: { path: 'backend.kind', equals: 'CloudVirt' },
      },
    ],
  },
  HybridFabric: {
    fields: [
      { path: 'enabled', labelKey: 'fields.enabled', widget: 'boolean' },
      {
        path: 'domainAsn',
        labelKey: 'fields.domainAsn',
        widget: 'number',
        immutable: true,
      },
      {
        path: 'entityRefs',
        labelKey: 'fields.entityRefs',
        widget: 'namedRefList',
        helpKey: 'fields.entityRefsHelp',
      },
      { path: 'vniPool.start', labelKey: 'fields.vniStart', widget: 'number', immutable: true },
      { path: 'vniPool.end', labelKey: 'fields.vniEnd', widget: 'number', immutable: true },
      {
        path: 'transportDefaults.defaultTunnelType',
        labelKey: 'fields.defaultTunnelType',
        widget: 'select',
        options: tunnelOptions,
        helpKey: 'fields.defaultTunnelTypeHelp',
      },
      { path: 'transportDefaults.mtu', labelKey: 'fields.mtu', widget: 'number' },
      {
        path: 'underlay.type',
        labelKey: 'fields.underlayType',
        widget: 'select',
        helpKey: 'fields.underlayHelp',
        options: [
          { value: 'localnet', labelKey: 'fields.underlayLocalnet' },
          { value: 'ovn-layer2', labelKey: 'fields.underlayLayer2' },
        ],
      },
      {
        path: 'underlay.physicalNetworkName',
        labelKey: 'fields.underlayPhysicalNetwork',
        widget: 'text',
        visibleWhen: underlayLocalnet,
      },
      { path: 'underlay.nadName', labelKey: 'fields.underlayNadName', widget: 'text' },
      { path: 'underlay.cidr', labelKey: 'fields.underlayCidr', widget: 'text', immutable: true },
      {
        path: 'underlay.hubVtepBlock',
        labelKey: 'fields.underlayHubVtepBlock',
        widget: 'text',
        helpKey: 'fields.underlayHubVtepBlockHelp',
        visibleWhen: underlayLocalnet,
      },
      {
        path: 'underlay.hubLegAddress',
        labelKey: 'fields.underlayHubLegAddress',
        widget: 'text',
        visibleWhen: underlayLocalnet,
      },
      {
        path: 'underlay.gatewayAddress',
        labelKey: 'fields.underlayGatewayAddress',
        widget: 'text',
        visibleWhen: underlayLayer2,
      },
      {
        path: 'underlay.dhcpRange.start',
        labelKey: 'fields.underlayDhcpStart',
        widget: 'text',
        visibleWhen: underlayLayer2,
      },
      {
        path: 'underlay.dhcpRange.end',
        labelKey: 'fields.underlayDhcpEnd',
        widget: 'text',
        visibleWhen: underlayLayer2,
      },
      { path: 'underlay.mtu', labelKey: 'fields.underlayMtu', widget: 'number' },
      {
        path: 'borderGateway.name',
        labelKey: 'fields.borderGatewayName',
        widget: 'text',
        helpKey: 'fields.borderGatewayHelp',
      },
      {
        path: 'borderGateway.loopback',
        labelKey: 'fields.borderGatewayLoopback',
        widget: 'text',
        immutable: true,
      },
      {
        path: 'borderGateway.vaultCredentialRef',
        labelKey: 'fields.borderGatewayVaultRef',
        widget: 'text',
      },
      { path: 'borderGateway.wireguard.address', labelKey: 'fields.bgwWireguardAddress', widget: 'text' },
      { path: 'borderGateway.wireguard.listenPort', labelKey: 'fields.bgwWireguardPort', widget: 'number' },
      { path: 'borderGateway.wireguard.mtu', labelKey: 'fields.bgwWireguardMtu', widget: 'number' },
      { path: 'borderGateway.ingressHost', labelKey: 'fields.bgwIngressHost', widget: 'text', omitWhenEmpty: true },
      { path: 'borderGateway.vmSize.cpu', labelKey: 'fields.bgwCpu', widget: 'number' },
      { path: 'borderGateway.vmSize.memory', labelKey: 'fields.bgwMemory', widget: 'text' },
      { path: 'borderGateway.image', labelKey: 'fields.bgwImage', widget: 'text' },
      { path: 'borderGateway.storageClass', labelKey: 'fields.storageClass', widget: 'text', omitWhenEmpty: true },
      {
        path: 'bgp.authentication.secretRef',
        labelKey: 'fields.bgpAuthSecret',
        widget: 'namedRef',
        helpKey: 'fields.bgpAuthSecretHelp',
      },
    ],
  },
  CloudGateway: {
    fields: [
      { path: 'enabled', labelKey: 'fields.enabled', widget: 'boolean' },
      { path: 'fabricRef', labelKey: 'fields.fabricRef', widget: 'text', immutable: true },
      { ...cloudRefField, helpKey: 'fields.gatewayCloudRefHelp' },
      {
        path: 'transport.type',
        labelKey: 'fields.tunnelType',
        widget: 'select',
        options: tunnelOptions,
        helpKey: 'fields.gatewayTransportHelp',
      },
      {
        path: 'transport.vaultPeerConfigRef',
        labelKey: 'fields.vaultPeerConfigRef',
        widget: 'text',
        visibleWhen: { path: 'transport.type', equals: 'wireguard' },
        omitWhenEmpty: true,
      },
      {
        path: 'wireguard.address',
        labelKey: 'fields.gatewayWireguardAddress',
        widget: 'text',
        helpKey: 'fields.gatewayWireguardAddressHelp',
        visibleWhen: { path: 'transport.type', equals: 'wireguard' },
        omitWhenEmpty: true,
      },
      {
        path: 'siteUnderlay.interface',
        labelKey: 'fields.siteUnderlayInterface',
        widget: 'text',
        helpKey: 'fields.siteUnderlayHelp',
      },
      { path: 'siteUnderlay.computeInterface', labelKey: 'fields.siteUnderlayComputeInterface', widget: 'text' },
      { path: 'siteUnderlay.nodeSetInterfaces', labelKey: 'fields.siteUnderlayNodeSetInterfaces', widget: 'json' },
      { path: 'siteUnderlay.cidr', labelKey: 'fields.siteUnderlayCidr', widget: 'text' },
      { path: 'siteUnderlay.gatewayAddress', labelKey: 'fields.siteUnderlayGatewayAddress', widget: 'text' },
      { path: 'siteUnderlay.mtu', labelKey: 'fields.siteUnderlayMtu', widget: 'number' },
      {
        path: 'macNatShim.enabled',
        labelKey: 'fields.macNatShim',
        widget: 'boolean',
        helpKey: 'fields.macNatShimHelp',
      },
      {
        path: 'macNatShim.interface',
        labelKey: 'fields.macNatShimInterface',
        widget: 'text',
        visibleWhen: { path: 'macNatShim.enabled', equals: 'true' },
        omitWhenEmpty: true,
      },
    ],
  },
  TransportLink: {
    fields: [
      { path: 'enabled', labelKey: 'fields.enabled', widget: 'boolean' },
      { path: 'fabricRef', labelKey: 'fields.fabricRef', widget: 'text', immutable: true },
      { path: 'cloudGatewayRef', labelKey: 'fields.cloudGatewayRef', widget: 'text', immutable: true },
      {
        path: 'tunnelType',
        labelKey: 'fields.tunnelType',
        widget: 'select',
        options: tunnelOptions,
        helpKey: 'fields.transportLinkTunnelHelp',
      },
    ],
  },
  UIHealthChecker: {
    fields: [
      { path: 'url', labelKey: 'fields.url', widget: 'text' },
      { path: 'displayName', labelKey: 'fields.displayName', widget: 'text' },
      { path: 'description', labelKey: 'fields.description', widget: 'textarea' },
      { path: 'group', labelKey: 'fields.group', widget: 'text' },
      { path: 'expectedStatus', labelKey: 'fields.expectedStatus', widget: 'number' },
      { path: 'timeoutSeconds', labelKey: 'fields.timeoutSeconds', widget: 'number' },
    ],
  },
  RbacConfig: {
    fields: [
      { path: 'description', labelKey: 'fields.description', widget: 'textarea' },
    ],
  },
  AAPConfig: {
    fields: [
      { path: 'description', labelKey: 'fields.description', widget: 'textarea' },
    ],
  },
  QuayConfig: {
    fields: [
      { path: 'description', labelKey: 'fields.description', widget: 'textarea' },
    ],
  },
};

/** Whether a field applies to the current draft spec (visibleWhen). */
export function isSpecFieldVisible(field: SpecFieldMeta, spec: Record<string, unknown>): boolean {
  const cond = field.visibleWhen;
  if (!cond) return true;
  const raw = getAtPath(spec, cond.path);
  const hasValue = raw != null && raw !== '' && !(Array.isArray(raw) && raw.length === 0);
  if (cond.isSet !== undefined && cond.isSet !== hasValue) return false;
  if (cond.equals !== undefined) {
    const current = raw == null ? '' : String(raw);
    const allowed = Array.isArray(cond.equals) ? cond.equals : [cond.equals];
    return allowed.includes(current);
  }
  return true;
}

export function getAtPath(obj: Record<string, unknown>, path: string): unknown {
  return path.split('.').reduce<unknown>((acc, key) => {
    if (acc && typeof acc === 'object' && !Array.isArray(acc)) {
      return (acc as Record<string, unknown>)[key];
    }
    return undefined;
  }, obj);
}

export function setAtPath(obj: Record<string, unknown>, path: string, value: unknown): Record<string, unknown> {
  const parts = path.split('.');
  const root = { ...obj };
  let cursor: Record<string, unknown> = root;
  for (let i = 0; i < parts.length - 1; i += 1) {
    const key = parts[i];
    const next = cursor[key];
    const copy =
      next && typeof next === 'object' && !Array.isArray(next)
        ? { ...(next as Record<string, unknown>) }
        : {};
    cursor[key] = copy;
    cursor = copy;
  }
  cursor[parts[parts.length - 1]] = value;
  return root;
}
