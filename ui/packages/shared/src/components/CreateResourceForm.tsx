import React, { useEffect, useMemo, useState } from 'react';
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
  FormHelperText,
  HelperText,
  HelperTextItem,
} from '@patternfly/react-core';
import { useServerCheck } from '../forms/useServerCheck';
import { ReviewPanel } from '../forms/ReviewPanel';
import { causeLines, fieldErrorsFrom } from '../forms/apiErrors';
import { errorText } from '../access/errorText';
import { pushToast } from './ToastHost';
import { usualCreateDuration } from '../status/durations';
import { createDashboardResource, useK8sResourceList } from '../hooks/k8s';
import { HybridSovereignKind, K8sResource } from '../types';
import { PageHeader } from './PageHeader';
import { RbacMultiSelect } from './RbacMultiSelect';
import { EntitySelect } from './EntitySelect';
import { FabricSelect } from './FabricSelect';
import { CloudGatewaySelect } from './CloudGatewaySelect';
import { SitePicker } from './SitePicker';
import { AllowedSitesSelect } from './AllowedSitesSelect';
import { ObjectListEditor, type ObjectListRow } from './ObjectListEditor';
import { cleanContacts, contactsValid, entityOptionalSpec, ENTITY_LIMITS } from '../forms/entityFields';
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
import { kindTerm } from '../i18n/vocabulary';

export type SelfServiceFormType =
  | 'team'
  | 'project'
  | 'assignment'
  | 'cloudoso'
  | 'cloudaws'
  | 'cloudgcp'
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
  /** The new object's page; after Create the form goes there instead of the list. */
  detailPathFor?: (name: string) => string | undefined;
}

/** DNS-1123 label (most kinds); Entity names are shorter and never hold "--" (they become namespaces). */
const DNS_LABEL_RE = /^[a-z0-9]([-a-z0-9]*[a-z0-9])?$/;
function nameProblem(kind: HybridSovereignKind, name: string): string | null {
  if (!name) return null;
  const max = kind === 'Entity' ? 56 : 63;
  if (name.length > max) return `max:${max}`;
  if (!DNS_LABEL_RE.test(name)) return 'pattern';
  if (kind === 'Entity' && name.includes('--')) return 'doubleHyphen';
  return null;
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
  team: 'Create team setup',
  project: 'Create app project',
  assignment: 'Create cluster assignment',
  cloudoso: 'Request an OpenStack environment',
  cloudaws: 'Request an AWS environment',
  cloudgcp: 'Request a Google Cloud environment',
  cloudvirt: 'Request an OpenShift Virtualization environment',
  platformopenshift: 'Create OpenShift cluster',
  migration: 'Migrate to OpenStack',
  persona: 'Create Persona',
  rbac: 'Create access group',
  vault: 'Create Vault',
  vaultkv: 'Create Vault KV',
  aaporg: 'Create AAP Org',
  quayorg: 'Create Quay Org',
  entity: 'Create tenant',
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
  cloudgcp: 'CloudGCP',
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

/**
 * Tenant-scoped kinds that live in entity-<tenant>. These forms get the
 * EntitySelect dropdown so the target namespace is picked, not typed.
 * Persona is handled separately: it already had its own inline Entity picker.
 */
const ENTITY_SCOPED_FORMS = new Set<SelfServiceFormType>([
  'team',
  'project',
  'assignment',
  'platformopenshift',
]);

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

/** One role field of a service object: the access groups whose members get that role. */
interface RoleField {
  key: string;
  labelKey: string;
  helpKey: string;
}

const VAULTKV_ROLES: readonly RoleField[] = [
  { key: 'vaultAdminRbac', labelKey: 'form.roles.vaultAdmin', helpKey: 'form.roles.vaultAdminHelp' },
  { key: 'vaultOpsRbac', labelKey: 'form.roles.vaultOps', helpKey: 'form.roles.vaultOpsHelp' },
  { key: 'vaultDeveloperRbac', labelKey: 'form.roles.vaultDeveloper', helpKey: 'form.roles.vaultDeveloperHelp' },
  { key: 'vaultReaderRbac', labelKey: 'form.roles.vaultReader', helpKey: 'form.roles.vaultReaderHelp' },
];
const AAPORG_ROLES: readonly RoleField[] = [
  { key: 'aapAdminRbac', labelKey: 'form.roles.aapAdmin', helpKey: 'form.roles.aapAdminHelp' },
  { key: 'aapJobExecutorRbac', labelKey: 'form.roles.aapJobExecutor', helpKey: 'form.roles.aapJobExecutorHelp' },
  { key: 'aapViewerRbac', labelKey: 'form.roles.aapViewer', helpKey: 'form.roles.aapViewerHelp' },
];
const QUAYORG_ROLES: readonly RoleField[] = [
  { key: 'quayAdminRbac', labelKey: 'form.roles.quayAdmin', helpKey: 'form.roles.quayAdminHelp' },
  { key: 'quayCreatorRbac', labelKey: 'form.roles.quayCreator', helpKey: 'form.roles.quayCreatorHelp' },
  { key: 'quayMemberRbac', labelKey: 'form.roles.quayMember', helpKey: 'form.roles.quayMemberHelp' },
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
  detailPathFor,
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
  const [nameTouched, setNameTouched] = useState(false);
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
  // Entity: who the tenant's users contact, and the sites it may use (empty = every site that allows it).
  const [contactRows, setContactRows] = useState<ObjectListRow[]>([]);
  const [allowedSites, setAllowedSites] = useState<string[]>([]);
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
  // CloudAWS / CloudGCP: the site (spec.cloudRef); the platform assigns the account or project.
  const [awsSiteRef, setAwsSiteRef] = useState('');
  const [gcpSiteRef, setGcpSiteRef] = useState('');
  const [cloudGcpRef, setCloudGcpRef] = useState('');
  // PlatformOpenshift
  const [platformType, setPlatformType] = useState('openstack');
  const [platformEnv, setPlatformEnv] = useState('');
  const [cpCount, setCpCount] = useState('3');
  const [workerCount, setWorkerCount] = useState('3');
  const [rbacMulti, setRbacMulti] = useState<string[]>([]);
  const [rbacOperator, setRbacOperator] = useState<string[]>([]);
  const [rbacDeveloper, setRbacDeveloper] = useState<string[]>([]);
  const [rbacViewer, setRbacViewer] = useState<string[]>([]);
  // Service objects: one access-group list per role field (UX-019).
  const [roleLists, setRoleLists] = useState<Record<string, string[]>>({});
  const roleList = (key: string): string[] => roleLists[key] ?? [];
  const setRoleList = (key: string) => (next: string[]) => setRoleLists((prev) => ({ ...prev, [key]: next }));
  // Project access fields (the interim form of a role assignment on the project, OD15).
  const [projectAdminRbac, setProjectAdminRbac] = useState<string[]>([]);
  const [projectDeveloperRbac, setProjectDeveloperRbac] = useState<string[]>([]);
  const [projectViewerRbac, setProjectViewerRbac] = useState<string[]>([]);
  // Team RBAC: Rbac CR names in the tenant namespace; each list is written only when it has entries.
  const [teamAdminRbac, setTeamAdminRbac] = useState<string[]>([]);
  const [teamMemberRbac, setTeamMemberRbac] = useState<string[]>([]);
  const [teamViewerRbac, setTeamViewerRbac] = useState<string[]>([]);
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
  const entityScoped = ENTITY_SCOPED_FORMS.has(type);
  /** Namespace resolved from the Entity the user picked in EntitySelect. */
  const [pickedEntityNs, setPickedEntityNs] = useState('');
  const entityNs =
    type === 'entity'
      ? PLATFORM_NAMESPACE
      : type === 'persona' && !namespace
        ? entityName
          ? `entity-${entityName}`
          : ''
        : entityScoped
          ? // Until the Entity list resolves, stay on whatever the host app scoped us to.
            pickedEntityNs || namespace
          : namespace;

  /** Picking a different tenant invalidates every namespace-scoped reference below. */
  const onEntityPicked = React.useCallback((next: string, nextNs: string) => {
    setEntityName(next);
    setPickedEntityNs(nextNs);
    setTeamRef('');
    setProjectRef('');
    setProjectRefs('');
    setPlatformRef('');
    setPlatformEnv('');
    setCloudosoRef('');
    setCloudAwsRef('');
    setAssignAdmin([]);
    setAssignDev([]);
    setAssignViewer([]);
    setAssignOps([]);
    setRbacMulti([]);
    setRbacOperator([]);
    setRbacDeveloper([]);
    setRbacViewer([]);
    setRoleLists({});
    setProjectAdminRbac([]);
    setProjectDeveloperRbac([]);
    setProjectViewerRbac([]);
    setTeamAdminRbac([]);
    setTeamMemberRbac([]);
    setTeamViewerRbac([]);
  }, []);

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
  const cloudGcps = useK8sResourceList<K8sResource>('CloudGCP', {
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
    enabled:
      (type === 'persona' ||
        type === 'vaultkv' ||
        type === 'aaporg' ||
        type === 'quayorg' ||
        type === 'assignment' ||
        type === 'platformopenshift' ||
        type === 'team' ||
        type === 'project') &&
      !!entityNs,
  });
  // Integration connections live in the platform's integration namespace, which only platform
  // administrators can read (tenants get 403, UX-013). Tenant forms never ask for them: the
  // platform uses its default connection and reports the one used in status.config.
  const isAdmin = audience === 'admin';
  /** The administrator chose a connection (or the platform default): stop preselecting. */
  const connectionTouched = React.useRef(false);
  const rbacConfigs = useK8sResourceList<K8sResource>('RbacConfig', {
    enabled: isAdmin && (type === 'rbac' || type === 'vault'),
  });
  const aapConfigs = useK8sResourceList<K8sResource>('AAPConfig', { enabled: isAdmin && type === 'aaporg' });
  const quayConfigs = useK8sResourceList<K8sResource>('QuayConfig', { enabled: isAdmin && type === 'quayorg' });
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
    // Platform administrators: preselect the only connection (they may clear it under Advanced).
    if (isAdmin && !connectionTouched.current && (type === 'rbac' || type === 'vault')) {
      if (!rbacConfig) {
        const n = firstName(rbacConfigs.items);
        if (n) setRbacConfig(n);
      }
    }
    if (isAdmin && !connectionTouched.current && type === 'aaporg' && !aapConfig) {
      const n = firstName(aapConfigs.items);
      if (n) setAapConfig(n);
    }
    if (isAdmin && !connectionTouched.current && type === 'quayorg' && !quayConfig) {
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
    isAdmin,
  ]);

  const optional = (key: string, value: string): Record<string, string> =>
    value.trim() ? { [key]: value.trim() } : {};

  /** The role lists of a service object that have entries. */
  const rolesSpec = (roles: readonly RoleField[]): Record<string, string[]> =>
    Object.fromEntries(roles.map((r) => [r.key, roleList(r.key)]).filter(([, l]) => (l as string[]).length));

  /** Entity contacts as sent (empty rows dropped) and what is wrong with them. */
  const entityContacts = cleanContacts(type === 'entity' ? contactRows : []);

  const buildSpec = (): Record<string, unknown> => {
    if (usesNetworkSection) return networkSpec ?? {};
    switch (type) {
      case 'entity':
        return {
          description,
          billingID,
          ...(websiteLink ? { websiteLink } : {}),
          ...entityOptionalSpec({ displayName, contacts: entityContacts.contacts, allowedSites }),
        };
      case 'team':
        return {
          features: { argo: argoEnabled, istio: istioEnabled },
          ...(teamAdminRbac.length ? { teamAdmin: teamAdminRbac } : {}),
          ...(teamMemberRbac.length ? { teamMemberRbac } : {}),
          ...(teamViewerRbac.length ? { teamViewerRbac } : {}),
        };
      case 'project':
        return {
          ...(description ? { description } : {}),
          ...(displayName ? { displayName } : {}),
          ...(projectAdminRbac.length ? { projectAdmin: projectAdminRbac } : {}),
          ...(projectDeveloperRbac.length ? { projectDeveloperRbac } : {}),
          ...(projectViewerRbac.length ? { projectViewerRbac } : {}),
        };
      case 'assignment': {
        const projectsList = projectRefs
          ? projectRefs.split(/[,\n]/).map((s) => s.trim()).filter(Boolean)
          : projectRef
            ? [projectRef]
            : [];
        // Assignment toolRbac fields are single access group names (single selects below).
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
        // The CRD needs only the site; the platform assigns the account (UX-038).
        return { cloudRef: { name: awsSiteRef.trim() } };
      case 'cloudgcp':
        return { cloudRef: { name: gcpSiteRef.trim() } };
      case 'platformopenshift': {
        const toolRbac =
          rbacMulti.length || rbacOperator.length || rbacDeveloper.length || rbacViewer.length
            ? {
                ...(rbacMulti.length ? { clusterAdminRbac: rbacMulti } : {}),
                ...(rbacOperator.length ? { clusterOperatorRbac: rbacOperator } : {}),
                ...(rbacDeveloper.length ? { clusterDeveloperRbac: rbacDeveloper } : {}),
                ...(rbacViewer.length ? { clusterViewerRbac: rbacViewer } : {}),
              }
            : undefined;
        if (platformType === 'gcp') {
          return {
            type: 'gcp',
            gcp: {
              environment: platformEnv || cloudGcpRef,
              ...optional('region', region),
              controlPlaneCount: Number(cpCount) || 3,
              workerCount: Number(workerCount) || 2,
            },
            ...(toolRbac ? { toolRbac } : {}),
          };
        }
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
      // Connection references are optional: without one the platform uses its default connection.
      case 'rbac':
        return { ...optional('config', rbacConfig), ...optional('description', description) };
      case 'vault':
        return { ha: haEnabled, ...optional('rbacConfig', rbacConfig) };
      case 'vaultkv':
        return { vault: vaultRef, ...rolesSpec(VAULTKV_ROLES) };
      case 'aaporg':
        return { ...optional('aapConfig', aapConfig), ...rolesSpec(AAPORG_ROLES) };
      case 'quayorg':
        return { ...optional('quayConfig', quayConfig), ...rolesSpec(QUAYORG_ROLES) };
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
    if (type === 'entity' && (!contactsValid(entityContacts) || displayName.trim().length > ENTITY_LIMITS.displayNameMax)) return false;
    if (type === 'persona' && !entityNs) return false;
    // Entity-scoped kinds cannot be created without a resolved tenant namespace.
    if (entityScoped && !entityNs) return false;
    if (type === 'assignment' && !teamRef) return false;
    if (type === 'cloudoso' && !osoProject.trim()) return false;
    if (type === 'cloudaws' && !awsSiteRef.trim()) return false;
    if (type === 'cloudgcp' && !gcpSiteRef.trim()) return false;
    if (type === 'platformopenshift' && !(platformEnv || cloudosoRef || cloudAwsRef || cloudGcpRef)) return false;
    if (type === 'persona' && (!(personaRbacs[0] || rbacRef) || !personaType)) return false;
    if (type === 'vaultkv' && !vaultRef) return false;
    if (type === 'migration' && (!vmName || !cloudosoRef)) return false;
    if (type === 'transportlink' && (!fabricRef || !gatewayRef)) return false;
    if (type === 'uihealthchecker' && !healthUrl.startsWith('http')) return false;
    return true;
  };

  // Client-side checks first (inline), then a server dry run of the exact object (Q8, UX-040).
  const targetNs = isPlatformForm ? PLATFORM_NAMESPACE : entityNs;
  const nameIssue = nameProblem(kind, name);
  // Tenant names become namespaces: the rule shows as soon as a wrong character is typed.
  const showNameIssue = !!nameIssue && (nameTouched || type === 'entity');
  const clientValid = canSubmit() && !nameIssue && !!targetNs;
  const specNow = clientValid ? buildSpec() : null;
  const checkBody = useMemo(
    () => (clientValid && specNow ? { name, namespace: targetNs, spec: specNow } : null),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [clientValid, name, targetNs, JSON.stringify(specNow)],
  );
  const check = useServerCheck(kind, checkBody, clientValid);
  const checkErrors = fieldErrorsFrom(check.error, {
    'metadata.name': 'name',
    ...(type === 'entity'
      ? { 'spec.displayName': 'displayName', 'spec.billingID': 'billingID', 'spec.contacts': 'contacts', 'spec.allowedSites': 'allowedSites' }
      : {}),
  });
  const checkText = check.error ? errorText(check.error, t, { kindLabel: kindTerm(t, kind), action: 'action' }) : null;
  const blockers: string[] = [];
  if (!name) blockers.push(t('form.blockName'));
  else if (nameIssue) blockers.push(t(`form.nameRule.${nameIssue.split(':')[0]}`, { max: nameIssue.split(':')[1] }));
  if (name && !nameIssue && !canSubmit()) blockers.push(t('form.blockRequired'));
  if (checkText) blockers.push(t('form.blockServer', { reason: [checkText.body || checkText.title, ...causeLines(checkErrors.other)].filter(Boolean).join(' ') }));
  const canCreate = clientValid && check.passed && !submitting;

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!canCreate || !targetNs) return;
    setSubmitting(true);
    setResult(null);
    try {
      await createDashboardResource(kind, { name, namespace: targetNs, spec: buildSpec() }, targetNs);
      const usual = usualCreateDuration(kind);
      pushToast({
        variant: 'success',
        title: t('form.created', { kind: kindTerm(t, kind), name }),
        body: usual ? t(usual.key, { defaultValue: usual.defaultValue }) : undefined,
      });
      // Stay with the new object: its page shows the progress (UX-090).
      onSuccess(detailPathFor?.(name) ?? listPath);
    } catch (err) {
      const txt = errorText(err, t, { kindLabel: kindTerm(t, kind), action: 'action' });
      const fe = fieldErrorsFrom(err, { 'metadata.name': 'name' });
      setResult({
        ok: false,
        message: [txt.body || txt.title, fe.byField.name, ...causeLines(fe.other)].filter(Boolean).join(' '),
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
        subtitle={t('form.inNamespace', { namespace: (isPlatformForm ? PLATFORM_NAMESPACE : entityNs) || '—' })}
        breadcrumbs={[
          { label: t('nav.sovereignCloud') },
          { label: kindTerm(t, kind, 'many'), to: listPath },
          { label: t('common.create') },
        ]}
      />
      {result && (
        <Alert
          variant={result.ok ? 'success' : 'danger'}
          title={result.ok ? t('form.submitted') : t('form.submitFailed')}
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
              {entityScoped && (
                <EntitySelect
                  id="entity-scope"
                  value={entityName}
                  onChange={onEntityPicked}
                  fallbackNamespace={namespace}
                  helperText={t('form.entityScopeHelp', {
                    namespace: entityNs || '—',
                    defaultValue: `The ${kind} is created in ${entityNs || '—'}.`,
                  })}
                  isRequired
                />
              )}

              <FormGroup label={t('common.name')} isRequired fieldId="name">
                <TextInput
                  id="name"
                  value={name}
                  onChange={(_e, v) => setName(v)}
                  onBlur={() => setNameTouched(true)}
                  isRequired
                  validated={showNameIssue || checkErrors.byField.name ? 'error' : 'default'}
                  aria-describedby="name-help"
                />
                <FormHelperText>
                  <HelperText id="name-help">
                    {showNameIssue && nameIssue ? (
                      <HelperTextItem variant="error">
                        {t(`form.nameRule.${nameIssue.split(':')[0]}`, { max: nameIssue.split(':')[1] })}
                      </HelperTextItem>
                    ) : checkErrors.byField.name ? (
                      <HelperTextItem variant="error">{checkErrors.byField.name}</HelperTextItem>
                    ) : (
                      <HelperTextItem>
                        {type === 'entity'
                          ? t('form.entityNameHelp', { namespace: `entity-${name || '<name>'}` })
                          : t('form.nameHelp')}
                      </HelperTextItem>
                    )}
                  </HelperText>
                </FormHelperText>
              </FormGroup>

              {type === 'entity' && (
                <>
                  <FormGroup label={t('fields.displayName')} fieldId="entity-display-name">
                    <TextInput
                      id="entity-display-name"
                      value={displayName}
                      onChange={(_e, v) => setDisplayName(v)}
                      validated={displayName.trim().length > ENTITY_LIMITS.displayNameMax || checkErrors.byField.displayName ? 'error' : 'default'}
                      aria-describedby="entity-display-name-help"
                    />
                    <FormHelperText>
                      <HelperText id="entity-display-name-help">
                        {displayName.trim().length > ENTITY_LIMITS.displayNameMax ? (
                          <HelperTextItem variant="error">{t('form.nameRule.max', { max: ENTITY_LIMITS.displayNameMax })}</HelperTextItem>
                        ) : checkErrors.byField.displayName ? (
                          <HelperTextItem variant="error">{checkErrors.byField.displayName}</HelperTextItem>
                        ) : (
                          <HelperTextItem>{t('entityForm.displayNameHelp')}</HelperTextItem>
                        )}
                      </HelperText>
                    </FormHelperText>
                  </FormGroup>
                  <FormGroup label={t('fields.billingID')} isRequired fieldId="billing">
                    <TextInput
                      id="billing"
                      value={billingID}
                      onChange={(_e, v) => setBillingID(v)}
                      isRequired
                      validated={checkErrors.byField.billingID ? 'error' : 'default'}
                    />
                    <FormHelperText>
                      <HelperText>
                        {checkErrors.byField.billingID ? (
                          <HelperTextItem variant="error">{checkErrors.byField.billingID}</HelperTextItem>
                        ) : (
                          <HelperTextItem>{t('entityForm.billingHelp')}</HelperTextItem>
                        )}
                      </HelperText>
                    </FormHelperText>
                  </FormGroup>
                  <FormGroup label={t('fields.description')} fieldId="description">
                    <TextArea id="description" value={description} onChange={(_e, v) => setDescription(v)} />
                  </FormGroup>
                  <FormGroup label={t('fields.websiteLink')} fieldId="website">
                    <TextInput id="website" value={websiteLink} onChange={(_e, v) => setWebsiteLink(v)} />
                  </FormGroup>
                  <ObjectListEditor
                    id="entity-contacts"
                    label={t('entityForm.contacts')}
                    columns={[
                      {
                        key: 'name',
                        label: t('entityForm.contactName'),
                        validate: (v, row) => !(row.email || row.url) || !!String(v ?? '').trim(),
                      },
                      {
                        key: 'email',
                        label: t('entityForm.contactEmail'),
                        validate: (v) => !String(v ?? '').trim() || /^[^\s@]+@[^\s@]+$/.test(String(v).trim()),
                      },
                      {
                        key: 'url',
                        label: t('entityForm.contactUrl'),
                        placeholder: 'https://',
                        validate: (v) => !String(v ?? '').trim() || String(v).trim().startsWith('https://'),
                      },
                    ]}
                    value={contactRows}
                    onChange={setContactRows}
                    maxItems={ENTITY_LIMITS.contactsMax}
                    addLabel={t('entityForm.addContact')}
                    removeLabel={t('form.removeRow')}
                    helperText={
                      contactsValid(entityContacts)
                        ? checkErrors.byField.contacts || t('entityForm.contactsHelp')
                        : t('entityForm.contactsInvalid')
                    }
                  />
                  <AllowedSitesSelect
                    id="entity-allowed-sites"
                    tenant={name}
                    value={allowedSites}
                    onChange={setAllowedSites}
                    error={checkErrors.byField.allowedSites}
                  />
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
                      aria-label={t('fields.argo')}
                      isChecked={argoEnabled}
                      onChange={(_e, checked) => setArgoEnabled(checked)}
                    />
                  </FormGroup>
                  <FormGroup label="Enable Istio" fieldId="istio">
                    <Switch
                      id="istio"
                      aria-label={t('fields.istio')}
                      isChecked={istioEnabled}
                      onChange={(_e, checked) => setIstioEnabled(checked)}
                    />
                  </FormGroup>
                  {/* Access groups of the selected tenant (the interim form of a role assignment, OD15). */}
                  <RbacMultiSelect
                    id="team-rbac-admin"
                    label={t('form.roles.teamAdmins')}
                    helperText={`${t('form.roles.teamAdminsHelp')} ${t('form.roles.accessListRule')}`}
                    value={teamAdminRbac}
                    onChange={setTeamAdminRbac}
                    options={names(rbacs.items)}
                  />
                  <RbacMultiSelect
                    id="team-rbac-member"
                    label={t('form.roles.teamMembers')}
                    helperText={`${t('form.roles.teamMembersHelp')} ${t('form.roles.accessListRule')}`}
                    value={teamMemberRbac}
                    onChange={setTeamMemberRbac}
                    options={names(rbacs.items)}
                  />
                  <RbacMultiSelect
                    id="team-rbac-viewer"
                    label={t('form.roles.teamViewers')}
                    helperText={`${t('form.roles.teamViewersHelp')} ${t('form.roles.accessListRule')}`}
                    value={teamViewerRbac}
                    onChange={setTeamViewerRbac}
                    options={names(rbacs.items)}
                  />
                  <Alert variant="info" isInline isPlain title={t('form.accessInterimNote')} className="sc-mb" />
                </>
              )}

              {type === 'project' && (
                <>
                  <FormGroup label={t('fields.displayName')} fieldId="display-name">
                    <TextInput id="display-name" value={displayName} onChange={(_e, v) => setDisplayName(v)} />
                  </FormGroup>
                  <RbacMultiSelect
                    id="project-rbac-admin"
                    label={t('form.roles.projectAdmins')}
                    helperText={`${t('form.roles.projectAdminsHelp')} ${t('form.roles.accessListRule')}`}
                    value={projectAdminRbac}
                    onChange={setProjectAdminRbac}
                    options={names(rbacs.items)}
                  />
                  <RbacMultiSelect
                    id="project-rbac-developer"
                    label={t('form.roles.projectDevelopers')}
                    helperText={`${t('form.roles.projectDevelopersHelp')} ${t('form.roles.accessListRule')}`}
                    value={projectDeveloperRbac}
                    onChange={setProjectDeveloperRbac}
                    options={names(rbacs.items)}
                  />
                  <RbacMultiSelect
                    id="project-rbac-viewer"
                    label={t('form.roles.projectViewers')}
                    helperText={`${t('form.roles.projectViewersHelp')} ${t('form.roles.accessListRule')}`}
                    value={projectViewerRbac}
                    onChange={setProjectViewerRbac}
                    options={names(rbacs.items)}
                  />
                  <Alert variant="info" isInline isPlain title={t('form.accessInterimNote')} className="sc-mb" />
                </>
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
                    label={t('kindsOne.PlatformOpenshift')}
                    value={platformRef}
                    onChange={setPlatformRef}
                    options={names(platforms.items)}
                    placeholder="Optional"
                  />
                  {/* toolRbac fields hold one access group each. */}
                  <RefSelect
                    id="assign-admin"
                    label={t('form.roles.assignmentAdmin')}
                    value={assignAdmin[0] ?? ''}
                    onChange={(v) => setAssignAdmin(v ? [v] : [])}
                    options={names(rbacs.items)}
                    placeholder={t('form.optionalNone')}
                  />
                  {/* toolRbac fields hold one access group each. */}
                  <RefSelect
                    id="assign-dev"
                    label={t('form.roles.assignmentDeveloper')}
                    value={assignDev[0] ?? ''}
                    onChange={(v) => setAssignDev(v ? [v] : [])}
                    options={names(rbacs.items)}
                    placeholder={t('form.optionalNone')}
                  />
                  {/* toolRbac fields hold one access group each. */}
                  <RefSelect
                    id="assign-viewer"
                    label={t('form.roles.assignmentViewer')}
                    value={assignViewer[0] ?? ''}
                    onChange={(v) => setAssignViewer(v ? [v] : [])}
                    options={names(rbacs.items)}
                    placeholder={t('form.optionalNone')}
                  />
                  {/* toolRbac fields hold one access group each. */}
                  <RefSelect
                    id="assign-ops"
                    label={t('form.roles.assignmentOps')}
                    value={assignOps[0] ?? ''}
                    onChange={(v) => setAssignOps(v ? [v] : [])}
                    options={names(rbacs.items)}
                    placeholder={t('form.optionalNone')}
                  />
                </>
              )}

              {type === 'cloudoso' && (
                <>
                  <SitePicker
                    id="oso-cloudref"
                    label={t('fields.site')}
                    value={cloudRefName}
                    onChange={setCloudRefName}
                    types={['openstack']}
                    namespace={entityNs}
                    audience={audience}
                    emptyLabel={t('form.installationDefaultSite')}
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
                  <SitePicker
                    id="aws-site"
                    label={t('form.awsSite')}
                    value={awsSiteRef}
                    onChange={setAwsSiteRef}
                    types={['aws']}
                    namespace={entityNs}
                    audience={audience}
                    isRequired
                  />
                  <Alert variant="info" isInline isPlain className="sc-mb" title={t('form.awsAccountAssigned')}>
                    {t('form.publicCloudBillable')}
                  </Alert>
                </>
              )}

              {type === 'cloudgcp' && (
                <>
                  <SitePicker
                    id="gcp-site"
                    label={t('form.gcpSite')}
                    value={gcpSiteRef}
                    onChange={setGcpSiteRef}
                    types={['gcp']}
                    namespace={entityNs}
                    audience={audience}
                    isRequired
                  />
                  <Alert variant="info" isInline isPlain className="sc-mb" title={t('form.gcpProjectAssigned')}>
                    {t('form.publicCloudBillable')}
                  </Alert>
                </>
              )}

              {type === 'cloudvirt' && (
                <CloudVirtCreateFields
                  namespace={entityNs}
                  entityName={currentEntityName}
                  onChange={setNetworkSpec}
                  audience={audience}
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
                      setWorkerCount(v === 'aws' || v === 'gcp' ? '2' : '3');
                    }}
                    options={[
                      { value: 'openstack', label: 'OpenStack' },
                      { value: 'aws', label: 'AWS' },
                      { value: 'gcp', label: 'Google Cloud' },
                    ]}
                    isRequired
                  />
                  <RefSelect
                    id="platform-env"
                    label={kindTerm(t, platformType === 'aws' ? 'CloudAWS' : platformType === 'gcp' ? 'CloudGCP' : 'CloudOSO')}
                    value={platformEnv || (platformType === 'aws' ? cloudAwsRef : platformType === 'gcp' ? cloudGcpRef : cloudosoRef)}
                    onChange={(v) => {
                      setPlatformEnv(v);
                      if (platformType === 'aws') setCloudAwsRef(v);
                      else if (platformType === 'gcp') setCloudGcpRef(v);
                      else setCloudosoRef(v);
                    }}
                    options={names(platformType === 'aws' ? cloudAwss.items : platformType === 'gcp' ? cloudGcps.items : cloudosos.items)}
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
                  {(platformType === 'aws' || platformType === 'gcp') && (
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
                    label={t('form.roles.clusterAdmins')}
                    value={rbacMulti}
                    onChange={setRbacMulti}
                    options={names(rbacs.items)}
                  />
                  <RbacMultiSelect
                    id="plat-rbac-op"
                    label={t('form.roles.clusterOperators')}
                    value={rbacOperator}
                    onChange={setRbacOperator}
                    options={names(rbacs.items)}
                  />
                  <RbacMultiSelect
                    id="plat-rbac-dev"
                    label={t('form.roles.clusterDevelopers')}
                    value={rbacDeveloper}
                    onChange={setRbacDeveloper}
                    options={names(rbacs.items)}
                  />
                  <RbacMultiSelect
                    id="plat-rbac-view"
                    label={t('form.roles.clusterViewers')}
                    value={rbacViewer}
                    onChange={setRbacViewer}
                    options={names(rbacs.items)}
                  />
                </>
              )}

              {type === 'persona' && (
                <>
                  {!namespace && (
                    <RefSelect
                      id="entity"
                      label={t('kindsOne.Entity')}
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
                    label={t('kindsOne.Rbac')}
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

              {type === 'vault' && (
                <FormGroup label={t('form.vaultHa')} fieldId="vault-ha">
                  <Switch id="vault-ha" isChecked={haEnabled} onChange={(_e, c) => setHaEnabled(c)} />
                </FormGroup>
              )}

              {type === 'vaultkv' && (
                <>
                  <RefSelect id="vault-ref" label={t('kindsOne.Vault')} value={vaultRef} onChange={setVaultRef} options={names(vaults.items)} isRequired />
                  {VAULTKV_ROLES.map((r) => (
                    <RbacMultiSelect
                      key={r.key}
                      id={`vaultkv-${r.key}`}
                      label={t(r.labelKey)}
                      helperText={t(r.helpKey)}
                      value={roleList(r.key)}
                      onChange={setRoleList(r.key)}
                      options={names(rbacs.items)}
                    />
                  ))}
                </>
              )}

              {type === 'aaporg' &&
                AAPORG_ROLES.map((r) => (
                  <RbacMultiSelect
                    key={r.key}
                    id={`aap-${r.key}`}
                    label={t(r.labelKey)}
                    helperText={t(r.helpKey)}
                    value={roleList(r.key)}
                    onChange={setRoleList(r.key)}
                    options={names(rbacs.items)}
                  />
                ))}

              {type === 'quayorg' &&
                QUAYORG_ROLES.map((r) => (
                  <RbacMultiSelect
                    key={r.key}
                    id={`quay-${r.key}`}
                    label={t(r.labelKey)}
                    helperText={t(r.helpKey)}
                    value={roleList(r.key)}
                    onChange={setRoleList(r.key)}
                    options={names(rbacs.items)}
                  />
                ))}

              {isAdmin && (type === 'rbac' || type === 'vault' || type === 'aaporg' || type === 'quayorg') && (
                <ExpandableSection toggleText={t('form.advanced')}>
                  <RefSelect
                    id="connection"
                    label={t('form.connection')}
                    value={type === 'aaporg' ? aapConfig : type === 'quayorg' ? quayConfig : rbacConfig}
                    onChange={(v) => {
                      connectionTouched.current = true;
                      (type === 'aaporg' ? setAapConfig : type === 'quayorg' ? setQuayConfig : setRbacConfig)(v);
                    }}
                    options={names(type === 'aaporg' ? aapConfigs.items : type === 'quayorg' ? quayConfigs.items : rbacConfigs.items)}
                    placeholder={t('form.connectionDefault')}
                  />
                  <p className="sc-text-muted">{t('form.connectionHelp')}</p>
                </ExpandableSection>
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
                    label={kindTerm(t, 'CloudOSO')}
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

              {clientValid && <ReviewPanel kind={kind} name={name} namespace={targetNs} spec={specNow} />}
              {check.warnings.length > 0 && (
                <Alert variant="warning" isInline isPlain title={t('form.apiWarnings')} className="sc-mb">
                  <ul>
                    {check.warnings.map((w) => (
                      <li key={w}>{w}</li>
                    ))}
                  </ul>
                </Alert>
              )}
              <ActionGroup>
                <Button
                  variant="primary"
                  type="submit"
                  isLoading={submitting || (clientValid && check.checking)}
                  isAriaDisabled={!canCreate}
                  aria-describedby="create-blockers"
                >
                  {t('common.create')}
                </Button>
                <Button variant="link" onClick={onCancel}>
                  {t('common.cancel')}
                </Button>
              </ActionGroup>
              <div id="create-blockers" className="sc-text-muted" aria-live="polite">
                {clientValid && check.checking ? (
                  t('form.checking')
                ) : blockers.length ? (
                  <>
                    {t('form.createBlockedBy')}
                    <ul>
                      {blockers.map((b) => (
                        <li key={b}>{b}</li>
                      ))}
                    </ul>
                  </>
                ) : null}
              </div>
            </Form>
          </CardBody>
        </Card>
      </div>
    </>
  );
}
