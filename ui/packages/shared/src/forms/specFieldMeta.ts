import { HybridSovereignKind } from '../types';

export type SpecFieldWidget =
  | 'text'
  | 'textarea'
  | 'number'
  | 'boolean'
  | 'stringList'
  | 'cidrList'
  | 'namedRefList'
  /** `{ name }` (or `{ <refKey> }`) object reference edited as one text input; empty removes it */
  | 'namedRef'
  | 'select'
  /** Array of small objects, one row per item (see columns) */
  | 'objectList'
  | 'json';

/** Show a field only when another spec path matches (type-keyed sections). */
export interface SpecFieldVisibleWhen {
  path: string;
  /** Unset values compare as '' */
  equals?: string | string[];
  /** true: visible only when path has a value; false: only when it has none */
  isSet?: boolean;
}

/** One input of an objectList row */
export interface SpecFieldColumn {
  key: string;
  labelKey: string;
  type?: 'text' | 'number' | 'list' | 'select';
  options?: Array<{ value: string; labelKey: string }>;
  /** select of the names of this kind (object namespace; platform kinds: the platform namespace) */
  refKind?: HybridSovereignKind;
  /** Label of the empty choice of a refKind select (the key is omitted when chosen) */
  emptyLabelKey?: string;
  /** Disabled on items that existed before editing (list-map items whose field is fixed) */
  lockedOnExisting?: boolean;
}

export interface SpecFieldMeta {
  /** Dot path under spec, e.g. features.istio or backend.name */
  path: string;
  labelKey: string;
  widget: SpecFieldWidget;
  /** Cannot change after create (CRD rule self == oldSelf) */
  immutable?: boolean;
  /** Can be set once, then cannot change (CRD rule on an optional field) */
  immutableWhenSet?: boolean;
  options?: Array<{ value: string; labelKey: string }>;
  helpKey?: string;
  visibleWhen?: SpecFieldVisibleWhen;
  /** Empty input removes the key (sent as null) instead of storing '' */
  omitWhenEmpty?: boolean;
  /** When the field is hidden by visibleWhen, saving removes this path (default: the field's own) */
  clearWhenHidden?: boolean | string;
  /** Platform-only field (e.g. a Vault path): not shown on tenant pages */
  adminOnly?: boolean;
  /** namedRef: the key inside the reference object (default name) */
  refKey?: string;
  /** number bounds */
  min?: number;
  max?: number;
  /** objectList */
  columns?: SpecFieldColumn[];
  keyField?: string;
  maxItems?: number;
}

export interface KindSpecMeta {
  fields: SpecFieldMeta[];
}

const transportOptions = [
  { value: 'none', labelKey: 'fields.transportNone' },
  { value: 'wireguard', labelKey: 'fields.transportWireguard' },
];

const transportLinkTunnelOptions = [
  { value: 'wireguard', labelKey: 'fields.tunnelWireguard' },
  { value: 'ipsec', labelKey: 'fields.tunnelIpsec' },
  { value: 'macsec', labelKey: 'fields.tunnelMacsec' },
  { value: 'none', labelKey: 'fields.tunnelNone' },
];

const whenType = (path: string, equals: string | string[]): SpecFieldVisibleWhen => ({ path, equals });

const cinfraOpenstack = whenType('type', 'openstack');
const cinfraOpenshift = whenType('type', 'openshift');
const cinfraAws = whenType('type', 'aws');
const cinfraGcp = whenType('type', 'gcp');
const cinfraExternal = whenType('type', 'external');
const cinfraCreds = whenType('type', ['openstack', 'openshift', 'aws']);
const cinfraDns = whenType('type', ['openstack', 'openshift']);
const dnsNative = whenType('dns.mode', 'native');
const poOpenstack = whenType('type', 'openstack');
const poAws = whenType('type', 'aws');
const poGcp = whenType('type', 'gcp');
const gwNone = whenType('transport', ['none', '']);
const gwWireguard = whenType('transport', 'wireguard');

const MTU = { min: 1280, max: 9216 };
const VNI = { min: 1, max: 16777215 };

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
          { value: 'gcp', labelKey: 'fields.platformGcp' },
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
      { path: 'gcp.environment', labelKey: 'fields.cloudGcpEnv', widget: 'text', immutable: true, visibleWhen: poGcp },
      { path: 'gcp.region', labelKey: 'fields.region', widget: 'text', visibleWhen: poGcp, omitWhenEmpty: true },
      { path: 'gcp.network', labelKey: 'fields.network', widget: 'text', visibleWhen: poGcp, omitWhenEmpty: true },
      {
        path: 'gcp.publish',
        labelKey: 'fields.publish',
        widget: 'select',
        visibleWhen: poGcp,
        options: [
          { value: 'External', labelKey: 'fields.publishExternal' },
          { value: 'Internal', labelKey: 'fields.publishInternal' },
        ],
      },
      { path: 'gcp.controlPlaneCount', labelKey: 'fields.controlPlaneCount', widget: 'number', visibleWhen: poGcp },
      { path: 'gcp.workerCount', labelKey: 'fields.workerCount', widget: 'number', visibleWhen: poGcp },
      { path: 'gcp.controllerFlavor', labelKey: 'fields.controllerFlavor', widget: 'text', visibleWhen: poGcp, omitWhenEmpty: true },
      { path: 'gcp.workerFlavor', labelKey: 'fields.workerFlavor', widget: 'text', visibleWhen: poGcp, omitWhenEmpty: true },
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
          { value: 'gcp', labelKey: 'fields.cloudTypeGcp' },
          { value: 'external', labelKey: 'fields.cloudTypeExternal' },
        ],
      },
      { path: 'displayName', labelKey: 'fields.displayName', widget: 'text', omitWhenEmpty: true },
      {
        path: 'credentialsRef',
        labelKey: 'fields.credentialsVaultPath',
        widget: 'namedRef',
        refKey: 'vaultPath',
        helpKey: 'fields.credentialsRefEditHelp',
        visibleWhen: cinfraCreds,
        adminOnly: true,
      },
      {
        path: 'entityRefs',
        labelKey: 'fields.entityRefs',
        widget: 'namedRefList',
        helpKey: 'fields.cloudInfraEntityRefsHelp',
      },
      { path: 'openstack.region', labelKey: 'fields.region', widget: 'text', helpKey: 'fields.openstackRegionHelp', visibleWhen: cinfraOpenstack, omitWhenEmpty: true },
      { path: 'openstack.projectDomain', labelKey: 'fields.projectDomain', widget: 'text', helpKey: 'fields.siteProjectDomainHelp', visibleWhen: cinfraOpenstack, omitWhenEmpty: true },
      { path: 'openstack.createProjectDomain', labelKey: 'fields.createProjectDomain', widget: 'boolean', helpKey: 'fields.createProjectDomainHelp', visibleWhen: cinfraOpenstack },
      { path: 'openstack.externalNetwork', labelKey: 'fields.externalNetwork', widget: 'text', helpKey: 'fields.siteExternalNetworkHelp', visibleWhen: cinfraOpenstack, omitWhenEmpty: true },
      { path: 'openshift.clusterRef', labelKey: 'fields.clusterRef', widget: 'text', helpKey: 'fields.clusterRefHelp', visibleWhen: cinfraOpenshift, omitWhenEmpty: true },
      { path: 'openshift.bootImage', labelKey: 'fields.bootImage', widget: 'text', helpKey: 'fields.bootImageHelp', visibleWhen: cinfraOpenshift, omitWhenEmpty: true },
      { path: 'openshift.storageClass', labelKey: 'fields.storageClass', widget: 'text', helpKey: 'fields.siteStorageClassHelp', visibleWhen: cinfraOpenshift, omitWhenEmpty: true },
      {
        path: 'openshift.fabricCredentialsRef',
        labelKey: 'fields.fabricCredentialsVaultPath',
        widget: 'namedRef',
        refKey: 'vaultPath',
        helpKey: 'fields.fabricCredentialsHelp',
        visibleWhen: cinfraOpenshift,
        adminOnly: true,
      },
      { path: 'aws.accountId', labelKey: 'fields.awsAccountId', widget: 'text', visibleWhen: cinfraAws },
      { path: 'aws.region', labelKey: 'fields.region', widget: 'text', visibleWhen: cinfraAws },
      { path: 'aws.baseDomain', labelKey: 'fields.baseDomain', widget: 'text', helpKey: 'fields.awsBaseDomainHelp', visibleWhen: cinfraAws, omitWhenEmpty: true },
      { path: 'gcp.region', labelKey: 'fields.region', widget: 'text', visibleWhen: cinfraGcp },
      {
        path: 'gcp.attachment',
        labelKey: 'fields.gcpAttachment',
        widget: 'select',
        visibleWhen: cinfraGcp,
        helpKey: 'fields.gcpAttachmentHelp',
        options: [
          { value: 'vpn', labelKey: 'fields.gcpAttachmentVpn' },
          { value: 'interconnect', labelKey: 'fields.gcpAttachmentInterconnect' },
        ],
      },
      { path: 'external.evpn', labelKey: 'fields.externalEvpn', widget: 'boolean', helpKey: 'fields.externalEvpnHelp', visibleWhen: cinfraExternal },
      {
        path: 'dns.mode',
        labelKey: 'fields.dnsMode',
        widget: 'select',
        helpKey: 'fields.siteDnsHelp',
        visibleWhen: cinfraDns,
        options: [
          { value: 'platform', labelKey: 'fields.dnsModePlatform' },
          { value: 'native', labelKey: 'fields.dnsModeNative' },
        ],
      },
      { path: 'dns.project', labelKey: 'fields.dnsProject', widget: 'text', helpKey: 'fields.dnsProjectHelp', visibleWhen: dnsNative, omitWhenEmpty: true },
      { path: 'dns.projectDomain', labelKey: 'fields.dnsProjectDomain', widget: 'text', helpKey: 'fields.dnsProjectDomainHelp', visibleWhen: dnsNative, omitWhenEmpty: true },
      { path: 'dns.createProject', labelKey: 'fields.dnsCreateProject', widget: 'boolean', visibleWhen: dnsNative },
      { path: 'dns.zoneQuota', labelKey: 'fields.dnsZoneQuota', widget: 'number', helpKey: 'fields.dnsZoneQuotaHelp', visibleWhen: dnsNative, min: 1, omitWhenEmpty: true },
    ],
  },
  CloudOSO: {
    fields: [
      {
        path: 'cloudRef',
        labelKey: 'fields.site',
        widget: 'namedRef',
        immutableWhenSet: true,
        helpKey: 'fields.cloudOsoSiteHelp',
      },
      { path: 'project', labelKey: 'fields.osoProject', widget: 'text' },
      { path: 'landingzone', labelKey: 'fields.landingzone', widget: 'text' },
      { path: 'baseDomain', labelKey: 'fields.baseDomain', widget: 'text', helpKey: 'fields.osoLegacyFieldHelp', omitWhenEmpty: true },
      { path: 'projectDomain', labelKey: 'fields.projectDomain', widget: 'text', helpKey: 'fields.osoLegacyFieldHelp', omitWhenEmpty: true },
      { path: 'externalNetwork', labelKey: 'fields.externalNetwork', widget: 'text', helpKey: 'fields.osoLegacyFieldHelp', omitWhenEmpty: true },
      { path: 'regionName', labelKey: 'fields.regionName', widget: 'text', omitWhenEmpty: true },
      {
        path: 'dnsProvider',
        labelKey: 'fields.dnsProvider',
        widget: 'select',
        helpKey: 'fields.osoLegacyFieldHelp',
        omitWhenEmpty: true,
        options: [
          { value: '', labelKey: 'form.installationDefault' },
          { value: 'designate', labelKey: 'fields.dnsProviderDesignate' },
          { value: 'route53', labelKey: 'fields.dnsProviderRoute53' },
          { value: 'none', labelKey: 'fields.dnsProviderNone' },
        ],
      },
      { path: 'designateZoneId', labelKey: 'fields.designateZoneId', widget: 'text', helpKey: 'fields.osoLegacyFieldHelp', omitWhenEmpty: true },
      { path: 'designateProjectId', labelKey: 'fields.designateProjectId', widget: 'text', helpKey: 'fields.osoLegacyFieldHelp', omitWhenEmpty: true },
      { path: 'vaultPath', labelKey: 'fields.vaultPath', widget: 'text', helpKey: 'fields.osoLegacyFieldHelp', adminOnly: true, omitWhenEmpty: true },
      { path: 'route53VaultPath', labelKey: 'fields.route53VaultPath', widget: 'text', helpKey: 'fields.osoLegacyFieldHelp', adminOnly: true, omitWhenEmpty: true },
      { path: 'enableVRF', labelKey: 'fields.enableVRF', widget: 'boolean' },
      { path: 'vrfId', labelKey: 'fields.vrfId', widget: 'text', omitWhenEmpty: true },
    ],
  },
  CloudAWS: {
    fields: [
      { path: 'account', labelKey: 'fields.account', widget: 'text' },
      { path: 'vaultPath', labelKey: 'fields.vaultPath', widget: 'text' },
      { path: 'baseDomain', labelKey: 'fields.baseDomain', widget: 'text' },
      { path: 'landingzone', labelKey: 'fields.landingzone', widget: 'text' },
    ],
  },
  CloudGCP: {
    fields: [
      { path: 'cloudRef', labelKey: 'fields.site', widget: 'namedRef', immutable: true, helpKey: 'fields.cloudGcpSiteHelp' },
    ],
  },
  CloudVirt: {
    fields: [
      { path: 'description', labelKey: 'fields.description', widget: 'textarea', omitWhenEmpty: true },
      { path: 'cloudRef', labelKey: 'fields.site', widget: 'namedRef', immutable: true, helpKey: 'fields.cloudVirtSiteHelp' },
      {
        path: 'namespaces',
        labelKey: 'fields.vmNamespaces',
        widget: 'objectList',
        keyField: 'name',
        maxItems: 16,
        helpKey: 'fields.vmNamespacesHelp',
        columns: [
          { key: 'name', labelKey: 'fields.namespaceName', lockedOnExisting: true },
          {
            key: 'network',
            labelKey: 'fields.namespaceNetwork',
            type: 'select',
            refKind: 'HybridNetwork',
            emptyLabelKey: 'form.siteDefaultNetwork',
            lockedOnExisting: true,
          },
        ],
      },
      { path: 'quota.cpu', labelKey: 'fields.quotaCpu', widget: 'text', helpKey: 'fields.quotaHelp', omitWhenEmpty: true },
      { path: 'quota.memory', labelKey: 'fields.quotaMemory', widget: 'text', omitWhenEmpty: true },
      { path: 'quota.storage', labelKey: 'fields.quotaStorage', widget: 'text', omitWhenEmpty: true },
      { path: 'quota.virtualMachines', labelKey: 'fields.quotaVirtualMachines', widget: 'number', min: 0, omitWhenEmpty: true },
      { path: 'storageClass', labelKey: 'fields.storageClass', widget: 'text', helpKey: 'fields.cloudVirtStorageClassHelp', omitWhenEmpty: true },
      { path: 'toolRbac.editRbac', labelKey: 'fields.editRbac', widget: 'stringList', helpKey: 'fields.editRbacHelp' },
      { path: 'toolRbac.viewRbac', labelKey: 'fields.viewRbac', widget: 'stringList', helpKey: 'fields.viewRbacHelp' },
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
      { path: 'prefixes', labelKey: 'fields.networkPrefixes', widget: 'cidrList', helpKey: 'fields.networkPrefixesHelp' },
      { path: 'description', labelKey: 'fields.description', widget: 'textarea', omitWhenEmpty: true },
      {
        path: 'fabricRef',
        labelKey: 'fields.fabricRef',
        widget: 'namedRef',
        immutableWhenSet: true,
        helpKey: 'fields.networkFabricRefHelp',
      },
      {
        path: 'mtu',
        labelKey: 'fields.networkMtu',
        widget: 'number',
        helpKey: 'fields.networkMtuHelp',
        omitWhenEmpty: true,
        ...MTU,
      },
      { path: 'egress.internet', labelKey: 'fields.egressInternet', widget: 'boolean', helpKey: 'fields.egressInternetHelp' },
      { path: 'networkViewerRbac', labelKey: 'fields.networkViewerRbac', widget: 'stringList' },
    ],
  },
  NetworkPlacement: {
    fields: [
      { path: 'network', labelKey: 'fields.network', widget: 'text', immutable: true },
      { path: 'backend.kind', labelKey: 'fields.backendKind', widget: 'text', immutable: true },
      { path: 'backend.name', labelKey: 'fields.backendName', widget: 'text', immutable: true },
      { path: 'ipv4.prefixLength', labelKey: 'fields.placementIpv4Size', widget: 'number', immutable: true },
      { path: 'ipv4.prefix', labelKey: 'fields.placementIpv4Pinned', widget: 'text', immutable: true },
      { path: 'ipv6.prefixLength', labelKey: 'fields.placementIpv6Size', widget: 'number', immutable: true },
      { path: 'ipv6.prefix', labelKey: 'fields.placementIpv6Pinned', widget: 'text', immutable: true },
    ],
  },
  HybridFabric: {
    fields: [
      { path: 'domainAsn', labelKey: 'fields.domainAsn', widget: 'number', immutable: true },
      { path: 'overlayMtu', labelKey: 'fields.overlayMtu', widget: 'number', helpKey: 'fields.overlayMtuHelp', ...MTU },
      { path: 'vniPool.start', labelKey: 'fields.vniPoolStart', widget: 'number', helpKey: 'fields.vniPoolHelp', ...VNI },
      { path: 'vniPool.end', labelKey: 'fields.vniPoolEnd', widget: 'number', ...VNI },
      { path: 'l2vniPool.start', labelKey: 'fields.l2vniPoolStart', widget: 'number', helpKey: 'fields.l2vniPoolHelp', ...VNI },
      { path: 'l2vniPool.end', labelKey: 'fields.l2vniPoolEnd', widget: 'number', ...VNI },
      {
        path: 'borderGateway.nodes',
        labelKey: 'fields.borderGatewayNodes',
        widget: 'objectList',
        keyField: 'name',
        maxItems: 8,
        helpKey: 'fields.borderGatewayNodesHelp',
        columns: [
          { key: 'name', labelKey: 'fields.nodeName' },
          { key: 'address', labelKey: 'fields.nodeAddress' },
        ],
      },
      {
        path: 'borderGateway.credentialsRef',
        labelKey: 'fields.borderGatewayCredentialsVaultPath',
        widget: 'namedRef',
        refKey: 'vaultPath',
        helpKey: 'fields.borderGatewayCredentialsHelp',
        adminOnly: true,
      },
      {
        path: 'bgp.passwordRef',
        labelKey: 'fields.bgpPasswordVaultPath',
        widget: 'namedRef',
        refKey: 'vaultPath',
        helpKey: 'fields.fabricBgpPasswordHelp',
        adminOnly: true,
      },
      {
        path: 'entityRefs',
        labelKey: 'fields.entityRefs',
        widget: 'namedRefList',
        helpKey: 'fields.fabricEntityRefsHelp',
      },
    ],
  },
  CloudGateway: {
    fields: [
      { path: 'fabricRef', labelKey: 'fields.fabricRef', widget: 'namedRef', immutable: true },
      { path: 'cloudRef', labelKey: 'fields.site', widget: 'namedRef', immutable: true, helpKey: 'fields.gatewayCloudRefHelp' },
      {
        path: 'transport',
        labelKey: 'fields.transport',
        widget: 'select',
        options: transportOptions,
        helpKey: 'fields.gatewayTransportHelp',
      },
      {
        path: 'siteUnderlay.vlan',
        labelKey: 'fields.siteUnderlayVlan',
        widget: 'number',
        helpKey: 'fields.siteUnderlayVlanHelp',
        visibleWhen: gwNone,
        clearWhenHidden: true,
        min: 1,
        max: 4094,
      },
      {
        path: 'wireguard.listenPort',
        labelKey: 'fields.wireguardListenPort',
        widget: 'number',
        helpKey: 'fields.wireguardListenPortHelp',
        visibleWhen: gwWireguard,
        clearWhenHidden: 'wireguard',
        omitWhenEmpty: true,
        min: 1024,
        max: 65535,
      },
      {
        path: 'wireguard.peers',
        labelKey: 'fields.wireguardPeers',
        widget: 'objectList',
        helpKey: 'fields.wireguardPeersHelp',
        visibleWhen: gwWireguard,
        clearWhenHidden: 'wireguard',
        columns: [
          { key: 'publicKey', labelKey: 'fields.wireguardPublicKey' },
          { key: 'endpoint', labelKey: 'fields.wireguardEndpoint' },
          { key: 'allowedIPs', labelKey: 'fields.wireguardAllowedIps', type: 'list' },
          { key: 'persistentKeepalive', labelKey: 'fields.wireguardKeepalive', type: 'number' },
        ],
      },
      { path: 'siteUnderlay.cidr', labelKey: 'fields.siteUnderlayCidr', widget: 'text', helpKey: 'fields.siteUnderlayCidrEditHelp' },
      {
        path: 'siteUnderlay.addresses',
        labelKey: 'fields.siteUnderlayAddresses',
        widget: 'objectList',
        keyField: 'node',
        maxItems: 8,
        helpKey: 'fields.siteUnderlayAddressesHelp',
        columns: [
          { key: 'node', labelKey: 'fields.borderGatewayNode' },
          { key: 'address', labelKey: 'fields.nodeUnderlayAddress' },
        ],
      },
      { path: 'siteUnderlay.mtu', labelKey: 'fields.siteUnderlayMtu', widget: 'number', helpKey: 'fields.siteUnderlayMtuHelp', omitWhenEmpty: true, ...MTU },
      { path: 'siteUnderlay.gateway', labelKey: 'fields.siteUnderlayGateway', widget: 'text', helpKey: 'fields.siteUnderlayGatewayHelp', omitWhenEmpty: true },
      {
        path: 'siteUnderlay.upstreamRouteReflectors',
        labelKey: 'fields.upstreamRouteReflectors',
        widget: 'stringList',
        helpKey: 'fields.upstreamRouteReflectorsHelp',
      },
      { path: 'bfd', labelKey: 'fields.bfd', widget: 'boolean', helpKey: 'fields.bfdHelp' },
      {
        path: 'bgp.passwordRef',
        labelKey: 'fields.bgpPasswordVaultPath',
        widget: 'namedRef',
        refKey: 'vaultPath',
        helpKey: 'fields.gatewayBgpPasswordHelp',
        adminOnly: true,
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
        options: transportLinkTunnelOptions,
      },
      { path: 'vaultConfigRef', labelKey: 'fields.vaultConfigRef', widget: 'text', adminOnly: true },
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
