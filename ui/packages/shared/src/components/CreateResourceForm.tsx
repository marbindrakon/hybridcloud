import React, { useEffect, useState } from 'react';
import {
  Form,
  FormGroup,
  TextInput,
  TextArea,
  Button,
  ActionGroup,
  Alert,
  Switch,
  Card,
  CardBody,
  FormSelect,
  FormSelectOption,
  ExpandableSection,
} from '@patternfly/react-core';
import { createDashboardResource, createNamespaceSecret, useK8sResourceList } from '../hooks/k8s';
import { HybridSovereignKind, K8sResource } from '../types';
import { PageHeader } from './PageHeader';
import { RbacMultiSelect } from './RbacMultiSelect';
import { EntityMultiSelect } from './EntityMultiSelect';
import { FabricSelect } from './FabricSelect';
import { CloudGatewaySelect } from './CloudGatewaySelect';
import { CloudInfrastructureSelect } from './CloudInfrastructureSelect';
import { BackendSelect, buildBackendOptions, type BackendSelectValue } from './BackendSelect';
import type { CloudInfrastructure, CloudInfrastructureType } from '../types';
import { useTranslation } from '../i18n';
import {
  CLOUD_INFRASTRUCTURE_TYPES,
  DEFAULT_CLOUD_INFRA_OPENSHIFT,
  DEFAULT_CLOUD_INFRA_OPENSTACK,
  DEFAULT_FABRIC_BORDER_GATEWAY,
  DEFAULT_FABRIC_DOMAIN_ASN,
  DEFAULT_FABRIC_UNDERLAY,
  DEFAULT_FABRIC_TRANSPORT,
  DEFAULT_FABRIC_VNI,
  FABRIC_TUNNEL_TYPES,
  cloudInfrastructureNeedsCredentials,
  parseCidrList,
  type FabricTunnelType,
  type FabricUnderlayType,
} from '../forms/hybridFabricDefaults';

/** Kubernetes namespace name (DNS label), as NetworkPlacement.spec.vmNamespaces requires. */
const DNS_LABEL = /^[a-z0-9]([-a-z0-9]*[a-z0-9])?$/;

export type SelfServiceFormType =
  | 'team'
  | 'project'
  | 'assignment'
  | 'cloudoso'
  | 'cloudaws'
  | 'cloudvirt'
  | 'platformopenshift'
  | 'migration'
  | 'persona'
  | 'rbac'
  | 'vault'
  | 'vaultkv'
  | 'aaporg'
  | 'quayorg'
  | 'entity'
  | 'hybridnetwork'
  | 'networkplacement'
  | 'hybridfabric'
  | 'cloudinfrastructure'
  | 'cloudgateway'
  | 'transportlink'
  | 'uihealthchecker';

export interface CreateResourceFormProps {
  formType: SelfServiceFormType;
  namespace: string;
  listPath: string;
  onSuccess: (listPath: string) => void;
  onCancel: () => void;
}

const FORM_TITLES: Record<SelfServiceFormType, string> = {
  team: 'Create Team',
  project: 'Create Project',
  assignment: 'Create Assignment',
  cloudoso: 'Request CloudOSO Environment',
  cloudaws: 'Request Cloud AWS Account',
  cloudvirt: 'Request Cloud Virt Environment',
  platformopenshift: 'Create Platform Openshift',
  migration: 'Migrate to OpenStack',
  persona: 'Create Persona',
  rbac: 'Create RBAC',
  vault: 'Create Vault',
  vaultkv: 'Create Vault KV',
  aaporg: 'Create AAP Org',
  quayorg: 'Create Quay Org',
  entity: 'Create Entity',
  hybridnetwork: 'Create Hybrid Network',
  networkplacement: 'Create Network Placement',
  hybridfabric: 'Create Hybrid Fabric',
  cloudinfrastructure: 'Create Cloud Infrastructure',
  cloudgateway: 'Create Cloud Gateway',
  transportlink: 'Create Transport Link',
  uihealthchecker: 'Create UI Health Checker',
};

const FORM_KINDS: Record<SelfServiceFormType, HybridSovereignKind> = {
  team: 'Team',
  project: 'Project',
  assignment: 'Assignment',
  cloudoso: 'CloudOSO',
  cloudaws: 'CloudAWS',
  cloudvirt: 'CloudVirt',
  platformopenshift: 'PlatformOpenshift',
  migration: 'OpenStackMigration',
  persona: 'Persona',
  rbac: 'Rbac',
  vault: 'Vault',
  vaultkv: 'VaultKV',
  aaporg: 'AAPOrg',
  quayorg: 'QuayOrg',
  entity: 'Entity',
  hybridnetwork: 'HybridNetwork',
  networkplacement: 'NetworkPlacement',
  hybridfabric: 'HybridFabric',
  cloudinfrastructure: 'CloudInfrastructure',
  cloudgateway: 'CloudGateway',
  transportlink: 'TransportLink',
  uihealthchecker: 'UIHealthChecker',
};

const PERSONA_TYPES = [
  'entityAdmin',
  'identityAdmin',
  'auditor',
  'teamAdmin',
  'teamView',
  'projectAdmin',
  'projectView',
  'assignmentAdmin',
  'platformOpenshiftAdmin',
  'platformOpenshiftView',
  'cloudOSOAdmin',
  'cloudOSOView',
  'cloudAWSAdmin',
  'cloudAWSView',
  'cloudVirtAdmin',
  'cloudVirtView',
  'BobsTeam',
];

function RefSelect({
  id,
  label,
  value,
  onChange,
  options,
  isRequired,
  placeholder = 'Select…',
}: {
  id: string;
  label: string;
  value: string;
  onChange: (v: string) => void;
  options: { value: string; label: string }[];
  isRequired?: boolean;
  placeholder?: string;
}): React.ReactElement {
  return (
    <FormGroup label={label} isRequired={isRequired} fieldId={id}>
      <FormSelect id={id} value={value} onChange={(_e, v) => onChange(v)} isRequired={isRequired}>
        <FormSelectOption value="" label={placeholder} isDisabled={isRequired} />
        {options.map((o) => (
          <FormSelectOption key={o.value} value={o.value} label={o.label} />
        ))}
      </FormSelect>
    </FormGroup>
  );
}

/** Router-agnostic create form used by standalone dashboards and console plugins. */
export function CreateResourceForm({
  formType,
  namespace,
  listPath,
  onSuccess,
  onCancel,
}: CreateResourceFormProps): React.ReactElement {
  const [name, setName] = useState('');
  const [description, setDescription] = useState('');
  const [billingID, setBillingID] = useState('');
  const [websiteLink, setWebsiteLink] = useState('');
  const [teamRef, setTeamRef] = useState('');
  const [projectRef, setProjectRef] = useState('');
  const [platformRef, setPlatformRef] = useState('');
  const [cloudAwsRef, setCloudAwsRef] = useState('');
  const [cloudVirtRef, setCloudVirtRef] = useState('');
  const [argoEnabled, setArgoEnabled] = useState(true);
  const [rbacRef, setRbacRef] = useState('');
  const [personaType, setPersonaType] = useState('entityAdmin');
  const [rbacConfig, setRbacConfig] = useState('');
  const [vaultRef, setVaultRef] = useState('');
  const [cloudosoRef, setCloudosoRef] = useState('');
  const [vmName, setVmName] = useState('');
  const [source, setSource] = useState('vmware');
  const [aapConfig, setAapConfig] = useState('');
  const [quayConfig, setQuayConfig] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [result, setResult] = useState<{ ok: boolean; message: string } | null>(null);
  const [networkRef, setNetworkRef] = useState('');
  const [backendKind, setBackendKind] = useState('');
  const [backendName, setBackendName] = useState('');
  const [prefixes, setPrefixes] = useState('10.50.0.0/24');
  const [domainAsn, setDomainAsn] = useState(String(DEFAULT_FABRIC_DOMAIN_ASN));
  const [vniStart, setVniStart] = useState(String(DEFAULT_FABRIC_VNI.start));
  const [vniEnd, setVniEnd] = useState(String(DEFAULT_FABRIC_VNI.end));
  const [fabricRef, setFabricRef] = useState('lab-fabric');
  /** Optional HybridNetwork.spec.fabricRef when Entity has multiple fabrics (§19.6) */
  const [networkFabricRef, setNetworkFabricRef] = useState('');
  const [region, setRegion] = useState('us-east-1');
  const [gatewayRef, setGatewayRef] = useState('');
  const [healthUrl, setHealthUrl] = useState('https://');
  const [healthGroup, setHealthGroup] = useState('custom');
  const [istioEnabled, setIstioEnabled] = useState(false);
  const [haEnabled, setHaEnabled] = useState(true);
  const [displayName, setDisplayName] = useState('');
  const [projectRefs, setProjectRefs] = useState('');
  const [assignAdmin, setAssignAdmin] = useState<string[]>([]);
  const [assignDev, setAssignDev] = useState<string[]>([]);
  const [assignViewer, setAssignViewer] = useState<string[]>([]);
  const [assignOps, setAssignOps] = useState<string[]>([]);
  const [osoProject, setOsoProject] = useState('');
  const [baseDomain, setBaseDomain] = useState('');
  // CloudOSO: unset values fall back to the CloudInfrastructure (prefilled from it when readable).
  const [projectDomain, setProjectDomain] = useState('');
  const [externalNetwork, setExternalNetwork] = useState('');
  const [osoDesignate, setOsoDesignate] = useState<{ zoneId?: string; projectId?: string }>({});
  const [route53VaultPath, setRoute53VaultPath] = useState('');
  const [landingzone, setLandingzone] = useState('default');
  const [awsAccount, setAwsAccount] = useState('');
  const [awsVaultPath, setAwsVaultPath] = useState('');
  const [awsBaseDomain, setAwsBaseDomain] = useState('');
  const [awsAccessKeyId, setAwsAccessKeyId] = useState('');
  const [awsSecretAccessKey, setAwsSecretAccessKey] = useState('');
  const [virtBaseDomain, setVirtBaseDomain] = useState('');
  const [virtStorageClass, setVirtStorageClass] = useState('ocs-storagecluster-ceph-rbd');
  const [platformType, setPlatformType] = useState('openstack');
  const [platformEnv, setPlatformEnv] = useState('');
  const [cpCount, setCpCount] = useState('3');
  const [workerCount, setWorkerCount] = useState('3');
  const [nodePoolReplicas, setNodePoolReplicas] = useState('2');
  // PlatformOpenshift explicit address plan; omitted CIDRs are allocated from the CloudInfrastructure.
  const [poClusterNetwork, setPoClusterNetwork] = useState('');
  const [poServiceNetwork, setPoServiceNetwork] = useState('');
  const [rbacMulti, setRbacMulti] = useState<string[]>([]);
  const [rbacOperator, setRbacOperator] = useState<string[]>([]);
  const [rbacViewer, setRbacViewer] = useState<string[]>([]);
  const [networkViewerRbac, setNetworkViewerRbac] = useState<string[]>([]);
  const [personaRbacs, setPersonaRbacs] = useState<string[]>([]);
  // HybridFabric — entity tagging (§18.3) + full fabric options with lab defaults
  const [fabricEntityRefs, setFabricEntityRefs] = useState<string[]>([]);
  const [fabricEnabled, setFabricEnabled] = useState(true);
  const [fabricAdvancedOpen, setFabricAdvancedOpen] = useState(false);
  const [fabricTunnelType, setFabricTunnelType] = useState<FabricTunnelType>(
    DEFAULT_FABRIC_TRANSPORT.defaultTunnelType,
  );
  const [fabricMtu, setFabricMtu] = useState(String(DEFAULT_FABRIC_TRANSPORT.mtu));
  const [fabricUnderlayType, setFabricUnderlayType] = useState<FabricUnderlayType>(DEFAULT_FABRIC_UNDERLAY.type);
  const [fabricUnderlayPhysnet, setFabricUnderlayPhysnet] = useState(DEFAULT_FABRIC_UNDERLAY.physicalNetworkName);
  const [fabricUnderlayNad, setFabricUnderlayNad] = useState(DEFAULT_FABRIC_UNDERLAY.nadName);
  const [fabricUnderlayCidr, setFabricUnderlayCidr] = useState(DEFAULT_FABRIC_UNDERLAY.cidr);
  const [fabricHubVtepBlock, setFabricHubVtepBlock] = useState(DEFAULT_FABRIC_UNDERLAY.hubVtepBlock);
  const [fabricHubLegAddress, setFabricHubLegAddress] = useState(DEFAULT_FABRIC_UNDERLAY.hubLegAddress);
  const [fabricUnderlayGateway, setFabricUnderlayGateway] = useState('');
  const [fabricUnderlayMtu, setFabricUnderlayMtu] = useState(String(DEFAULT_FABRIC_UNDERLAY.mtu));
  const [fabricBgwName, setFabricBgwName] = useState(DEFAULT_FABRIC_BORDER_GATEWAY.name);
  const [fabricBgwLoopback, setFabricBgwLoopback] = useState(DEFAULT_FABRIC_BORDER_GATEWAY.loopback);
  const [fabricBgwVaultRef, setFabricBgwVaultRef] = useState('');
  const [fabricBgwWgAddress, setFabricBgwWgAddress] = useState(DEFAULT_FABRIC_BORDER_GATEWAY.wireguardAddress);
  const [fabricBgwWgPort, setFabricBgwWgPort] = useState(String(DEFAULT_FABRIC_BORDER_GATEWAY.wireguardListenPort));
  // Tenant cloud projects and CloudGateway — spec.cloudRef.name (CloudInfrastructure in sovereign-cloud)
  const [cloudRefName, setCloudRefName] = useState('');
  /** Type of the selected CloudInfrastructure when the list is readable ('' = unknown). */
  const [cloudRefType, setCloudRefType] = useState<CloudInfrastructureType | ''>('');
  // CloudGateway — transport ('' = fabric transportDefaults.defaultTunnelType) + OpenStack site underlay
  const [gatewayTransport, setGatewayTransport] = useState<FabricTunnelType | ''>('');
  const [gatewayWgAddress, setGatewayWgAddress] = useState('');
  const [siteUnderlayInterface, setSiteUnderlayInterface] = useState('');
  const [siteComputeInterface, setSiteComputeInterface] = useState('');
  const [siteUnderlayCidr, setSiteUnderlayCidr] = useState('');
  const [siteUnderlayGateway, setSiteUnderlayGateway] = useState('');
  // NetworkPlacement (CloudVirt) — hub namespaces the operator creates; empty = <cloudvirt>-<network>
  const [vmNamespaces, setVmNamespaces] = useState('');
  // HybridNetwork — optional overlay MTU (defaults from the fabric transport MTU)
  const [overlayMtu, setOverlayMtu] = useState('');
  // CloudInfrastructure
  const [cinfraType, setCinfraType] = useState<CloudInfrastructureType>('openstack');
  const [cinfraCredMode, setCinfraCredMode] = useState<'vault' | 'secret'>('vault');
  const [cinfraCredVaultPath, setCinfraCredVaultPath] = useState('');
  const [cinfraCredSecret, setCinfraCredSecret] = useState('');
  const [cinfraEntityRefs, setCinfraEntityRefs] = useState<string[]>([]);
  const [cinfraRegion, setCinfraRegion] = useState(DEFAULT_CLOUD_INFRA_OPENSTACK.region);
  const [cinfraMgmtKubeconfig, setCinfraMgmtKubeconfig] = useState('');
  const [cinfraNetConfig, setCinfraNetConfig] = useState(DEFAULT_CLOUD_INFRA_OPENSTACK.netConfigRef);
  const [cinfraNodeSets, setCinfraNodeSets] = useState('');
  const [cinfraExternalNetwork, setCinfraExternalNetwork] = useState(DEFAULT_CLOUD_INFRA_OPENSTACK.externalNetwork);
  const [cinfraBaseDomain, setCinfraBaseDomain] = useState('');
  const [cinfraProjectDomain, setCinfraProjectDomain] = useState(DEFAULT_CLOUD_INFRA_OPENSTACK.projectDomain);
  const [cinfraDesignateZone, setCinfraDesignateZone] = useState('');
  const [cinfraDesignateProject, setCinfraDesignateProject] = useState('');
  const [cinfraClusterRef, setCinfraClusterRef] = useState(DEFAULT_CLOUD_INFRA_OPENSHIFT.clusterRef);
  const [cinfraBootImage, setCinfraBootImage] = useState(DEFAULT_CLOUD_INFRA_OPENSHIFT.bootImage);
  const [cinfraStorageClass, setCinfraStorageClass] = useState('');
  const [cinfraPodPool, setCinfraPodPool] = useState(DEFAULT_CLOUD_INFRA_OPENSHIFT.clusterNetworkPool.cidr);
  const [cinfraPodBlock, setCinfraPodBlock] = useState(String(DEFAULT_CLOUD_INFRA_OPENSHIFT.clusterNetworkPool.blockPrefixLength));
  const [cinfraSvcPool, setCinfraSvcPool] = useState(DEFAULT_CLOUD_INFRA_OPENSHIFT.serviceNetworkPool.cidr);
  const [cinfraSvcBlock, setCinfraSvcBlock] = useState(String(DEFAULT_CLOUD_INFRA_OPENSHIFT.serviceNetworkPool.blockPrefixLength));
  const [cinfraAwsAccount, setCinfraAwsAccount] = useState('');

  const { t } = useTranslation();
  const type = formType;
  const title = t(`form.titles.${type}`, { defaultValue: FORM_TITLES[type] ?? 'Create Resource' });
  const kind = FORM_KINDS[type];
  const [entityName, setEntityName] = useState('');
  const entityNs =
    type === 'entity'
      ? 'sovereign-cloud'
      : type === 'persona' && !namespace
        ? entityName
          ? `entity-${entityName}`
          : ''
        : namespace;

  const entities = useK8sResourceList<K8sResource>('Entity', {
    namespace: 'sovereign-cloud',
    enabled: (type === 'persona' && !namespace) || type === 'hybridfabric' || type === 'cloudinfrastructure',
  });

  const teams = useK8sResourceList<K8sResource>('Team', { namespace: entityNs, enabled: type === 'assignment' });
  const projects = useK8sResourceList<K8sResource>('Project', {
    namespace: entityNs,
    enabled: type === 'assignment',
  });
  const platforms = useK8sResourceList<K8sResource>('PlatformOpenshift', {
    namespace: entityNs,
    enabled: type === 'assignment',
  });
  const cloudAwss = useK8sResourceList<K8sResource>('CloudAWS', {
    namespace: entityNs,
    enabled: type === 'assignment' || type === 'platformopenshift',
  });
  const cloudVirts = useK8sResourceList<K8sResource>('CloudVirt', {
    namespace: entityNs,
    enabled: type === 'platformopenshift',
  });
  const vaults = useK8sResourceList<K8sResource>('Vault', {
    namespace: entityNs,
    enabled: type === 'vaultkv',
  });
  const cloudosos = useK8sResourceList<K8sResource>('CloudOSO', {
    namespace: entityNs,
    enabled: type === 'migration' || type === 'platformopenshift',
  });
  const rbacs = useK8sResourceList<K8sResource>('Rbac', {
    namespace: entityNs,
    enabled: (type === 'persona' || type === 'vaultkv' || type === 'aaporg' || type === 'quayorg' || type === 'assignment' || type === 'hybridnetwork' || type === 'platformopenshift') && !!entityNs,
  });
  const rbacConfigs = useK8sResourceList<K8sResource>('RbacConfig', {
    enabled: type === 'rbac' || type === 'vault',
  });
  const aapConfigs = useK8sResourceList<K8sResource>('AAPConfig', { enabled: type === 'aaporg' });
  const quayConfigs = useK8sResourceList<K8sResource>('QuayConfig', { enabled: type === 'quayorg' });
  const hybridNetworks = useK8sResourceList<K8sResource>('HybridNetwork', { namespace: entityNs, enabled: type === 'networkplacement' && !!entityNs });
  // BackendSelect — CloudOSO / CloudVirt projects in the entity namespace.
  const cloudososForPlacement = useK8sResourceList<K8sResource>('CloudOSO', { namespace: entityNs, enabled: type === 'networkplacement' && !!entityNs });
  const cloudvirtsForPlacement = useK8sResourceList<K8sResource>('CloudVirt', { namespace: entityNs, enabled: type === 'networkplacement' && !!entityNs });
  const fabrics = useK8sResourceList<K8sResource>('HybridFabric', {
    namespace: 'sovereign-cloud',
    enabled: type === 'cloudgateway' || type === 'transportlink' || type === 'hybridnetwork',
  });
  const gateways = useK8sResourceList<K8sResource>('CloudGateway', { namespace: 'sovereign-cloud', enabled: type === 'transportlink' });


  const names = (items: K8sResource[] | undefined | null) =>
    (items ?? [])
      .map((i) => i?.metadata?.name)
      .filter((n): n is string => typeof n === 'string' && n.length > 0)
      .map((n) => ({ value: n, label: n }));

  const firstName = (items: K8sResource[] | undefined | null): string | undefined =>
    names(items)[0]?.value;

  /** Options with a Ready badge — used by EntityMultiSelect / FabricSelect. */
  const namesWithReady = (items: K8sResource[] | undefined | null) =>
    (items ?? [])
      .filter((i): i is K8sResource => typeof i?.metadata?.name === 'string' && i.metadata.name.length > 0)
      .map((i) => ({
        value: i.metadata.name,
        label: i.metadata.name,
        ready: (i.status as { ready?: boolean } | undefined)?.ready,
      }));

  const currentEntityName = (entityNs || '').replace(/^entity-/, '');

  /** Entity names tagged on a given HybridFabric (spec.entityRefs). */
  const entityRefsOfFabric = (fabricName: string): string[] =>
    (fabrics.items.find((f) => f.metadata.name === fabricName)?.spec as
      | { entityRefs?: Array<{ name: string }> }
      | undefined)?.entityRefs?.map((r) => r.name) ?? [];

  /** FabricSelect options — disabled (greyed) when this Entity is not tagged on the fabric. */
  const fabricOptionsForEntity = fabrics.items.map((f) => ({
    value: f.metadata.name,
    label: f.metadata.name,
    ready: (f.status as { ready?: boolean } | undefined)?.ready,
    isDisabled: !entityRefsOfFabric(f.metadata.name).includes(currentEntityName),
  }));

  /** FabricSelect options for CloudGateway / TransportLink — Ready fabrics, no entity filter. */
  const fabricSelectOptions = fabrics.items.length
    ? fabrics.items.map((f) => ({
        value: f.metadata.name,
        label: f.metadata.name,
        ready: (f.status as { ready?: boolean } | undefined)?.ready,
      }))
    : [{ value: 'lab-fabric', label: 'lab-fabric' }];

  /** CloudGatewaySelect options for TransportLink — gateways for the selected fabric, re-filters on change (§18.11). */
  const gatewayOptionsForFabric = gateways.items
    .filter((g) => !fabricRef || (g.spec as { fabricRef?: string } | undefined)?.fabricRef === fabricRef)
    .map((g) => ({
      value: g.metadata.name,
      label: g.metadata.name,
      ready: (g.status as { ready?: boolean } | undefined)?.ready,
    }));

  /** BackendSelect options for NetworkPlacement — CloudOSO and CloudVirt projects. */
  const backendOptions = buildBackendOptions({
    cloudosos: cloudososForPlacement.items,
    cloudvirts: cloudvirtsForPlacement.items,
  });

  /** Picking a CloudInfrastructure: remember its type and prefill site values the project inherits. */
  const onCloudInfrastructurePicked = (item: CloudInfrastructure | undefined) => {
    setCloudRefType(item?.spec.type ?? '');
    const os = item?.spec.openstack;
    if (type === 'cloudoso' && os) {
      setProjectDomain(os.projectDomain ?? '');
      setExternalNetwork(os.externalNetwork ?? '');
      setOsoDesignate({ zoneId: os.designate?.zoneId, projectId: os.designate?.projectId });
      if (!baseDomain && os.baseDomain) setBaseDomain(os.baseDomain);
      if (!route53VaultPath && os.route53VaultPath) setRoute53VaultPath(os.route53VaultPath);
    }
    if (type === 'cloudaws' && item?.spec.aws?.baseDomain && !awsBaseDomain) {
      setAwsBaseDomain(item.spec.aws.baseDomain);
    }
  };

  const vmNamespaceList = parseCidrList(vmNamespaces);
  const vmNamespacesValid =
    vmNamespaceList.length <= 16 && vmNamespaceList.every((n) => n.length <= 63 && DNS_LABEL.test(n));
  const overlayMtuValid =
    !overlayMtu.trim() || (Number(overlayMtu) >= 1280 && Number(overlayMtu) <= 9216);
  const cinfraCredsSet =
    cinfraCredMode === 'vault' ? !!cinfraCredVaultPath.trim() : !!cinfraCredSecret.trim();

  useEffect(() => {
    if (type === 'persona' && !namespace && !entityName) {
      const n = firstName(entities.items);
      if (n) setEntityName(n);
    }
    if (type === 'rbac' || type === 'vault') {
      if (!rbacConfig) {
        const n = firstName(rbacConfigs.items);
        if (n) setRbacConfig(n);
      }
    }
    if (type === 'aaporg' && !aapConfig) {
      const n = firstName(aapConfigs.items);
      if (n) setAapConfig(n);
    }
    if (type === 'quayorg' && !quayConfig) {
      const n = firstName(quayConfigs.items);
      if (n) setQuayConfig(n);
    }
    if (type === 'assignment' && !teamRef) {
      const n = firstName(teams.items);
      if (n) setTeamRef(n);
    }
    if (type === 'persona' && personaRbacs.length === 0 && !rbacRef) {
      const n = firstName(rbacs.items);
      if (n) {
        setPersonaRbacs([n]);
        setRbacRef(n);
      }
    }
    if (type === 'vaultkv' && !vaultRef) {
      const n = firstName(vaults.items);
      if (n) setVaultRef(n);
    }
    if (type === 'migration' && !cloudosoRef) {
      const n = firstName(cloudosos.items);
      if (n) setCloudosoRef(n);
    }
  }, [
    type,
    namespace,
    entityName,
    entities.items,
    rbacConfigs.items,
    aapConfigs.items,
    quayConfigs.items,
    teams.items,
    rbacs.items,
    vaults.items,
    cloudosos.items,
    rbacConfig,
    aapConfig,
    quayConfig,
    teamRef,
    rbacRef,
    personaRbacs,
    vaultRef,
    cloudosoRef,
  ]);

  const buildSpec = (): Record<string, unknown> => {
    switch (type) {
      case 'entity':
        return {
          description,
          billingID,
          ...(websiteLink ? { websiteLink } : {}),
        };
      case 'team':
        return {
          features: { argo: argoEnabled, istio: istioEnabled },
        };
      case 'project':
        return {
          description,
          ...(displayName ? { displayName } : {}),
        };
      case 'assignment': {
        const projectsList = projectRefs
          ? projectRefs.split(/[,\n]/).map((s) => s.trim()).filter(Boolean)
          : projectRef
            ? [projectRef]
            : [];
        // Assignment CRD toolRbac fields are single strings — use first selected.
        const toolRbac: Record<string, string> = {};
        if (assignAdmin[0]) toolRbac.assignmentAdmin = assignAdmin[0];
        if (assignDev[0]) toolRbac.assignmentDeveloper = assignDev[0];
        if (assignViewer[0]) toolRbac.assignmentViewer = assignViewer[0];
        if (assignOps[0]) toolRbac.assignmentOps = assignOps[0];
        return {
          team: teamRef,
          projects: projectsList,
          openshift: platformRef || undefined,
          ...(Object.keys(toolRbac).length ? { toolRbac } : {}),
        };
      }
      case 'cloudoso':
        // Credentials and site settings come from the CloudInfrastructure (no vaultPath / secret here).
        return {
          cloudRef: { kind: 'CloudInfrastructure', name: cloudRefName },
          project: osoProject,
          baseDomain,
          landingzone,
          ...(projectDomain.trim() ? { projectDomain: projectDomain.trim() } : {}),
          ...(externalNetwork.trim() ? { externalNetwork: externalNetwork.trim() } : {}),
          ...(osoDesignate.zoneId ? { designateZoneId: osoDesignate.zoneId } : {}),
          ...(osoDesignate.projectId ? { designateProjectId: osoDesignate.projectId } : {}),
          ...(route53VaultPath.trim() ? { route53VaultPath: route53VaultPath.trim() } : {}),
        };
      case 'cloudaws': {
        if (cloudRefName) {
          // Credentials come from the CloudInfrastructure.
          return {
            cloudRef: { kind: 'CloudInfrastructure', name: cloudRefName },
            account: awsAccount,
            baseDomain: awsBaseDomain || baseDomain,
            landingzone,
          };
        }
        const credName = `${name}-aws-credentials`;
        return {
          account: awsAccount,
          baseDomain: awsBaseDomain || baseDomain,
          landingzone,
          ...(awsAccessKeyId.trim() && awsSecretAccessKey.trim()
            ? { credentialsSecretRef: { name: credName } }
            : { vaultPath: awsVaultPath }),
        };
      }
      case 'cloudvirt':
        return {
          cloudRef: { kind: 'CloudInfrastructure', name: cloudRefName },
          baseDomain: virtBaseDomain || baseDomain,
          storageClass: virtStorageClass || undefined,
        };
      case 'cloudinfrastructure': {
        const credentialsRef = cloudInfrastructureNeedsCredentials(cinfraType)
          ? cinfraCredMode === 'vault'
            ? { vaultPath: cinfraCredVaultPath.trim() }
            : { secretRef: { name: cinfraCredSecret.trim() } }
          : undefined;
        const common = {
          type: cinfraType,
          ...(displayName.trim() ? { displayName: displayName.trim() } : {}),
          ...(credentialsRef ? { credentialsRef } : {}),
          ...(cinfraEntityRefs.length ? { entityRefs: cinfraEntityRefs.map((n) => ({ name: n })) } : {}),
        };
        if (cinfraType === 'openstack') {
          const nodeSets = parseCidrList(cinfraNodeSets);
          return {
            ...common,
            openstack: {
              region: cinfraRegion.trim() || DEFAULT_CLOUD_INFRA_OPENSTACK.region,
              ...(cinfraMgmtKubeconfig.trim() ? { managementClusterKubeconfigRef: cinfraMgmtKubeconfig.trim() } : {}),
              netConfigRef: cinfraNetConfig.trim() || DEFAULT_CLOUD_INFRA_OPENSTACK.netConfigRef,
              ...(nodeSets.length ? { dataplaneNodeSetRefs: nodeSets } : {}),
              ...(cinfraExternalNetwork.trim() ? { externalNetwork: cinfraExternalNetwork.trim() } : {}),
              ...(cinfraBaseDomain.trim() ? { baseDomain: cinfraBaseDomain.trim() } : {}),
              ...(cinfraProjectDomain.trim() ? { projectDomain: cinfraProjectDomain.trim() } : {}),
              ...(cinfraDesignateZone.trim() || cinfraDesignateProject.trim()
                ? {
                    designate: {
                      ...(cinfraDesignateZone.trim() ? { zoneId: cinfraDesignateZone.trim() } : {}),
                      ...(cinfraDesignateProject.trim() ? { projectId: cinfraDesignateProject.trim() } : {}),
                    },
                  }
                : {}),
              ...(route53VaultPath.trim() ? { route53VaultPath: route53VaultPath.trim() } : {}),
            },
          };
        }
        if (cinfraType === 'openshift') {
          return {
            ...common,
            openshift: {
              clusterRef: cinfraClusterRef.trim() || DEFAULT_CLOUD_INFRA_OPENSHIFT.clusterRef,
              bootImage: cinfraBootImage.trim() || DEFAULT_CLOUD_INFRA_OPENSHIFT.bootImage,
              ...(cinfraStorageClass.trim() ? { storageClass: cinfraStorageClass.trim() } : {}),
              hostedClusterCidrDefaults: {
                clusterNetworkPool: {
                  cidr: cinfraPodPool.trim() || DEFAULT_CLOUD_INFRA_OPENSHIFT.clusterNetworkPool.cidr,
                  blockPrefixLength:
                    Number(cinfraPodBlock) || DEFAULT_CLOUD_INFRA_OPENSHIFT.clusterNetworkPool.blockPrefixLength,
                },
                serviceNetworkPool: {
                  cidr: cinfraSvcPool.trim() || DEFAULT_CLOUD_INFRA_OPENSHIFT.serviceNetworkPool.cidr,
                  blockPrefixLength:
                    Number(cinfraSvcBlock) || DEFAULT_CLOUD_INFRA_OPENSHIFT.serviceNetworkPool.blockPrefixLength,
                },
              },
            },
          };
        }
        return {
          ...common,
          aws: {
            accountId: cinfraAwsAccount.trim(),
            region: cinfraRegion.trim(),
            ...(cinfraBaseDomain.trim() ? { baseDomain: cinfraBaseDomain.trim() } : {}),
          },
        };
      }
      case 'platformopenshift': {
        const adminList = rbacMulti;
        const operatorList = rbacOperator;
        const viewerList = rbacViewer;
        const toolRbac =
          adminList.length || operatorList.length || viewerList.length
            ? {
                ...(adminList.length ? { clusterAdminRbac: adminList } : {}),
                ...(operatorList.length ? { clusterOperatorRbac: operatorList } : {}),
                ...(viewerList.length ? { clusterViewerRbac: viewerList } : {}),
              }
            : undefined;
        if (platformType === 'aws') {
          // AWS clusters take no address plan from a CloudInfrastructure — omit spec.networking.
          return {
            type: 'aws',
            aws: {
              environment: platformEnv || cloudAwsRef,
              region,
              clusterType: 'standalone',
              controlPlaneCount: Number(cpCount) || 3,
              workerCount: Number(workerCount) || 2,
            },
            ...(toolRbac ? { toolRbac } : {}),
          };
        }
        // Clusters do not join a fabric. Explicit CIDRs are used as given; omitted ones are
        // allocated from the CloudInfrastructure's hostedClusterCidrDefaults.
        const clusterNetwork = parseCidrList(poClusterNetwork);
        const serviceNetwork = parseCidrList(poServiceNetwork);
        const networkingSpec =
          clusterNetwork.length || serviceNetwork.length
            ? {
                ...(clusterNetwork.length ? { clusterNetwork } : {}),
                ...(serviceNetwork.length ? { serviceNetwork } : {}),
              }
            : undefined;
        if (platformType === 'hosted') {
          const envName = platformEnv || cloudVirtRef;
          return {
            type: 'hosted',
            hosted: {
              environment: envName,
              nodePoolReplicas: Number(nodePoolReplicas) || 2,
            },
            ...(networkingSpec ? { networking: networkingSpec } : {}),
            ...(toolRbac ? { toolRbac } : {}),
          };
        }
        return {
          type: 'openstack',
          openstack: {
            environment: platformEnv || cloudosoRef,
            controlPlaneCount: Number(cpCount) || 3,
            workerCount: Number(workerCount) || 3,
            ...(externalNetwork.trim() ? { externalNetwork: externalNetwork.trim() } : {}),
          },
          ...(networkingSpec ? { networking: networkingSpec } : {}),
          ...(toolRbac ? { toolRbac } : {}),
        };
      }
      case 'migration':
        return { source, vmName, cloudoso: cloudosoRef, providerNamespace: 'openshift-mtv' };
      case 'persona':
        // Persona CRD rbac is a single string — use first selected.
        return { rbac: personaRbacs[0] || rbacRef, type: personaType };
      case 'rbac':
        return { config: rbacConfig, description };
      case 'vault':
        return { ha: haEnabled, rbacConfig };
      case 'vaultkv': {
        const list = rbacMulti;
        return {
          vault: vaultRef,
          vaultAdminRbac: list,
          vaultReaderRbac: list,
          vaultOpsRbac: list,
          vaultDeveloperRbac: list,
        };
      }
      case 'aaporg': {
        const list = rbacMulti;
        return {
          aapConfig,
          aapAdminRbac: list,
          aapJobExecutorRbac: list,
          aapViewerRbac: list,
        };
      }
      case 'quayorg': {
        const list = rbacMulti;
        return {
          quayConfig,
          quayAdminRbac: list,
          quayCreatorRbac: list,
          quayMemberRbac: list,
        };
      }
      case 'hybridnetwork':
        return {
          description,
          ...(networkFabricRef ? { fabricRef: networkFabricRef } : {}),
          ...(overlayMtu.trim() ? { overlayMtu: Number(overlayMtu) } : {}),
          networkViewerRbac,
        };
      case 'networkplacement':
        return {
          network: networkRef,
          // BackendSelect — CloudOSO / CloudVirt project in this entity namespace.
          backend: { kind: backendKind, name: backendName },
          prefixes: prefixes.split(/[,\n]/).map((s) => s.trim()).filter(Boolean),
          ...(backendKind === 'CloudVirt' && vmNamespaceList.length ? { vmNamespaces: vmNamespaceList } : {}),
        };
      case 'hybridfabric': {
        const localnet = fabricUnderlayType === 'localnet';
        return {
          enabled: fabricEnabled,
          domainAsn: Number(domainAsn) || DEFAULT_FABRIC_DOMAIN_ASN,
          entityRefs: fabricEntityRefs.map((n) => ({ name: n })),
          vniPool: {
            start: Number(vniStart) || DEFAULT_FABRIC_VNI.start,
            end: Number(vniEnd) || DEFAULT_FABRIC_VNI.end,
          },
          transportDefaults: {
            mtu: Number(fabricMtu) || DEFAULT_FABRIC_TRANSPORT.mtu,
            defaultTunnelType: fabricTunnelType,
          },
          underlay: {
            type: fabricUnderlayType,
            nadName: fabricUnderlayNad || DEFAULT_FABRIC_UNDERLAY.nadName,
            cidr: fabricUnderlayCidr || DEFAULT_FABRIC_UNDERLAY.cidr,
            ...(localnet
              ? {
                  physicalNetworkName: fabricUnderlayPhysnet || DEFAULT_FABRIC_UNDERLAY.physicalNetworkName,
                  ...(fabricHubVtepBlock ? { hubVtepBlock: fabricHubVtepBlock } : {}),
                  ...(fabricHubLegAddress ? { hubLegAddress: fabricHubLegAddress } : {}),
                }
              : fabricUnderlayGateway
                ? { gatewayAddress: fabricUnderlayGateway }
                : {}),
            mtu: Number(fabricUnderlayMtu) || DEFAULT_FABRIC_UNDERLAY.mtu,
          },
          borderGateway: {
            name: fabricBgwName || DEFAULT_FABRIC_BORDER_GATEWAY.name,
            loopback: fabricBgwLoopback || DEFAULT_FABRIC_BORDER_GATEWAY.loopback,
            ...(fabricBgwVaultRef ? { vaultCredentialRef: fabricBgwVaultRef } : {}),
            wireguard: {
              address: fabricBgwWgAddress || DEFAULT_FABRIC_BORDER_GATEWAY.wireguardAddress,
              listenPort: Number(fabricBgwWgPort) || DEFAULT_FABRIC_BORDER_GATEWAY.wireguardListenPort,
            },
          },
        };
      }
      case 'cloudgateway': {
        const siteUnderlay = {
          ...(siteUnderlayInterface.trim() ? { interface: siteUnderlayInterface.trim() } : {}),
          ...(siteComputeInterface.trim() ? { computeInterface: siteComputeInterface.trim() } : {}),
          ...(siteUnderlayCidr.trim() ? { cidr: siteUnderlayCidr.trim() } : {}),
          ...(siteUnderlayGateway.trim() ? { gatewayAddress: siteUnderlayGateway.trim() } : {}),
        };
        return {
          enabled: true,
          fabricRef,
          // The CloudInfrastructure type selects the gateway flavour (openstack site VM / openshift hub landing).
          cloudRef: { kind: 'CloudInfrastructure', name: cloudRefName },
          ...(gatewayTransport ? { transport: { type: gatewayTransport } } : {}),
          ...(gatewayTransport === 'wireguard' && gatewayWgAddress.trim()
            ? { wireguard: { address: gatewayWgAddress.trim() } }
            : {}),
          ...(cloudRefType !== 'openshift' && Object.keys(siteUnderlay).length ? { siteUnderlay } : {}),
        };
      }
      case 'transportlink': {
        // Operator-generated in the new API; tunnel type follows the gateway's transport.
        const gwTransport = (
          gateways.items.find((g) => g.metadata.name === gatewayRef)?.spec as
            | { transport?: { type?: string } }
            | undefined
        )?.transport?.type;
        return {
          enabled: true,
          fabricRef,
          cloudGatewayRef: gatewayRef,
          ...(gwTransport === 'none' || gwTransport === 'wireguard' ? { tunnelType: gwTransport } : {}),
        };
      }
      case 'uihealthchecker':
        return {
          url: healthUrl,
          displayName: description || name,
          description,
          group: healthGroup,
          expectedStatus: 200,
          timeoutSeconds: 10,
        };
      default:
        return {};
    }
  };

  const canSubmit = (): boolean => {
    if (!name) return false;
    if (type === 'entity' && !billingID) return false;
    if (type === 'persona' && !entityNs) return false;
    if (type === 'assignment' && !teamRef) return false;
    if (type === 'cloudoso' && (!cloudRefName || !osoProject || !baseDomain)) return false;
    if (
      type === 'cloudaws' &&
      (!awsAccount ||
        !(awsBaseDomain || baseDomain) ||
        (!cloudRefName && !(awsAccessKeyId.trim() && awsSecretAccessKey.trim()) && !awsVaultPath))
    )
      return false;
    if (type === 'cloudvirt' && (!cloudRefName || !(virtBaseDomain || baseDomain))) return false;
    if (type === 'platformopenshift' && !(platformEnv || cloudosoRef || cloudAwsRef || cloudVirtRef)) return false;
    if (type === 'cloudinfrastructure') {
      if (cloudInfrastructureNeedsCredentials(cinfraType) && !cinfraCredsSet) return false;
      if (cinfraType === 'aws' && (!/^[0-9]{12}$/.test(cinfraAwsAccount.trim()) || !cinfraRegion.trim())) {
        return false;
      }
    }
    if (type === 'persona' && (!(personaRbacs[0] || rbacRef) || !personaType)) return false;
    if (type === 'vaultkv' && !vaultRef) return false;
    if (type === 'migration' && (!vmName || !cloudosoRef)) return false;
    if ((type === 'vault' || type === 'rbac') && !rbacConfig) return false;
    if (type === 'aaporg' && !aapConfig) return false;
    if (type === 'quayorg' && !quayConfig) return false;
    if (type === 'networkplacement' && (!networkRef || !backendKind || !backendName || !prefixes.trim())) return false;
    if (type === 'networkplacement' && backendKind === 'CloudVirt' && !vmNamespacesValid) return false;
    if (type === 'hybridnetwork' && !overlayMtuValid) return false;
    if (
      type === 'hybridnetwork' &&
      fabricOptionsForEntity.filter((o) => !o.isDisabled).length > 1 &&
      !networkFabricRef
    ) {
      return false;
    }
    if (
      type === 'hybridfabric' &&
      (!domainAsn || fabricEntityRefs.length === 0 || !fabricUnderlayCidr || !fabricBgwLoopback)
    )
      return false;
    if (type === 'cloudgateway' && (!fabricRef || !cloudRefName)) return false;
    if (type === 'transportlink' && (!fabricRef || !gatewayRef)) return false;
    if (type === 'uihealthchecker' && !healthUrl.startsWith('http')) return false;
    return true;
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setSubmitting(true);
    setResult(null);
    try {
      const targetNs =
        type === 'hybridfabric' ||
        type === 'cloudinfrastructure' ||
        type === 'cloudgateway' ||
        type === 'transportlink' ||
        type === 'uihealthchecker'
          ? 'sovereign-cloud'
          : entityNs;
      if (!targetNs) {
        throw new Error('Namespace is required');
      }
      if (type === 'cloudaws' && !cloudRefName && awsAccessKeyId.trim() && awsSecretAccessKey.trim()) {
        await createNamespaceSecret(targetNs, {
          name: `${name}-aws-credentials`,
          labels: {
            'hybridsovereign.redhat/cloudaws': name,
            'hybridsovereign.redhat/credential-type': 'aws',
          },
          stringData: {
            AWS_ACCESS_KEY_ID: awsAccessKeyId.trim(),
            AWS_SECRET_ACCESS_KEY: awsSecretAccessKey.trim(),
            ACCOUNT_ID: awsAccount.trim(),
          },
        });
      }
      await createDashboardResource(kind, { name, namespace: targetNs, spec: buildSpec() }, targetNs);
      setResult({ ok: true, message: `${kind} "${name}" submitted.` });
      setTimeout(() => onSuccess(listPath), 1200);
    } catch (err) {
      setResult({
        ok: false,
        message: err instanceof Error ? err.message : 'Submit failed',
      });
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <>
      <PageHeader
        title={title}
        subtitle={`Namespace ${entityNs || 'cluster'}`}
        breadcrumbs={[
          { label: 'Sovereign Cloud' },
          { label: title.replace(/^Create |^Request |^Migrate to /, ''), to: listPath },
          { label: 'Create' },
        ]}
      />
      {result && (
        <Alert
          variant={result.ok ? 'success' : 'danger'}
          title={result.ok ? 'Submitted' : 'Submission failed'}
          isInline
          className="sc-mb"
        >
          {result.message}
        </Alert>
      )}
      <div className="sc-form-layout">
        <Card isCompact>
          <CardBody>
            <Form onSubmit={handleSubmit}>
              <FormGroup label="Name" isRequired fieldId="name">
                <TextInput id="name" value={name} onChange={(_e, v) => setName(v)} isRequired />
              </FormGroup>

              {type === 'entity' && (
                <>
                  <FormGroup label="Billing ID" isRequired fieldId="billing">
                    <TextInput id="billing" value={billingID} onChange={(_e, v) => setBillingID(v)} isRequired />
                  </FormGroup>
                  <FormGroup label="Description" fieldId="description">
                    <TextArea id="description" value={description} onChange={(_e, v) => setDescription(v)} />
                  </FormGroup>
                  <FormGroup label="Website" fieldId="website">
                    <TextInput id="website" value={websiteLink} onChange={(_e, v) => setWebsiteLink(v)} />
                  </FormGroup>
                </>
              )}

              {(type === 'project' || type === 'rbac') && (
                <FormGroup label="Description" fieldId="description">
                  <TextArea id="description" value={description} onChange={(_e, v) => setDescription(v)} />
                </FormGroup>
              )}

              {type === 'team' && (
                <>
                  <FormGroup label="Enable Argo CD" fieldId="argo">
                    <Switch
                      id="argo"
                      isChecked={argoEnabled}
                      onChange={(_e, checked) => setArgoEnabled(checked)}
                    />
                  </FormGroup>
                  <FormGroup label="Enable Istio" fieldId="istio">
                    <Switch
                      id="istio"
                      isChecked={istioEnabled}
                      onChange={(_e, checked) => setIstioEnabled(checked)}
                    />
                  </FormGroup>
                </>
              )}

              {type === 'project' && (
                <FormGroup label="Display name" fieldId="display-name">
                  <TextInput id="display-name" value={displayName} onChange={(_e, v) => setDisplayName(v)} />
                </FormGroup>
              )}

              {type === 'assignment' && (
                <>
                  <RefSelect id="team" label="Team" value={teamRef} onChange={setTeamRef} options={names(teams.items)} isRequired />
                  <FormGroup label="Projects (comma-separated)" fieldId="projects">
                    <TextArea
                      id="projects"
                      value={projectRefs || projectRef}
                      onChange={(_e, v) => { setProjectRefs(v); setProjectRef(''); }}
                      rows={2}
                      placeholder={names(projects.items).map((p) => p.value).join(', ') || 'project-a, project-b'}
                    />
                  </FormGroup>
                  <RefSelect
                    id="platform"
                    label="Platform Openshift"
                    value={platformRef}
                    onChange={setPlatformRef}
                    options={names(platforms.items)}
                    placeholder="Optional"
                  />
                  <RbacMultiSelect
                    id="assign-admin"
                    label="Assignment admin RBAC"
                    value={assignAdmin}
                    onChange={setAssignAdmin}
                    options={names(rbacs.items)}
                    placeholder="Optional"
                  />
                  <RbacMultiSelect
                    id="assign-dev"
                    label="Assignment developer RBAC"
                    value={assignDev}
                    onChange={setAssignDev}
                    options={names(rbacs.items)}
                    placeholder="Optional"
                  />
                  <RbacMultiSelect
                    id="assign-viewer"
                    label="Assignment viewer RBAC"
                    value={assignViewer}
                    onChange={setAssignViewer}
                    options={names(rbacs.items)}
                    placeholder="Optional"
                  />
                  <RbacMultiSelect
                    id="assign-ops"
                    label="Assignment ops RBAC"
                    value={assignOps}
                    onChange={setAssignOps}
                    options={names(rbacs.items)}
                    placeholder="Optional"
                  />
                </>
              )}

              {type === 'cloudoso' && (
                <>
                  <CloudInfrastructureSelect
                    id="oso-cloudref"
                    label={t('fields.cloudRef')}
                    value={cloudRefName}
                    onChange={setCloudRefName}
                    onSelectInfrastructure={onCloudInfrastructurePicked}
                    types={['openstack']}
                    entityName={currentEntityName}
                    isRequired
                  />
                  <FormGroup label="OpenStack project" fieldId="oso-project" isRequired>
                    <TextInput id="oso-project" value={osoProject} onChange={(_e, v) => setOsoProject(v)} isRequired />
                  </FormGroup>
                  <FormGroup label="Base domain" fieldId="oso-base" isRequired>
                    <TextInput id="oso-base" value={baseDomain} onChange={(_e, v) => setBaseDomain(v)} isRequired />
                  </FormGroup>
                  <FormGroup label={t('fields.projectDomain')} fieldId="oso-pdom">
                    <TextInput id="oso-pdom" value={projectDomain} onChange={(_e, v) => setProjectDomain(v)} />
                    <p className="sc-text-muted" style={{ marginTop: '0.25rem' }}>
                      {t('fields.inheritsFromCloudInfra')}
                    </p>
                  </FormGroup>
                  <FormGroup label={t('fields.externalNetwork')} fieldId="oso-ext">
                    <TextInput id="oso-ext" value={externalNetwork} onChange={(_e, v) => setExternalNetwork(v)} />
                    <p className="sc-text-muted" style={{ marginTop: '0.25rem' }}>
                      {t('fields.inheritsFromCloudInfra')}
                    </p>
                  </FormGroup>
                  <FormGroup label="Route53 vault path (optional)" fieldId="oso-r53">
                    <TextInput id="oso-r53" value={route53VaultPath} onChange={(_e, v) => setRoute53VaultPath(v)} />
                  </FormGroup>
                  <FormGroup label="Landing zone" fieldId="oso-lz">
                    <TextInput id="oso-lz" value={landingzone} onChange={(_e, v) => setLandingzone(v)} />
                  </FormGroup>
                </>
              )}

              {type === 'cloudaws' && (
                <>
                  <CloudInfrastructureSelect
                    id="aws-cloudref"
                    label={t('fields.cloudRef')}
                    value={cloudRefName}
                    onChange={setCloudRefName}
                    onSelectInfrastructure={onCloudInfrastructurePicked}
                    types={['aws']}
                    entityName={currentEntityName}
                    placeholder={t('form.cloudInfraOptional')}
                  />
                  <FormGroup label="AWS account ID" fieldId="aws-acct" isRequired>
                    <TextInput id="aws-acct" value={awsAccount} onChange={(_e, v) => setAwsAccount(v)} isRequired />
                  </FormGroup>
                  {!cloudRefName && (
                    <>
                      <FormGroup label="AWS access key ID" fieldId="aws-ak" isRequired>
                        <TextInput
                          id="aws-ak"
                          value={awsAccessKeyId}
                          onChange={(_e, v) => setAwsAccessKeyId(v)}
                          autoComplete="off"
                          isRequired
                        />
                      </FormGroup>
                      <FormGroup label="AWS secret access key" fieldId="aws-sk" isRequired>
                        <TextInput
                          id="aws-sk"
                          type="password"
                          value={awsSecretAccessKey}
                          onChange={(_e, v) => setAwsSecretAccessKey(v)}
                          autoComplete="new-password"
                          isRequired
                        />
                      </FormGroup>
                    </>
                  )}
                  <FormGroup label="Base domain" fieldId="aws-base" isRequired>
                    <TextInput id="aws-base" value={awsBaseDomain} onChange={(_e, v) => setAwsBaseDomain(v)} isRequired />
                  </FormGroup>
                  {!cloudRefName && (
                    <FormGroup label="Vault path (optional fallback)" fieldId="aws-vault">
                      <TextInput id="aws-vault" value={awsVaultPath} onChange={(_e, v) => setAwsVaultPath(v)} />
                    </FormGroup>
                  )}
                  <FormGroup label="Landing zone" fieldId="aws-lz">
                    <TextInput id="aws-lz" value={landingzone} onChange={(_e, v) => setLandingzone(v)} />
                  </FormGroup>
                </>
              )}

              {type === 'cloudvirt' && (
                <>
                  <CloudInfrastructureSelect
                    id="virt-cloudref"
                    label={t('fields.cloudRef')}
                    value={cloudRefName}
                    onChange={setCloudRefName}
                    onSelectInfrastructure={onCloudInfrastructurePicked}
                    types={['openshift']}
                    entityName={currentEntityName}
                    isRequired
                  />
                  <FormGroup label="Base domain" fieldId="virt-base" isRequired>
                    <TextInput id="virt-base" value={virtBaseDomain} onChange={(_e, v) => setVirtBaseDomain(v)} isRequired />
                  </FormGroup>
                  <FormGroup label="Storage class" fieldId="virt-sc">
                    <TextInput id="virt-sc" value={virtStorageClass} onChange={(_e, v) => setVirtStorageClass(v)} />
                  </FormGroup>
                </>
              )}

              {type === 'platformopenshift' && (
                <>
                  <RefSelect
                    id="platform-type"
                    label="Platform type"
                    value={platformType}
                    onChange={(v) => {
                      setPlatformType(v);
                      setPlatformEnv('');
                      if (v === 'hosted') {
                        setNodePoolReplicas('2');
                      } else {
                        setCpCount('3');
                        setWorkerCount(v === 'aws' ? '2' : '3');
                      }
                    }}
                    options={[
                      { value: 'openstack', label: 'OpenStack' },
                      { value: 'aws', label: 'AWS' },
                      { value: 'hosted', label: 'Hosted — Hypershift HCP (containerized control plane)' },
                    ]}
                    isRequired
                  />
                  <RefSelect
                    id="platform-env"
                    label={
                      platformType === 'aws'
                        ? 'CloudAWS environment'
                        : platformType === 'hosted'
                          ? 'CloudVirt environment'
                          : 'CloudOSO environment'
                    }
                    value={
                      platformEnv ||
                      (platformType === 'aws'
                        ? cloudAwsRef
                        : platformType === 'hosted'
                          ? cloudVirtRef
                          : cloudosoRef)
                    }
                    onChange={(v) => {
                      setPlatformEnv(v);
                      if (platformType === 'aws') setCloudAwsRef(v);
                      else if (platformType === 'hosted') setCloudVirtRef(v);
                      else setCloudosoRef(v);
                    }}
                    options={names(
                      platformType === 'aws'
                        ? cloudAwss.items
                        : platformType === 'hosted'
                          ? cloudVirts.items
                          : cloudosos.items,
                    )}
                    isRequired
                  />
                  {platformType === 'hosted' ? (
                    <FormGroup label="Worker NodePool replicas" fieldId="nodepool-replicas">
                      <TextInput
                        id="nodepool-replicas"
                        type="number"
                        value={nodePoolReplicas}
                        onChange={(_e, v) => setNodePoolReplicas(v)}
                      />
                    </FormGroup>
                  ) : (
                    <>
                      <FormGroup label="Control plane count" fieldId="cp-count">
                        <TextInput id="cp-count" type="number" value={cpCount} onChange={(_e, v) => setCpCount(v)} />
                      </FormGroup>
                      <FormGroup label="Worker count" fieldId="worker-count">
                        <TextInput
                          id="worker-count"
                          type="number"
                          value={workerCount}
                          onChange={(_e, v) => setWorkerCount(v)}
                        />
                      </FormGroup>
                    </>
                  )}
                  {platformType === 'aws' && (
                    <FormGroup label="Region" fieldId="plat-region">
                      <TextInput id="plat-region" value={region} onChange={(_e, v) => setRegion(v)} />
                    </FormGroup>
                  )}
                  {platformType === 'openstack' && (
                    <FormGroup label="External network" fieldId="plat-ext">
                      <TextInput id="plat-ext" value={externalNetwork} onChange={(_e, v) => setExternalNetwork(v)} />
                    </FormGroup>
                  )}

                  {platformType !== 'aws' && (
                    <>
                      <FormGroup label={t('fields.clusterNetwork')} fieldId="po-cluster-net">
                        <TextInput
                          id="po-cluster-net"
                          value={poClusterNetwork}
                          onChange={(_e, v) => setPoClusterNetwork(v)}
                          placeholder={t('form.allocateAutomatically')}
                        />
                      </FormGroup>
                      <FormGroup label={t('fields.serviceNetwork')} fieldId="po-service-net">
                        <TextInput
                          id="po-service-net"
                          value={poServiceNetwork}
                          onChange={(_e, v) => setPoServiceNetwork(v)}
                          placeholder={t('form.allocateAutomatically')}
                        />
                        <p className="sc-text-muted" style={{ marginTop: '0.25rem' }}>
                          {t('fields.clusterCidrsHelp')}
                        </p>
                      </FormGroup>
                    </>
                  )}
                  <Alert variant="info" isInline title={t('form.noFabricJoinTitle')} className="sc-mb">
                    {t('form.noFabricJoinBody')}
                  </Alert>

                  <RbacMultiSelect
                    id="plat-rbac-admin"
                    label="Cluster admin RBAC"
                    value={rbacMulti}
                    onChange={setRbacMulti}
                    options={names(rbacs.items)}
                    placeholder="Select Rbac CRs…"
                  />
                  <RbacMultiSelect
                    id="plat-rbac-op"
                    label="Cluster operator RBAC"
                    value={rbacOperator}
                    onChange={setRbacOperator}
                    options={names(rbacs.items)}
                    placeholder="Select Rbac CRs…"
                  />
                  <RbacMultiSelect
                    id="plat-rbac-view"
                    label="Cluster viewer RBAC"
                    value={rbacViewer}
                    onChange={setRbacViewer}
                    options={names(rbacs.items)}
                    placeholder="Select Rbac CRs…"
                  />
                </>
              )}

              {type === 'persona' && (
                <>
                  {!namespace && (
                    <RefSelect
                      id="entity"
                      label="Entity"
                      value={entityName}
                      onChange={(v) => {
                        setEntityName(v);
                        setRbacRef('');
                        setPersonaRbacs([]);
                      }}
                      options={names(entities.items)}
                      isRequired
                    />
                  )}
                  <RbacMultiSelect
                    id="rbac"
                    label="RBAC"
                    value={personaRbacs}
                    onChange={(next) => {
                      setPersonaRbacs(next);
                      setRbacRef(next[0] ?? '');
                    }}
                    options={names(rbacs.items)}
                    isRequired
                  />
                  <RefSelect
                    id="persona-type"
                    label="Type"
                    value={personaType}
                    onChange={setPersonaType}
                    options={PERSONA_TYPES.map((t) => ({ value: t, label: t }))}
                    isRequired
                  />
                </>
              )}

              {type === 'rbac' && (
                <RefSelect
                  id="rbac-config"
                  label="Config"
                  value={rbacConfig}
                  onChange={setRbacConfig}
                  options={names(rbacConfigs.items)}
                  isRequired
                />
              )}

              {type === 'vault' && (
                <>
                  <RefSelect
                    id="vault-rbac-config"
                    label="RBAC Config"
                    value={rbacConfig}
                    onChange={setRbacConfig}
                    options={names(rbacConfigs.items)}
                    isRequired
                  />
                  <FormGroup label="High availability" fieldId="vault-ha">
                    <Switch id="vault-ha" isChecked={haEnabled} onChange={(_e, c) => setHaEnabled(c)} />
                  </FormGroup>
                </>
              )}

              {type === 'vaultkv' && (
                <>
                  <RefSelect id="vault-ref" label="Vault" value={vaultRef} onChange={setVaultRef} options={names(vaults.items)} isRequired />
                  <RbacMultiSelect
                    id="vaultkv-rbac"
                    label="RBAC groups"
                    value={rbacMulti}
                    onChange={setRbacMulti}
                    options={names(rbacs.items)}
                    placeholder="Select Rbac CRs…"
                  />
                </>
              )}

              {type === 'aaporg' && (
                <>
                  <RefSelect
                    id="aap-config"
                    label="AAP Config"
                    value={aapConfig}
                    onChange={setAapConfig}
                    options={names(aapConfigs.items)}
                    isRequired
                  />
                  <RbacMultiSelect
                    id="aap-rbac"
                    label="RBAC groups"
                    value={rbacMulti}
                    onChange={setRbacMulti}
                    options={names(rbacs.items)}
                    placeholder="Select Rbac CRs…"
                  />
                </>
              )}

              {type === 'quayorg' && (
                <>
                  <RefSelect
                    id="quay-config"
                    label="Quay Config"
                    value={quayConfig}
                    onChange={setQuayConfig}
                    options={names(quayConfigs.items)}
                    isRequired
                  />
                  <RbacMultiSelect
                    id="quay-rbac"
                    label="RBAC groups"
                    value={rbacMulti}
                    onChange={setRbacMulti}
                    options={names(rbacs.items)}
                    placeholder="Select Rbac CRs…"
                  />
                </>
              )}

              {type === 'migration' && (
                <>
                  <RefSelect
                    id="source"
                    label="Source"
                    value={source}
                    onChange={setSource}
                    options={[
                      { value: 'vmware', label: 'VMware' },
                      { value: 'ovirt', label: 'oVirt' },
                      { value: 'openstack', label: 'OpenStack' },
                    ]}
                    isRequired
                  />
                  <FormGroup label="VM Name" isRequired fieldId="vm">
                    <TextInput id="vm" value={vmName} onChange={(_e, v) => setVmName(v)} isRequired />
                  </FormGroup>
                  <RefSelect
                    id="cloudoso"
                    label="CloudOSO"
                    value={cloudosoRef}
                    onChange={setCloudosoRef}
                    options={names(cloudosos.items)}
                    isRequired
                  />
                </>
              )}


              {type === 'hybridnetwork' && (
                <>
                  <FormGroup label="Description" fieldId="hn-desc">
                    <TextArea id="hn-desc" value={description} onChange={(_e, v) => setDescription(v)} rows={3} />
                  </FormGroup>
                  <FabricSelect
                    id="hn-fabric"
                    label="Fabric"
                    value={networkFabricRef}
                    onChange={setNetworkFabricRef}
                    options={fabricOptionsForEntity}
                    placeholder={
                      fabricOptionsForEntity.filter((o) => !o.isDisabled).length > 1
                        ? 'Required — multiple fabrics tag this entity'
                        : 'Auto (single fabric) or pick explicitly'
                    }
                    isRequired={fabricOptionsForEntity.filter((o) => !o.isDisabled).length > 1}
                  />
                  <FormGroup label={t('fields.overlayMtu')} fieldId="hn-mtu">
                    <TextInput
                      id="hn-mtu"
                      type="number"
                      value={overlayMtu}
                      onChange={(_e, v) => setOverlayMtu(v)}
                      validated={overlayMtuValid ? 'default' : 'error'}
                    />
                    <p className="sc-text-muted" style={{ marginTop: '0.25rem' }}>
                      {t('fields.overlayMtuHelp')}
                    </p>
                  </FormGroup>
                  <RbacMultiSelect
                    id="hn-viewers"
                    label="Network viewer RBAC"
                    value={networkViewerRbac}
                    onChange={setNetworkViewerRbac}
                    options={names(rbacs.items)}
                    placeholder="Select Rbac CRs…"
                  />
                </>
              )}

              {type === 'networkplacement' && (
                <>
                  <RefSelect id="np-network" label="Hybrid Network" value={networkRef} onChange={setNetworkRef} options={names(hybridNetworks.items)} isRequired />
                  <BackendSelect
                    id="np-backend"
                    label="Backend"
                    value={backendName ? { kind: backendKind, name: backendName } : null}
                    onChange={(b: BackendSelectValue) => {
                      setBackendKind(b.kind);
                      setBackendName(b.name);
                    }}
                    options={backendOptions}
                    isRequired
                  />
                  <FormGroup label="Prefixes (CIDR, comma-separated)" fieldId="np-prefixes" isRequired>
                    <TextArea id="np-prefixes" value={prefixes} onChange={(_e, v) => setPrefixes(v)} rows={2} />
                  </FormGroup>
                  {backendKind === 'CloudVirt' && (
                    <FormGroup label={t('fields.vmNamespaces')} fieldId="np-vm-ns">
                      <TextArea
                        id="np-vm-ns"
                        value={vmNamespaces}
                        onChange={(_e, v) => setVmNamespaces(v)}
                        rows={2}
                        placeholder={backendName && networkRef ? `${backendName}-${networkRef}` : undefined}
                        validated={vmNamespacesValid ? 'default' : 'error'}
                      />
                      <p className="sc-text-muted" style={{ marginTop: '0.25rem' }}>
                        {t('fields.vmNamespacesHelp')}
                      </p>
                    </FormGroup>
                  )}
                </>
              )}

              {type === 'hybridfabric' && (
                <>
                  <FormGroup label="Enabled" fieldId="hf-enabled">
                    <Switch
                      id="hf-enabled"
                      isChecked={fabricEnabled}
                      onChange={(_e, v) => setFabricEnabled(v)}
                      label={fabricEnabled ? 'Enabled' : 'Disabled'}
                    />
                  </FormGroup>
                  <FormGroup label="Domain ASN" fieldId="hf-asn" isRequired>
                    <TextInput id="hf-asn" value={domainAsn} onChange={(_e, v) => setDomainAsn(v)} />
                    <p className="sc-text-muted" style={{ marginTop: '0.25rem' }}>
                      {t('fields.domainAsnHelp')}
                    </p>
                  </FormGroup>
                  <FormGroup label="VNI pool start" fieldId="hf-vni-start">
                    <TextInput id="hf-vni-start" value={vniStart} onChange={(_e, v) => setVniStart(v)} />
                  </FormGroup>
                  <FormGroup label="VNI pool end" fieldId="hf-vni-end">
                    <TextInput id="hf-vni-end" value={vniEnd} onChange={(_e, v) => setVniEnd(v)} />
                  </FormGroup>
                  <EntityMultiSelect
                    id="hf-entities"
                    label="Entities that may use this fabric"
                    value={fabricEntityRefs}
                    onChange={setFabricEntityRefs}
                    options={namesWithReady(entities.items)}
                    isRequired
                  />
                  <ExpandableSection
                    toggleText={fabricAdvancedOpen ? 'Hide advanced fabric options' : 'Show advanced fabric options'}
                    onToggle={(_e, isOpen) => setFabricAdvancedOpen(isOpen)}
                    isExpanded={fabricAdvancedOpen}
                  >
                    <FormGroup label="Default tunnel type" fieldId="hf-tunnel">
                      <FormSelect
                        id="hf-tunnel"
                        value={fabricTunnelType}
                        onChange={(_e, v) => setFabricTunnelType(v as FabricTunnelType)}
                        aria-label="Default tunnel type"
                      >
                        {FABRIC_TUNNEL_TYPES.map((tt) => (
                          <FormSelectOption
                            key={tt}
                            value={tt}
                            label={tt === 'none' ? t('fields.tunnelNone') : t('fields.tunnelWireguard')}
                          />
                        ))}
                      </FormSelect>
                      <p className="sc-text-muted" style={{ marginTop: '0.25rem' }}>
                        {t('fields.defaultTunnelTypeHelp')}
                      </p>
                    </FormGroup>
                    <FormGroup label="MTU" fieldId="hf-mtu">
                      <TextInput id="hf-mtu" value={fabricMtu} onChange={(_e, v) => setFabricMtu(v)} />
                    </FormGroup>
                    <FormGroup label={t('fields.underlayType')} fieldId="hf-ul-type">
                      <FormSelect
                        id="hf-ul-type"
                        value={fabricUnderlayType}
                        onChange={(_e, v) => setFabricUnderlayType(v as FabricUnderlayType)}
                        aria-label={t('fields.underlayType')}
                      >
                        <FormSelectOption value="localnet" label={t('fields.underlayLocalnet')} />
                        <FormSelectOption value="ovn-layer2" label={t('fields.underlayLayer2')} />
                      </FormSelect>
                      <p className="sc-text-muted" style={{ marginTop: '0.25rem' }}>
                        {t('fields.underlayHelp')}
                      </p>
                    </FormGroup>
                    {fabricUnderlayType === 'localnet' && (
                      <FormGroup label={t('fields.underlayPhysicalNetwork')} fieldId="hf-ul-physnet">
                        <TextInput
                          id="hf-ul-physnet"
                          value={fabricUnderlayPhysnet}
                          onChange={(_e, v) => setFabricUnderlayPhysnet(v)}
                        />
                      </FormGroup>
                    )}
                    <FormGroup label="Underlay network name" fieldId="hf-ul-nad">
                      <TextInput
                        id="hf-ul-nad"
                        value={fabricUnderlayNad}
                        onChange={(_e, v) => setFabricUnderlayNad(v)}
                      />
                    </FormGroup>
                    <FormGroup label="Underlay CIDR" fieldId="hf-ul-cidr" isRequired>
                      <TextInput
                        id="hf-ul-cidr"
                        value={fabricUnderlayCidr}
                        onChange={(_e, v) => setFabricUnderlayCidr(v)}
                      />
                      <p className="sc-text-muted" style={{ marginTop: '0.25rem' }}>
                        {t('form.immutableAfterCreate')}
                      </p>
                    </FormGroup>
                    {fabricUnderlayType === 'localnet' ? (
                      <>
                        <FormGroup label={t('fields.underlayHubVtepBlock')} fieldId="hf-ul-vtep">
                          <TextInput
                            id="hf-ul-vtep"
                            value={fabricHubVtepBlock}
                            onChange={(_e, v) => setFabricHubVtepBlock(v)}
                          />
                          <p className="sc-text-muted" style={{ marginTop: '0.25rem' }}>
                            {t('fields.underlayHubVtepBlockHelp')}
                          </p>
                        </FormGroup>
                        <FormGroup label={t('fields.underlayHubLegAddress')} fieldId="hf-ul-leg">
                          <TextInput
                            id="hf-ul-leg"
                            value={fabricHubLegAddress}
                            onChange={(_e, v) => setFabricHubLegAddress(v)}
                          />
                        </FormGroup>
                      </>
                    ) : (
                      <FormGroup label="Underlay gateway address" fieldId="hf-ul-gw">
                        <TextInput
                          id="hf-ul-gw"
                          value={fabricUnderlayGateway}
                          onChange={(_e, v) => setFabricUnderlayGateway(v)}
                          placeholder={t('form.firstHostOfCidr')}
                        />
                      </FormGroup>
                    )}
                    <FormGroup label="Underlay MTU" fieldId="hf-ul-mtu">
                      <TextInput
                        id="hf-ul-mtu"
                        value={fabricUnderlayMtu}
                        onChange={(_e, v) => setFabricUnderlayMtu(v)}
                      />
                    </FormGroup>
                    <FormGroup label={t('fields.borderGatewayName')} fieldId="hf-bgw-name">
                      <TextInput
                        id="hf-bgw-name"
                        value={fabricBgwName}
                        onChange={(_e, v) => setFabricBgwName(v)}
                      />
                    </FormGroup>
                    <FormGroup label={t('fields.borderGatewayLoopback')} fieldId="hf-bgw-lo" isRequired>
                      <TextInput
                        id="hf-bgw-lo"
                        value={fabricBgwLoopback}
                        onChange={(_e, v) => setFabricBgwLoopback(v)}
                      />
                      <p className="sc-text-muted" style={{ marginTop: '0.25rem' }}>
                        {t('form.immutableAfterCreate')}
                      </p>
                    </FormGroup>
                    <FormGroup label={t('fields.bgwWireguardAddress')} fieldId="hf-bgw-wg">
                      <TextInput
                        id="hf-bgw-wg"
                        value={fabricBgwWgAddress}
                        onChange={(_e, v) => setFabricBgwWgAddress(v)}
                      />
                    </FormGroup>
                    <FormGroup label={t('fields.bgwWireguardPort')} fieldId="hf-bgw-wg-port">
                      <TextInput
                        id="hf-bgw-wg-port"
                        type="number"
                        value={fabricBgwWgPort}
                        onChange={(_e, v) => setFabricBgwWgPort(v)}
                      />
                    </FormGroup>
                    <FormGroup label="Border gateway Vault credential ref (optional)" fieldId="hf-bgw-vault">
                      <TextInput
                        id="hf-bgw-vault"
                        value={fabricBgwVaultRef}
                        onChange={(_e, v) => setFabricBgwVaultRef(v)}
                        placeholder="fabric/<fabric>/bgw"
                      />
                      <p className="sc-text-muted" style={{ marginTop: '0.25rem' }}>
                        {t('form.bgwVaultRefHelp')}
                      </p>
                    </FormGroup>
                  </ExpandableSection>
                </>
              )}

              {type === 'cloudinfrastructure' && (
                <>
                  <RefSelect
                    id="ci-type"
                    label={t('fields.cloudInfraType')}
                    value={cinfraType}
                    onChange={(v) => {
                      const next = v as CloudInfrastructureType;
                      setCinfraType(next);
                      setCinfraRegion(next === 'openstack' ? DEFAULT_CLOUD_INFRA_OPENSTACK.region : next === 'aws' ? 'us-east-1' : '');
                    }}
                    options={CLOUD_INFRASTRUCTURE_TYPES.map((ct) => ({
                      value: ct,
                      label:
                        ct === 'openstack'
                          ? t('fields.cloudTypeOpenstack')
                          : ct === 'openshift'
                            ? t('fields.cloudTypeOpenshift')
                            : t('fields.cloudTypeAws'),
                    }))}
                    isRequired
                  />
                  <p className="sc-text-muted" style={{ marginTop: '-0.75rem' }}>
                    {t('form.immutableAfterCreate')}
                  </p>
                  <FormGroup label={t('fields.displayName')} fieldId="ci-display">
                    <TextInput id="ci-display" value={displayName} onChange={(_e, v) => setDisplayName(v)} />
                  </FormGroup>
                  {cloudInfrastructureNeedsCredentials(cinfraType) && (
                    <>
                      <RefSelect
                        id="ci-cred-mode"
                        label={t('fields.credentialsSource')}
                        value={cinfraCredMode}
                        onChange={(v) => setCinfraCredMode(v === 'secret' ? 'secret' : 'vault')}
                        options={[
                          { value: 'vault', label: t('fields.credentialsVaultPath') },
                          { value: 'secret', label: t('fields.credentialsSecretName') },
                        ]}
                        isRequired
                      />
                      {cinfraCredMode === 'vault' ? (
                        <FormGroup label={t('fields.credentialsVaultPath')} fieldId="ci-cred-vault" isRequired>
                          <TextInput
                            id="ci-cred-vault"
                            value={cinfraCredVaultPath}
                            onChange={(_e, v) => setCinfraCredVaultPath(v)}
                            placeholder={cinfraType === 'openstack' ? 'oso/accounts/<site>-admin' : 'aws/accounts/<account>'}
                            isRequired
                          />
                        </FormGroup>
                      ) : (
                        <FormGroup label={t('fields.credentialsSecretName')} fieldId="ci-cred-secret" isRequired>
                          <TextInput
                            id="ci-cred-secret"
                            value={cinfraCredSecret}
                            onChange={(_e, v) => setCinfraCredSecret(v)}
                            isRequired
                          />
                        </FormGroup>
                      )}
                      <p className="sc-text-muted" style={{ marginTop: '-0.75rem' }}>
                        {t('fields.credentialsRefHelp')}
                      </p>
                    </>
                  )}
                  <EntityMultiSelect
                    id="ci-entities"
                    label={t('fields.entityRefs')}
                    value={cinfraEntityRefs}
                    onChange={setCinfraEntityRefs}
                    options={namesWithReady(entities.items)}
                  />
                  <p className="sc-text-muted" style={{ marginTop: '-0.75rem' }}>
                    {t('fields.cloudInfraEntityRefsHelp')}
                  </p>
                  {cinfraType === 'openstack' && (
                    <>
                      <FormGroup label={t('fields.region')} fieldId="ci-os-region">
                        <TextInput id="ci-os-region" value={cinfraRegion} onChange={(_e, v) => setCinfraRegion(v)} />
                      </FormGroup>
                      <FormGroup label={t('fields.managementClusterKubeconfigRef')} fieldId="ci-os-kubeconfig">
                        <TextInput
                          id="ci-os-kubeconfig"
                          value={cinfraMgmtKubeconfig}
                          onChange={(_e, v) => setCinfraMgmtKubeconfig(v)}
                          placeholder={name ? `oso/${name}/mgmt-kubeconfig` : 'oso/<name>/mgmt-kubeconfig'}
                        />
                        <p className="sc-text-muted" style={{ marginTop: '0.25rem' }}>
                          {t('fields.managementClusterKubeconfigRefHelp')}
                        </p>
                      </FormGroup>
                      <FormGroup label={t('fields.netConfigRef')} fieldId="ci-os-netconfig">
                        <TextInput id="ci-os-netconfig" value={cinfraNetConfig} onChange={(_e, v) => setCinfraNetConfig(v)} />
                      </FormGroup>
                      <FormGroup label={t('fields.dataplaneNodeSetRefs')} fieldId="ci-os-nodesets">
                        <TextArea
                          id="ci-os-nodesets"
                          value={cinfraNodeSets}
                          onChange={(_e, v) => setCinfraNodeSets(v)}
                          rows={2}
                          placeholder="openstack-compute01"
                        />
                        <p className="sc-text-muted" style={{ marginTop: '0.25rem' }}>
                          {t('fields.dataplaneNodeSetRefsHelp')}
                        </p>
                      </FormGroup>
                      <FormGroup label={t('fields.externalNetwork')} fieldId="ci-os-ext">
                        <TextInput
                          id="ci-os-ext"
                          value={cinfraExternalNetwork}
                          onChange={(_e, v) => setCinfraExternalNetwork(v)}
                        />
                      </FormGroup>
                      <FormGroup label={t('fields.baseDomain')} fieldId="ci-os-base">
                        <TextInput id="ci-os-base" value={cinfraBaseDomain} onChange={(_e, v) => setCinfraBaseDomain(v)} />
                      </FormGroup>
                      <FormGroup label={t('fields.projectDomain')} fieldId="ci-os-pdom">
                        <TextInput
                          id="ci-os-pdom"
                          value={cinfraProjectDomain}
                          onChange={(_e, v) => setCinfraProjectDomain(v)}
                        />
                      </FormGroup>
                      <FormGroup label={t('fields.designateZoneId')} fieldId="ci-os-dzone">
                        <TextInput
                          id="ci-os-dzone"
                          value={cinfraDesignateZone}
                          onChange={(_e, v) => setCinfraDesignateZone(v)}
                        />
                      </FormGroup>
                      <FormGroup label={t('fields.designateProjectId')} fieldId="ci-os-dproj">
                        <TextInput
                          id="ci-os-dproj"
                          value={cinfraDesignateProject}
                          onChange={(_e, v) => setCinfraDesignateProject(v)}
                        />
                      </FormGroup>
                      <FormGroup label={t('fields.route53VaultPath')} fieldId="ci-os-r53">
                        <TextInput
                          id="ci-os-r53"
                          value={route53VaultPath}
                          onChange={(_e, v) => setRoute53VaultPath(v)}
                        />
                      </FormGroup>
                    </>
                  )}
                  {cinfraType === 'openshift' && (
                    <>
                      <FormGroup label={t('fields.clusterRef')} fieldId="ci-ocp-cluster">
                        <TextInput
                          id="ci-ocp-cluster"
                          value={cinfraClusterRef}
                          onChange={(_e, v) => setCinfraClusterRef(v)}
                        />
                        <p className="sc-text-muted" style={{ marginTop: '0.25rem' }}>
                          {t('fields.clusterRefHelp')}
                        </p>
                      </FormGroup>
                      <FormGroup label={t('fields.bootImage')} fieldId="ci-ocp-image">
                        <TextInput id="ci-ocp-image" value={cinfraBootImage} onChange={(_e, v) => setCinfraBootImage(v)} />
                      </FormGroup>
                      <FormGroup label={t('fields.storageClass')} fieldId="ci-ocp-sc">
                        <TextInput
                          id="ci-ocp-sc"
                          value={cinfraStorageClass}
                          onChange={(_e, v) => setCinfraStorageClass(v)}
                        />
                      </FormGroup>
                      <FormGroup label={t('fields.clusterNetworkPoolCidr')} fieldId="ci-ocp-pod">
                        <TextInput id="ci-ocp-pod" value={cinfraPodPool} onChange={(_e, v) => setCinfraPodPool(v)} />
                      </FormGroup>
                      <FormGroup label={t('fields.clusterNetworkPoolBlock')} fieldId="ci-ocp-pod-block">
                        <TextInput
                          id="ci-ocp-pod-block"
                          type="number"
                          value={cinfraPodBlock}
                          onChange={(_e, v) => setCinfraPodBlock(v)}
                        />
                      </FormGroup>
                      <FormGroup label={t('fields.serviceNetworkPoolCidr')} fieldId="ci-ocp-svc">
                        <TextInput id="ci-ocp-svc" value={cinfraSvcPool} onChange={(_e, v) => setCinfraSvcPool(v)} />
                      </FormGroup>
                      <FormGroup label={t('fields.serviceNetworkPoolBlock')} fieldId="ci-ocp-svc-block">
                        <TextInput
                          id="ci-ocp-svc-block"
                          type="number"
                          value={cinfraSvcBlock}
                          onChange={(_e, v) => setCinfraSvcBlock(v)}
                        />
                        <p className="sc-text-muted" style={{ marginTop: '0.25rem' }}>
                          {t('fields.hostedClusterCidrDefaultsHelp')}
                        </p>
                      </FormGroup>
                    </>
                  )}
                  {cinfraType === 'aws' && (
                    <>
                      <FormGroup label={t('fields.awsAccountId')} fieldId="ci-aws-account" isRequired>
                        <TextInput
                          id="ci-aws-account"
                          value={cinfraAwsAccount}
                          onChange={(_e, v) => setCinfraAwsAccount(v)}
                          validated={!cinfraAwsAccount || /^[0-9]{12}$/.test(cinfraAwsAccount.trim()) ? 'default' : 'error'}
                          isRequired
                        />
                      </FormGroup>
                      <FormGroup label={t('fields.region')} fieldId="ci-aws-region" isRequired>
                        <TextInput id="ci-aws-region" value={cinfraRegion} onChange={(_e, v) => setCinfraRegion(v)} isRequired />
                      </FormGroup>
                      <FormGroup label={t('fields.baseDomain')} fieldId="ci-aws-base">
                        <TextInput id="ci-aws-base" value={cinfraBaseDomain} onChange={(_e, v) => setCinfraBaseDomain(v)} />
                      </FormGroup>
                    </>
                  )}
                </>
              )}

              {type === 'cloudgateway' && (
                <>
                  <FabricSelect
                    id="cg-fabric"
                    label="Fabric"
                    value={fabricRef}
                    onChange={setFabricRef}
                    options={fabricSelectOptions}
                    isRequired
                  />
                  <CloudInfrastructureSelect
                    id="cg-cloudref"
                    label={t('fields.cloudRef')}
                    value={cloudRefName}
                    onChange={(v) => {
                      setCloudRefName(v);
                      setCloudRefType('');
                    }}
                    onSelectInfrastructure={onCloudInfrastructurePicked}
                    types={['openstack', 'openshift']}
                    isRequired
                  />
                  <p className="sc-text-muted" style={{ marginTop: '-0.75rem' }}>
                    {t('fields.gatewayCloudRefHelp')}
                  </p>
                  <FormGroup label={t('fields.tunnelType')} fieldId="cg-transport">
                    <FormSelect
                      id="cg-transport"
                      value={gatewayTransport}
                      onChange={(_e, v) => setGatewayTransport(v as FabricTunnelType | '')}
                      aria-label={t('fields.tunnelType')}
                    >
                      <FormSelectOption value="" label={t('form.fabricDefaultTransport')} />
                      {FABRIC_TUNNEL_TYPES.map((tt) => (
                        <FormSelectOption
                          key={tt}
                          value={tt}
                          label={tt === 'none' ? t('fields.tunnelNone') : t('fields.tunnelWireguard')}
                        />
                      ))}
                    </FormSelect>
                    <p className="sc-text-muted" style={{ marginTop: '0.25rem' }}>
                      {t('fields.gatewayTransportHelp')}
                    </p>
                  </FormGroup>
                  {gatewayTransport === 'wireguard' && (
                    <FormGroup label={t('fields.gatewayWireguardAddress')} fieldId="cg-wg-address">
                      <TextInput
                        id="cg-wg-address"
                        value={gatewayWgAddress}
                        onChange={(_e, v) => setGatewayWgAddress(v)}
                        placeholder={t('form.allocateAutomatically')}
                      />
                      <p className="sc-text-muted" style={{ marginTop: '0.25rem' }}>
                        {t('fields.gatewayWireguardAddressHelp')}
                      </p>
                    </FormGroup>
                  )}
                  {cloudRefType !== 'openshift' && (
                    <ExpandableSection
                      toggleText={t('form.siteUnderlaySection')}
                      isExpanded={fabricAdvancedOpen}
                      onToggle={(_e, isOpen) => setFabricAdvancedOpen(isOpen)}
                    >
                      <p className="sc-text-muted">{t('fields.siteUnderlayHelp')}</p>
                      <FormGroup label={t('fields.siteUnderlayInterface')} fieldId="cg-su-if">
                        <TextInput
                          id="cg-su-if"
                          value={siteUnderlayInterface}
                          onChange={(_e, v) => setSiteUnderlayInterface(v)}
                        />
                      </FormGroup>
                      <FormGroup label={t('fields.siteUnderlayComputeInterface')} fieldId="cg-su-cif">
                        <TextInput
                          id="cg-su-cif"
                          value={siteComputeInterface}
                          onChange={(_e, v) => setSiteComputeInterface(v)}
                        />
                      </FormGroup>
                      <FormGroup label={t('fields.siteUnderlayCidr')} fieldId="cg-su-cidr">
                        <TextInput
                          id="cg-su-cidr"
                          value={siteUnderlayCidr}
                          onChange={(_e, v) => setSiteUnderlayCidr(v)}
                          placeholder="192.168.80.0/24"
                        />
                      </FormGroup>
                      <FormGroup label={t('fields.siteUnderlayGatewayAddress')} fieldId="cg-su-gw">
                        <TextInput
                          id="cg-su-gw"
                          value={siteUnderlayGateway}
                          onChange={(_e, v) => setSiteUnderlayGateway(v)}
                          placeholder={t('form.firstHostOfCidr')}
                        />
                      </FormGroup>
                    </ExpandableSection>
                  )}
                </>
              )}

              {type === 'transportlink' && (
                <>
                  <FabricSelect
                    id="tl-fabric"
                    label="Fabric"
                    value={fabricRef}
                    onChange={(v) => {
                      setFabricRef(v);
                      setGatewayRef('');
                    }}
                    options={fabricSelectOptions}
                    isRequired
                  />
                  <CloudGatewaySelect
                    id="tl-gw"
                    value={gatewayRef}
                    onChange={setGatewayRef}
                    options={gatewayOptionsForFabric}
                    isRequired
                  />
                </>
              )}

              {type === 'uihealthchecker' && (
                <>
                  <FormGroup label="URL" fieldId="uh-url" isRequired>
                    <TextInput id="uh-url" value={healthUrl} onChange={(_e, v) => setHealthUrl(v)} />
                  </FormGroup>
                  <FormGroup label="Group" fieldId="uh-group">
                    <TextInput id="uh-group" value={healthGroup} onChange={(_e, v) => setHealthGroup(v)} />
                  </FormGroup>
                  <FormGroup label="Display name / description" fieldId="uh-desc">
                    <TextInput id="uh-desc" value={description} onChange={(_e, v) => setDescription(v)} />
                  </FormGroup>
                </>
              )}

              <ActionGroup>
                <Button variant="primary" type="submit" isLoading={submitting} isDisabled={!canSubmit()}>
                  Create
                </Button>
                <Button variant="link" onClick={onCancel}>
                  Cancel
                </Button>
              </ActionGroup>
            </Form>
          </CardBody>
        </Card>
      </div>
    </>
  );
}
