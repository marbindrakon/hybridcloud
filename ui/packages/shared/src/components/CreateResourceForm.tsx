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
import { createDashboardResource, useK8sResourceList } from '../hooks/k8s';
import { HybridSovereignKind, K8sResource } from '../types';
import { PageHeader } from './PageHeader';
import { RbacMultiSelect } from './RbacMultiSelect';
import { FabricSelect } from './FabricSelect';
import { CloudGatewaySelect } from './CloudGatewaySelect';
import { CloudInfrastructureSelect } from './CloudInfrastructureSelect';
import { useTranslation } from '../i18n';
import { DEFAULT_LANDING_ZONE } from '../forms/hybridFabricDefaults';
import {
  CloudGatewayCreateFields,
  CloudInfrastructureCreateFields,
  CloudVirtCreateFields,
  HybridFabricCreateFields,
  HybridNetworkCreateFields,
  NetworkPlacementCreateFields,
  PLATFORM_NAMESPACE,
} from '../forms/NetworkCreateFields';

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
  /** Who is creating: tenant pages never offer platform kinds (default tenant). */
  audience?: 'admin' | 'tenant';
}

/** Kinds that live in the platform namespace and only platform admins create. */
const PLATFORM_FORMS = new Set<SelfServiceFormType>([
  'entity',
  'hybridfabric',
  'cloudinfrastructure',
  'cloudgateway',
  'transportlink',
  'uihealthchecker',
]);

/** Forms whose fields are a network create section (spec reported by the section). */
const NETWORK_SECTION_FORMS = new Set<SelfServiceFormType>([
  'hybridfabric',
  'cloudinfrastructure',
  'cloudgateway',
  'hybridnetwork',
  'networkplacement',
  'cloudvirt',
]);

const TRANSPORT_LINK_TUNNELS = ['wireguard', 'ipsec', 'macsec', 'none'];
const OSO_DNS_PROVIDERS = ['designate', 'route53', 'none'];

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
  audience = 'tenant',
}: CreateResourceFormProps): React.ReactElement {
  const [name, setName] = useState('');
  const [description, setDescription] = useState('');
  const [billingID, setBillingID] = useState('');
  const [websiteLink, setWebsiteLink] = useState('');
  const [teamRef, setTeamRef] = useState('');
  const [projectRef, setProjectRef] = useState('');
  const [platformRef, setPlatformRef] = useState('');
  const [cloudAwsRef, setCloudAwsRef] = useState('');
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
  // TransportLink
  const [fabricRef, setFabricRef] = useState('');
  const [gatewayRef, setGatewayRef] = useState('');
  const [tunnelType, setTunnelType] = useState('wireguard');
  const [region, setRegion] = useState('us-east-1');
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
  // CloudOSO: the site comes from spec.cloudRef (or the installation default); the other site
  // settings are used only when no Cloud Infrastructure is in effect.
  const [cloudRefName, setCloudRefName] = useState('');
  const [osoProject, setOsoProject] = useState('');
  const [osoLegacyOpen, setOsoLegacyOpen] = useState(false);
  const [osoVaultPath, setOsoVaultPath] = useState('');
  const [osoRegionName, setOsoRegionName] = useState('');
  const [osoDnsProvider, setOsoDnsProvider] = useState('');
  const [osoDesignateZoneId, setOsoDesignateZoneId] = useState('');
  const [osoDesignateProjectId, setOsoDesignateProjectId] = useState('');
  const [baseDomain, setBaseDomain] = useState('');
  const [projectDomain, setProjectDomain] = useState('');
  const [externalNetwork, setExternalNetwork] = useState('');
  const [route53VaultPath, setRoute53VaultPath] = useState('');
  const [landingzone, setLandingzone] = useState(DEFAULT_LANDING_ZONE);
  // CloudAWS
  const [awsAccount, setAwsAccount] = useState('');
  const [awsVaultPath, setAwsVaultPath] = useState('');
  const [awsBaseDomain, setAwsBaseDomain] = useState('');
  // PlatformOpenshift
  const [platformType, setPlatformType] = useState('openstack');
  const [platformEnv, setPlatformEnv] = useState('');
  const [cpCount, setCpCount] = useState('3');
  const [workerCount, setWorkerCount] = useState('3');
  const [rbacMulti, setRbacMulti] = useState<string[]>([]);
  const [rbacOperator, setRbacOperator] = useState<string[]>([]);
  const [rbacViewer, setRbacViewer] = useState<string[]>([]);
  const [personaRbacs, setPersonaRbacs] = useState<string[]>([]);
  /** Spec reported by a network create section (null while incomplete or invalid) */
  const [networkSpec, setNetworkSpec] = useState<Record<string, unknown> | null>(null);

  const { t } = useTranslation();
  const type = formType;
  const title = t(`form.titles.${type}`, { defaultValue: FORM_TITLES[type] ?? 'Create Resource' });
  const kind = FORM_KINDS[type];
  const isPlatformForm = PLATFORM_FORMS.has(type);
  const usesNetworkSection = NETWORK_SECTION_FORMS.has(type);
  const [entityName, setEntityName] = useState('');
  const entityNs =
    type === 'entity'
      ? PLATFORM_NAMESPACE
      : type === 'persona' && !namespace
        ? entityName
          ? `entity-${entityName}`
          : ''
        : namespace;

  const entities = useK8sResourceList<K8sResource>('Entity', {
    namespace: PLATFORM_NAMESPACE,
    enabled: type === 'persona' && !namespace,
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
    enabled: (type === 'persona' || type === 'vaultkv' || type === 'aaporg' || type === 'quayorg' || type === 'assignment' || type === 'platformopenshift') && !!entityNs,
  });
  const rbacConfigs = useK8sResourceList<K8sResource>('RbacConfig', {
    enabled: type === 'rbac' || type === 'vault',
  });
  const aapConfigs = useK8sResourceList<K8sResource>('AAPConfig', { enabled: type === 'aaporg' });
  const quayConfigs = useK8sResourceList<K8sResource>('QuayConfig', { enabled: type === 'quayorg' });
  const fabrics = useK8sResourceList<K8sResource>('HybridFabric', {
    namespace: PLATFORM_NAMESPACE,
    enabled: type === 'transportlink',
  });
  const gateways = useK8sResourceList<K8sResource>('CloudGateway', {
    namespace: PLATFORM_NAMESPACE,
    enabled: type === 'transportlink',
  });

  const names = (items: K8sResource[] | undefined | null) =>
    (items ?? [])
      .map((i) => i?.metadata?.name)
      .filter((n): n is string => typeof n === 'string' && n.length > 0)
      .map((n) => ({ value: n, label: n }));

  const firstName = (items: K8sResource[] | undefined | null): string | undefined =>
    names(items)[0]?.value;

  const currentEntityName = (entityNs || '').replace(/^entity-/, '');

  /** FabricSelect options for TransportLink. */
  const fabricSelectOptions = fabrics.items.map((f) => ({
    value: f.metadata.name,
    label: f.metadata.name,
    ready: (f.status as { ready?: boolean } | undefined)?.ready,
  }));

  /** CloudGatewaySelect options for TransportLink — gateways of the selected fabric. */
  const gatewayOptionsForFabric = gateways.items
    .filter((g) => !fabricRef || (g.spec as { fabricRef?: { name?: string } } | undefined)?.fabricRef?.name === fabricRef)
    .map((g) => ({
      value: g.metadata.name,
      label: g.metadata.name,
      ready: (g.status as { ready?: boolean } | undefined)?.ready,
    }));

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

  const optional = (key: string, value: string): Record<string, string> =>
    value.trim() ? { [key]: value.trim() } : {};

  const buildSpec = (): Record<string, unknown> => {
    if (usesNetworkSection) return networkSpec ?? {};
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
        return {
          ...(cloudRefName.trim() ? { cloudRef: { name: cloudRefName.trim() } } : {}),
          project: osoProject.trim(),
          ...optional('landingzone', landingzone),
          ...optional('baseDomain', baseDomain),
          ...optional('projectDomain', projectDomain),
          ...optional('externalNetwork', externalNetwork),
          ...optional('regionName', osoRegionName),
          ...optional('dnsProvider', osoDnsProvider),
          ...optional('designateZoneId', osoDesignateZoneId),
          ...optional('designateProjectId', osoDesignateProjectId),
          ...(audience === 'admin' ? optional('vaultPath', osoVaultPath) : {}),
          ...(audience === 'admin' ? optional('route53VaultPath', route53VaultPath) : {}),
        };
      case 'cloudaws':
        return {
          account: awsAccount.trim(),
          vaultPath: awsVaultPath.trim(),
          baseDomain: (awsBaseDomain || baseDomain).trim(),
          ...optional('landingzone', landingzone),
        };
      case 'platformopenshift': {
        const toolRbac =
          rbacMulti.length || rbacOperator.length || rbacViewer.length
            ? {
                ...(rbacMulti.length ? { clusterAdminRbac: rbacMulti } : {}),
                ...(rbacOperator.length ? { clusterOperatorRbac: rbacOperator } : {}),
                ...(rbacViewer.length ? { clusterViewerRbac: rbacViewer } : {}),
              }
            : undefined;
        if (platformType === 'aws') {
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
        return {
          type: 'openstack',
          openstack: {
            environment: platformEnv || cloudosoRef,
            controlPlaneCount: Number(cpCount) || 3,
            workerCount: Number(workerCount) || 3,
            ...optional('externalNetwork', externalNetwork),
          },
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
      case 'transportlink':
        return {
          enabled: true,
          fabricRef,
          cloudGatewayRef: gatewayRef,
          tunnelType,
        };
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
    if (isPlatformForm && audience !== 'admin') return false;
    if (usesNetworkSection) return networkSpec !== null && !!(type === 'hybridfabric' || type === 'cloudinfrastructure' || type === 'cloudgateway' || entityNs);
    if (type === 'entity' && !billingID) return false;
    if (type === 'persona' && !entityNs) return false;
    if (type === 'assignment' && !teamRef) return false;
    if (type === 'cloudoso' && !osoProject.trim()) return false;
    if (type === 'cloudaws' && (!awsAccount.trim() || !awsVaultPath.trim() || !(awsBaseDomain || baseDomain).trim())) {
      return false;
    }
    if (type === 'platformopenshift' && !(platformEnv || cloudosoRef || cloudAwsRef)) return false;
    if (type === 'persona' && (!(personaRbacs[0] || rbacRef) || !personaType)) return false;
    if (type === 'vaultkv' && !vaultRef) return false;
    if (type === 'migration' && (!vmName || !cloudosoRef)) return false;
    if ((type === 'vault' || type === 'rbac') && !rbacConfig) return false;
    if (type === 'aaporg' && !aapConfig) return false;
    if (type === 'quayorg' && !quayConfig) return false;
    if (type === 'transportlink' && (!fabricRef || !gatewayRef)) return false;
    if (type === 'uihealthchecker' && !healthUrl.startsWith('http')) return false;
    return true;
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!canSubmit()) return;
    setSubmitting(true);
    setResult(null);
    try {
      const targetNs = isPlatformForm ? PLATFORM_NAMESPACE : entityNs;
      if (!targetNs) {
        throw new Error('Namespace is required');
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

  if (isPlatformForm && audience !== 'admin') {
    return (
      <Alert variant="warning" isInline title={t('form.platformKindNotOffered')}>
        {t('form.platformKindNotOfferedBody')}
      </Alert>
    );
  }

  return (
    <>
      <PageHeader
        title={title}
        subtitle={`Namespace ${(isPlatformForm ? PLATFORM_NAMESPACE : entityNs) || 'cluster'}`}
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
                    label={t('fields.site')}
                    value={cloudRefName}
                    onChange={setCloudRefName}
                    types={['openstack']}
                    entityName={currentEntityName}
                    placeholder={t('form.installationDefaultSite')}
                    allowEmpty
                  />
                  <p className="sc-text-muted" style={{ marginTop: '-0.75rem' }}>
                    {t('fields.cloudOsoSiteHelp')}
                  </p>
                  <FormGroup label={t('fields.osoProject')} fieldId="oso-project" isRequired>
                    <TextInput id="oso-project" value={osoProject} onChange={(_e, v) => setOsoProject(v)} isRequired />
                  </FormGroup>
                  <FormGroup label={t('fields.landingzone')} fieldId="oso-lz">
                    <TextInput id="oso-lz" value={landingzone} onChange={(_e, v) => setLandingzone(v)} />
                  </FormGroup>
                  <ExpandableSection
                    toggleText={t('form.osoLegacySection')}
                    isExpanded={osoLegacyOpen}
                    onToggle={(_e, open) => setOsoLegacyOpen(open)}
                  >
                    <p className="sc-text-muted">{t('fields.osoLegacyHelp')}</p>
                    <FormGroup label={t('fields.baseDomain')} fieldId="oso-base">
                      <TextInput id="oso-base" value={baseDomain} onChange={(_e, v) => setBaseDomain(v)} />
                    </FormGroup>
                    <FormGroup label={t('fields.projectDomain')} fieldId="oso-pdom">
                      <TextInput id="oso-pdom" value={projectDomain} onChange={(_e, v) => setProjectDomain(v)} />
                    </FormGroup>
                    <FormGroup label={t('fields.externalNetwork')} fieldId="oso-ext">
                      <TextInput id="oso-ext" value={externalNetwork} onChange={(_e, v) => setExternalNetwork(v)} />
                    </FormGroup>
                    <FormGroup label={t('fields.regionName')} fieldId="oso-region">
                      <TextInput id="oso-region" value={osoRegionName} onChange={(_e, v) => setOsoRegionName(v)} />
                    </FormGroup>
                    <FormGroup label={t('fields.dnsProvider')} fieldId="oso-dns">
                      <FormSelect
                        id="oso-dns"
                        value={osoDnsProvider}
                        onChange={(_e, v) => setOsoDnsProvider(v)}
                        aria-label={t('fields.dnsProvider')}
                      >
                        <FormSelectOption value="" label={t('form.installationDefault')} />
                        {OSO_DNS_PROVIDERS.map((p) => (
                          <FormSelectOption key={p} value={p} label={p} />
                        ))}
                      </FormSelect>
                    </FormGroup>
                    <FormGroup label={t('fields.designateZoneId')} fieldId="oso-dzone">
                      <TextInput
                        id="oso-dzone"
                        value={osoDesignateZoneId}
                        onChange={(_e, v) => setOsoDesignateZoneId(v)}
                      />
                    </FormGroup>
                    <FormGroup label={t('fields.designateProjectId')} fieldId="oso-dproj">
                      <TextInput
                        id="oso-dproj"
                        value={osoDesignateProjectId}
                        onChange={(_e, v) => setOsoDesignateProjectId(v)}
                      />
                    </FormGroup>
                    {audience === 'admin' && (
                      <>
                        <FormGroup label={t('fields.vaultPath')} fieldId="oso-vault">
                          <TextInput id="oso-vault" value={osoVaultPath} onChange={(_e, v) => setOsoVaultPath(v)} />
                        </FormGroup>
                        <FormGroup label={t('fields.route53VaultPath')} fieldId="oso-r53">
                          <TextInput
                            id="oso-r53"
                            value={route53VaultPath}
                            onChange={(_e, v) => setRoute53VaultPath(v)}
                          />
                        </FormGroup>
                      </>
                    )}
                  </ExpandableSection>
                </>
              )}

              {type === 'cloudaws' && (
                <>
                  <FormGroup label="AWS account ID" fieldId="aws-acct" isRequired>
                    <TextInput id="aws-acct" value={awsAccount} onChange={(_e, v) => setAwsAccount(v)} isRequired />
                  </FormGroup>
                  <FormGroup label="Vault path" fieldId="aws-vault" isRequired>
                    <TextInput id="aws-vault" value={awsVaultPath} onChange={(_e, v) => setAwsVaultPath(v)} isRequired />
                  </FormGroup>
                  <FormGroup label="Base domain" fieldId="aws-base" isRequired>
                    <TextInput id="aws-base" value={awsBaseDomain} onChange={(_e, v) => setAwsBaseDomain(v)} isRequired />
                  </FormGroup>
                  <FormGroup label="Landing zone" fieldId="aws-lz">
                    <TextInput id="aws-lz" value={landingzone} onChange={(_e, v) => setLandingzone(v)} />
                  </FormGroup>
                </>
              )}

              {type === 'cloudvirt' && (
                <CloudVirtCreateFields
                  namespace={entityNs}
                  entityName={currentEntityName}
                  onChange={setNetworkSpec}
                />
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
                      setCpCount('3');
                      setWorkerCount(v === 'aws' ? '2' : '3');
                    }}
                    options={[
                      { value: 'openstack', label: 'OpenStack' },
                      { value: 'aws', label: 'AWS' },
                    ]}
                    isRequired
                  />
                  <RefSelect
                    id="platform-env"
                    label={platformType === 'aws' ? 'CloudAWS environment' : 'CloudOSO environment'}
                    value={platformEnv || (platformType === 'aws' ? cloudAwsRef : cloudosoRef)}
                    onChange={(v) => {
                      setPlatformEnv(v);
                      if (platformType === 'aws') setCloudAwsRef(v);
                      else setCloudosoRef(v);
                    }}
                    options={names(platformType === 'aws' ? cloudAwss.items : cloudosos.items)}
                    isRequired
                  />
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
                <HybridNetworkCreateFields
                  namespace={entityNs}
                  entityName={currentEntityName}
                  onChange={setNetworkSpec}
                />
              )}

              {type === 'networkplacement' && (
                <NetworkPlacementCreateFields namespace={entityNs} onChange={setNetworkSpec} />
              )}

              {type === 'hybridfabric' && <HybridFabricCreateFields onChange={setNetworkSpec} />}

              {type === 'cloudinfrastructure' && <CloudInfrastructureCreateFields onChange={setNetworkSpec} />}

              {type === 'cloudgateway' && <CloudGatewayCreateFields onChange={setNetworkSpec} />}

              {type === 'transportlink' && (
                <>
                  <FabricSelect
                    id="tl-fabric"
                    label={t('fields.fabricRef')}
                    value={fabricRef}
                    onChange={(v) => {
                      setFabricRef(v);
                      setGatewayRef('');
                    }}
                    options={fabricSelectOptions}
                    placeholder={t('form.fabricSelectPlaceholder')}
                    emptyStateMessage={t('form.fabricNone')}
                    isRequired
                  />
                  <CloudGatewaySelect
                    id="tl-gw"
                    label={t('fields.cloudGatewayRef')}
                    value={gatewayRef}
                    onChange={setGatewayRef}
                    options={gatewayOptionsForFabric}
                    isRequired
                  />
                  <RefSelect
                    id="tl-tunnel"
                    label={t('fields.tunnelType')}
                    value={tunnelType}
                    onChange={setTunnelType}
                    options={TRANSPORT_LINK_TUNNELS.map((tt) => ({ value: tt, label: tt }))}
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
