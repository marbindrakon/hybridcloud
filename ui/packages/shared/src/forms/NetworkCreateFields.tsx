/**
 * Create-form sections for the network kinds. Each section owns its inputs and reports the spec it
 * would submit, or null while the input is incomplete or invalid. Fields and rules follow the CRDs;
 * the only preset values are the CRDs' own defaults.
 */
import React, { useEffect, useMemo, useState } from 'react';
import {
  ExpandableSection,
  FormGroup,
  FormSelect,
  FormSelectOption,
  Switch,
  TextArea,
  TextInput,
} from '@patternfly/react-core';
import { useK8sResourceList } from '../hooks/k8s';
import { useTranslation } from '../i18n';
import type {
  CloudInfrastructure,
  CloudInfrastructureType,
  GatewayTransport,
  HybridFabric,
  K8sResource,
  NetworkPlacementBackendKind,
} from '../types';
import { EntityMultiSelect } from '../components/EntityMultiSelect';
import { FabricSelect } from '../components/FabricSelect';
import { CloudInfrastructureSelect } from '../components/CloudInfrastructureSelect';
import { RbacMultiSelect } from '../components/RbacMultiSelect';
import { BackendSelect, buildBackendOptions } from '../components/BackendSelect';
import { ObjectListEditor, type ObjectListRow } from '../components/ObjectListEditor';
import { ListTextArea } from '../components/ListTextArea';
import {
  ASN_MAX,
  ASN_MIN,
  CLOUD_INFRASTRUCTURE_TYPES,
  DEFAULT_CLOUDVIRT_NAMESPACES,
  DEFAULT_CREATE_PROJECT_DOMAIN,
  DEFAULT_EXTERNAL_EVPN,
  DEFAULT_GATEWAY_BFD,
  DEFAULT_GATEWAY_TRANSPORT,
  DEFAULT_OPENSHIFT_CLUSTER_REF,
  DEFAULT_SITE_DNS,
  DNS_LABEL,
  GATEWAY_TRANSPORTS,
  MTU_MAX,
  MTU_MIN,
  PLACEMENT_BACKEND_KINDS,
  VNI_MAX,
  VNI_MIN,
  WIREGUARD_KEY,
  cidrContains,
  cloudInfrastructureNeedsCredentials,
  isIntInRange,
  isIpv4,
  isIpv4Cidr,
  isPlacementPrefix,
  parseIpv4Cidr,
} from './hybridFabricDefaults';

export const PLATFORM_NAMESPACE = 'sovereign-cloud';

/** Reports the spec to submit, or null while the section is incomplete or invalid. */
export type SpecChange = (spec: Record<string, unknown> | null) => void;

function Help({ children }: { children: React.ReactNode }): React.ReactElement {
  return (
    <p className="sc-text-muted" style={{ marginTop: '0.25rem' }}>
      {children}
    </p>
  );
}

const trim = (v: string) => v.trim();
const optionalInt = (v: string, min: number, max: number) => !v.trim() || isIntInRange(v, min, max);
const nonEmpty = (v: unknown) => typeof v === 'string' && v.trim().length > 0;

function names(items: K8sResource[] | undefined | null): Array<{ value: string; label: string }> {
  return (items ?? [])
    .map((i) => i?.metadata?.name)
    .filter((n): n is string => typeof n === 'string' && n.length > 0)
    .map((n) => ({ value: n, label: n }));
}

/** Report spec changes after render, only when the spec actually changed. */
function useReportSpec(spec: Record<string, unknown> | null, onChange: SpecChange): void {
  const key = JSON.stringify(spec);
  useEffect(() => {
    onChange(spec);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);
}

function rangesOverlap(a: [number, number], b: [number, number]): boolean {
  return !(a[1] < b[0] || b[1] < a[0]);
}

function prefixesOverlap(list: string[]): boolean {
  const parsed = list.map(parseIpv4Cidr).filter((c): c is NonNullable<typeof c> => !!c);
  const spans = parsed.map((c) => {
    const size = 2 ** (32 - c.length);
    const start = c.base - (c.base % size);
    return [start, start + size - 1] as [number, number];
  });
  for (let i = 0; i < spans.length; i += 1) {
    for (let j = i + 1; j < spans.length; j += 1) {
      if (rangesOverlap(spans[i], spans[j])) return true;
    }
  }
  return false;
}

/* ------------------------------------------------------------------ HybridFabric (platform) */

export function HybridFabricCreateFields({ onChange }: { onChange: SpecChange }): React.ReactElement {
  const { t } = useTranslation();
  const entities = useK8sResourceList<K8sResource>('Entity', { namespace: PLATFORM_NAMESPACE });
  const [domainAsn, setDomainAsn] = useState('');
  const [overlayMtu, setOverlayMtu] = useState('');
  const [vniStart, setVniStart] = useState('');
  const [vniEnd, setVniEnd] = useState('');
  const [l2Start, setL2Start] = useState('');
  const [l2End, setL2End] = useState('');
  const [nodes, setNodes] = useState<ObjectListRow[]>([{}]);
  const [credVault, setCredVault] = useState('');
  const [bgpVault, setBgpVault] = useState('');
  const [entityRefs, setEntityRefs] = useState<string[]>([]);

  const asnOk = isIntInRange(domainAsn, ASN_MIN, ASN_MAX);
  const mtuOk = isIntInRange(overlayMtu, MTU_MIN, MTU_MAX);
  const poolOk = (s: string, e: string) =>
    isIntInRange(s, VNI_MIN, VNI_MAX) && isIntInRange(e, VNI_MIN, VNI_MAX) && Number(s) <= Number(e);
  const vniOk = poolOk(vniStart, vniEnd);
  const l2Ok = poolOk(l2Start, l2End);
  const poolsDisjoint =
    !vniOk ||
    !l2Ok ||
    !rangesOverlap([Number(vniStart), Number(vniEnd)], [Number(l2Start), Number(l2End)]);
  const nodeNames = nodes.map((n) => String(n.name ?? ''));
  const nodesOk =
    nodes.length >= 1 &&
    nodes.length <= 8 &&
    nodes.every((n) => DNS_LABEL.test(String(n.name ?? '')) && String(n.name).length <= 63 && nonEmpty(n.address)) &&
    new Set(nodeNames).size === nodeNames.length;

  const valid = asnOk && mtuOk && vniOk && l2Ok && poolsDisjoint && nodesOk && !!credVault.trim();
  const spec = valid
    ? {
        domainAsn: Number(domainAsn),
        overlayMtu: Number(overlayMtu),
        vniPool: { start: Number(vniStart), end: Number(vniEnd) },
        l2vniPool: { start: Number(l2Start), end: Number(l2End) },
        borderGateway: {
          nodes: nodes.map((n) => ({ name: String(n.name).trim(), address: String(n.address).trim() })),
          credentialsRef: { vaultPath: credVault.trim() },
        },
        ...(bgpVault.trim() ? { bgp: { passwordRef: { vaultPath: bgpVault.trim() } } } : {}),
        ...(entityRefs.length ? { entityRefs: entityRefs.map((name) => ({ name })) } : {}),
      }
    : null;
  useReportSpec(spec, onChange);

  return (
    <>
      <FormGroup label={t('fields.domainAsn')} fieldId="hf-asn" isRequired>
        <TextInput
          id="hf-asn"
          type="number"
          value={domainAsn}
          onChange={(_e, v) => setDomainAsn(v)}
          validated={!domainAsn || asnOk ? 'default' : 'error'}
        />
        <Help>{t('fields.domainAsnHelp')}</Help>
      </FormGroup>
      <FormGroup label={t('fields.overlayMtu')} fieldId="hf-mtu" isRequired>
        <TextInput
          id="hf-mtu"
          type="number"
          value={overlayMtu}
          onChange={(_e, v) => setOverlayMtu(v)}
          validated={!overlayMtu || mtuOk ? 'default' : 'error'}
        />
        <Help>{t('fields.overlayMtuHelp')}</Help>
      </FormGroup>
      <FormGroup label={t('fields.vniPool')} fieldId="hf-vni-start" isRequired>
        <div style={{ display: 'flex', gap: '0.5rem' }}>
          <TextInput
            id="hf-vni-start"
            type="number"
            aria-label={t('fields.poolStart')}
            placeholder={t('fields.poolStart')}
            value={vniStart}
            onChange={(_e, v) => setVniStart(v)}
            validated={!vniStart || vniOk ? 'default' : 'error'}
          />
          <TextInput
            id="hf-vni-end"
            type="number"
            aria-label={t('fields.poolEnd')}
            placeholder={t('fields.poolEnd')}
            value={vniEnd}
            onChange={(_e, v) => setVniEnd(v)}
            validated={!vniEnd || vniOk ? 'default' : 'error'}
          />
        </div>
        <Help>{t('fields.vniPoolHelp')}</Help>
      </FormGroup>
      <FormGroup label={t('fields.l2vniPool')} fieldId="hf-l2-start" isRequired>
        <div style={{ display: 'flex', gap: '0.5rem' }}>
          <TextInput
            id="hf-l2-start"
            type="number"
            aria-label={t('fields.poolStart')}
            placeholder={t('fields.poolStart')}
            value={l2Start}
            onChange={(_e, v) => setL2Start(v)}
            validated={!l2Start || (l2Ok && poolsDisjoint) ? 'default' : 'error'}
          />
          <TextInput
            id="hf-l2-end"
            type="number"
            aria-label={t('fields.poolEnd')}
            placeholder={t('fields.poolEnd')}
            value={l2End}
            onChange={(_e, v) => setL2End(v)}
            validated={!l2End || (l2Ok && poolsDisjoint) ? 'default' : 'error'}
          />
        </div>
        <Help>{poolsDisjoint ? t('fields.l2vniPoolHelp') : t('form.poolsOverlap')}</Help>
      </FormGroup>
      <ObjectListEditor
        id="hf-nodes"
        label={t('fields.borderGatewayNodes')}
        isRequired
        maxItems={8}
        keyField="name"
        value={nodes}
        onChange={setNodes}
        addLabel={t('form.addNode')}
        removeLabel={t('form.removeRow')}
        columns={[
          {
            key: 'name',
            label: t('fields.nodeName'),
            validate: (v) => v == null || (DNS_LABEL.test(String(v)) && String(v).length <= 63),
          },
          { key: 'address', label: t('fields.nodeAddress') },
        ]}
        helperText={t('fields.borderGatewayNodesHelp')}
      />
      <FormGroup label={t('fields.borderGatewayCredentialsVaultPath')} fieldId="hf-cred" isRequired>
        <TextInput id="hf-cred" value={credVault} onChange={(_e, v) => setCredVault(v)} />
        <Help>{t('fields.borderGatewayCredentialsHelp')}</Help>
      </FormGroup>
      <FormGroup label={t('fields.bgpPasswordVaultPath')} fieldId="hf-bgp">
        <TextInput id="hf-bgp" value={bgpVault} onChange={(_e, v) => setBgpVault(v)} />
        <Help>{t('fields.fabricBgpPasswordHelp')}</Help>
      </FormGroup>
      <EntityMultiSelect
        id="hf-entities"
        label={t('fields.entityRefs')}
        value={entityRefs}
        onChange={setEntityRefs}
        options={(entities.items ?? []).map((e) => ({
          value: e.metadata.name,
          label: e.metadata.name,
          ready: e.status?.ready,
        }))}
        placeholder={t('form.allEntities')}
      />
      <Help>{t('fields.fabricEntityRefsHelp')}</Help>
    </>
  );
}

/* ------------------------------------------------------------ CloudInfrastructure (platform) */

const SITE_TYPE_LABEL: Record<CloudInfrastructureType, string> = {
  openstack: 'fields.cloudTypeOpenstack',
  openshift: 'fields.cloudTypeOpenshift',
  aws: 'fields.cloudTypeAws',
  external: 'fields.cloudTypeExternal',
};

export function CloudInfrastructureCreateFields({ onChange }: { onChange: SpecChange }): React.ReactElement {
  const { t } = useTranslation();
  const entities = useK8sResourceList<K8sResource>('Entity', { namespace: PLATFORM_NAMESPACE });
  const [type, setType] = useState<CloudInfrastructureType>('openstack');
  const [displayName, setDisplayName] = useState('');
  const [credVault, setCredVault] = useState('');
  const [entityRefs, setEntityRefs] = useState<string[]>([]);
  // openstack
  const [osRegion, setOsRegion] = useState('');
  const [osProjectDomain, setOsProjectDomain] = useState('');
  const [osCreateProjectDomain, setOsCreateProjectDomain] = useState(DEFAULT_CREATE_PROJECT_DOMAIN);
  const [osExternalNetwork, setOsExternalNetwork] = useState('');
  // openshift
  const [clusterRef, setClusterRef] = useState(DEFAULT_OPENSHIFT_CLUSTER_REF);
  const [bootImage, setBootImage] = useState('');
  const [storageClass, setStorageClass] = useState('');
  const [fabricCredVault, setFabricCredVault] = useState('');
  // aws
  const [awsAccount, setAwsAccount] = useState('');
  const [awsRegion, setAwsRegion] = useState('');
  const [awsBaseDomain, setAwsBaseDomain] = useState('');
  // external
  const [evpn, setEvpn] = useState(DEFAULT_EXTERNAL_EVPN);
  // dns (openstack, openshift)
  const [dnsOpen, setDnsOpen] = useState(false);
  const [dnsMode, setDnsMode] = useState<'native' | 'platform'>(DEFAULT_SITE_DNS.mode);
  const [dnsProject, setDnsProject] = useState('');
  const [dnsProjectDomain, setDnsProjectDomain] = useState('');
  const [dnsCreateProject, setDnsCreateProject] = useState(DEFAULT_SITE_DNS.createProject);
  const [dnsZoneQuota, setDnsZoneQuota] = useState(String(DEFAULT_SITE_DNS.zoneQuota));

  const needsCreds = cloudInfrastructureNeedsCredentials(type, clusterRef.trim());
  const showCreds = type !== 'external';
  const hasDns = type === 'openstack' || type === 'openshift';
  const awsAccountOk = /^[0-9]{12}$/.test(awsAccount.trim());
  const zoneQuotaOk = !dnsZoneQuota.trim() || isIntInRange(dnsZoneQuota, 1, Number.MAX_SAFE_INTEGER);

  let valid = !needsCreds || !!credVault.trim();
  if (type === 'aws' && (!awsAccountOk || !awsRegion.trim())) valid = false;
  if (hasDns && dnsMode === 'native' && !dnsProject.trim()) valid = false;
  if (hasDns && !zoneQuotaOk) valid = false;

  const section = (): Record<string, unknown> => {
    const set = (o: Record<string, unknown>, k: string, v: string) => (v.trim() ? { ...o, [k]: v.trim() } : o);
    if (type === 'openstack') {
      let os: Record<string, unknown> = {};
      os = set(os, 'region', osRegion);
      os = set(os, 'projectDomain', osProjectDomain);
      os = set(os, 'externalNetwork', osExternalNetwork);
      if (osCreateProjectDomain !== DEFAULT_CREATE_PROJECT_DOMAIN) os.createProjectDomain = osCreateProjectDomain;
      return { openstack: os };
    }
    if (type === 'openshift') {
      let ocp: Record<string, unknown> = {};
      ocp = set(ocp, 'clusterRef', clusterRef);
      ocp = set(ocp, 'bootImage', bootImage);
      ocp = set(ocp, 'storageClass', storageClass);
      if (fabricCredVault.trim()) ocp.fabricCredentialsRef = { vaultPath: fabricCredVault.trim() };
      return { openshift: ocp };
    }
    if (type === 'aws') {
      return {
        aws: {
          accountId: awsAccount.trim(),
          region: awsRegion.trim(),
          ...(awsBaseDomain.trim() ? { baseDomain: awsBaseDomain.trim() } : {}),
        },
      };
    }
    return { external: { evpn } };
  };

  const dnsSpec = (): Record<string, unknown> | null => {
    if (!hasDns) return null;
    const custom =
      dnsMode !== DEFAULT_SITE_DNS.mode ||
      !!dnsProject.trim() ||
      !!dnsProjectDomain.trim() ||
      dnsCreateProject !== DEFAULT_SITE_DNS.createProject ||
      (dnsZoneQuota.trim() !== '' && Number(dnsZoneQuota) !== DEFAULT_SITE_DNS.zoneQuota);
    if (!custom) return null;
    return {
      mode: dnsMode,
      ...(dnsProject.trim() ? { project: dnsProject.trim() } : {}),
      ...(dnsProjectDomain.trim() ? { projectDomain: dnsProjectDomain.trim() } : {}),
      ...(dnsCreateProject !== DEFAULT_SITE_DNS.createProject ? { createProject: dnsCreateProject } : {}),
      ...(dnsZoneQuota.trim() ? { zoneQuota: Number(dnsZoneQuota) } : {}),
    };
  };

  const dns = dnsSpec();
  const spec = valid
    ? {
        type,
        ...(displayName.trim() ? { displayName: displayName.trim() } : {}),
        ...(showCreds && credVault.trim() ? { credentialsRef: { vaultPath: credVault.trim() } } : {}),
        ...(entityRefs.length ? { entityRefs: entityRefs.map((name) => ({ name })) } : {}),
        ...section(),
        ...(dns ? { dns } : {}),
      }
    : null;
  useReportSpec(spec, onChange);

  return (
    <>
      <FormGroup label={t('fields.cloudInfraType')} fieldId="ci-type" isRequired>
        <FormSelect
          id="ci-type"
          value={type}
          onChange={(_e, v) => setType(v as CloudInfrastructureType)}
          aria-label={t('fields.cloudInfraType')}
        >
          {CLOUD_INFRASTRUCTURE_TYPES.map((ct) => (
            <FormSelectOption key={ct} value={ct} label={t(SITE_TYPE_LABEL[ct])} />
          ))}
        </FormSelect>
        <Help>{t('fields.cloudInfraTypeHelp')}</Help>
      </FormGroup>
      <FormGroup label={t('fields.displayName')} fieldId="ci-display">
        <TextInput id="ci-display" value={displayName} onChange={(_e, v) => setDisplayName(v)} />
      </FormGroup>
      {showCreds && (
        <FormGroup label={t('fields.credentialsVaultPath')} fieldId="ci-cred" isRequired={needsCreds}>
          <TextInput id="ci-cred" value={credVault} onChange={(_e, v) => setCredVault(v)} />
          <Help>{t(`fields.credentialsRefHelp_${type}`)}</Help>
        </FormGroup>
      )}
      <EntityMultiSelect
        id="ci-entities"
        label={t('fields.entityRefs')}
        value={entityRefs}
        onChange={setEntityRefs}
        options={(entities.items ?? []).map((e) => ({
          value: e.metadata.name,
          label: e.metadata.name,
          ready: e.status?.ready,
        }))}
        placeholder={t('form.allEntities')}
      />
      <Help>{t('fields.cloudInfraEntityRefsHelp')}</Help>

      {type === 'openstack' && (
        <>
          <FormGroup label={t('fields.region')} fieldId="ci-os-region">
            <TextInput id="ci-os-region" value={osRegion} onChange={(_e, v) => setOsRegion(v)} />
            <Help>{t('fields.openstackRegionHelp')}</Help>
          </FormGroup>
          <FormGroup label={t('fields.projectDomain')} fieldId="ci-os-pdom">
            <TextInput id="ci-os-pdom" value={osProjectDomain} onChange={(_e, v) => setOsProjectDomain(v)} />
            <Help>{t('fields.siteProjectDomainHelp')}</Help>
          </FormGroup>
          <FormGroup label={t('fields.createProjectDomain')} fieldId="ci-os-create-pdom">
            <Switch
              id="ci-os-create-pdom"
              isChecked={osCreateProjectDomain}
              onChange={(_e, v) => setOsCreateProjectDomain(v)}
              label={osCreateProjectDomain ? t('common.enabled') : t('common.disabled')}
            />
            <Help>{t('fields.createProjectDomainHelp')}</Help>
          </FormGroup>
          <FormGroup label={t('fields.externalNetwork')} fieldId="ci-os-ext">
            <TextInput id="ci-os-ext" value={osExternalNetwork} onChange={(_e, v) => setOsExternalNetwork(v)} />
            <Help>{t('fields.siteExternalNetworkHelp')}</Help>
          </FormGroup>
        </>
      )}

      {type === 'openshift' && (
        <>
          <FormGroup label={t('fields.clusterRef')} fieldId="ci-ocp-cluster">
            <TextInput id="ci-ocp-cluster" value={clusterRef} onChange={(_e, v) => setClusterRef(v)} />
            <Help>{t('fields.clusterRefHelp')}</Help>
          </FormGroup>
          <FormGroup label={t('fields.bootImage')} fieldId="ci-ocp-image">
            <TextInput id="ci-ocp-image" value={bootImage} onChange={(_e, v) => setBootImage(v)} />
            <Help>{t('fields.bootImageHelp')}</Help>
          </FormGroup>
          <FormGroup label={t('fields.storageClass')} fieldId="ci-ocp-sc">
            <TextInput id="ci-ocp-sc" value={storageClass} onChange={(_e, v) => setStorageClass(v)} />
            <Help>{t('fields.siteStorageClassHelp')}</Help>
          </FormGroup>
          <FormGroup label={t('fields.fabricCredentialsVaultPath')} fieldId="ci-ocp-fabric-cred">
            <TextInput id="ci-ocp-fabric-cred" value={fabricCredVault} onChange={(_e, v) => setFabricCredVault(v)} />
            <Help>{t('fields.fabricCredentialsHelp')}</Help>
          </FormGroup>
        </>
      )}

      {type === 'aws' && (
        <>
          <FormGroup label={t('fields.awsAccountId')} fieldId="ci-aws-account" isRequired>
            <TextInput
              id="ci-aws-account"
              value={awsAccount}
              onChange={(_e, v) => setAwsAccount(v)}
              validated={!awsAccount || awsAccountOk ? 'default' : 'error'}
            />
          </FormGroup>
          <FormGroup label={t('fields.region')} fieldId="ci-aws-region" isRequired>
            <TextInput id="ci-aws-region" value={awsRegion} onChange={(_e, v) => setAwsRegion(v)} />
          </FormGroup>
          <FormGroup label={t('fields.baseDomain')} fieldId="ci-aws-base">
            <TextInput id="ci-aws-base" value={awsBaseDomain} onChange={(_e, v) => setAwsBaseDomain(v)} />
            <Help>{t('fields.awsBaseDomainHelp')}</Help>
          </FormGroup>
        </>
      )}

      {type === 'external' && (
        <FormGroup label={t('fields.externalEvpn')} fieldId="ci-ext-evpn">
          <Switch
            id="ci-ext-evpn"
            isChecked={evpn}
            onChange={(_e, v) => setEvpn(v)}
            label={evpn ? t('common.enabled') : t('common.disabled')}
          />
          <Help>{t('fields.externalEvpnHelp')}</Help>
        </FormGroup>
      )}

      {hasDns && (
        <ExpandableSection
          toggleText={t('form.siteDnsSection')}
          isExpanded={dnsOpen}
          onToggle={(_e, open) => setDnsOpen(open)}
        >
          <Help>{t('fields.siteDnsHelp')}</Help>
          <FormGroup label={t('fields.dnsMode')} fieldId="ci-dns-mode">
            <FormSelect
              id="ci-dns-mode"
              value={dnsMode}
              onChange={(_e, v) => setDnsMode(v === 'native' ? 'native' : 'platform')}
              aria-label={t('fields.dnsMode')}
            >
              <FormSelectOption value="platform" label={t('fields.dnsModePlatform')} />
              <FormSelectOption value="native" label={t('fields.dnsModeNative')} />
            </FormSelect>
          </FormGroup>
          {dnsMode === 'native' && (
            <>
              <FormGroup label={t('fields.dnsProject')} fieldId="ci-dns-project" isRequired>
                <TextInput id="ci-dns-project" value={dnsProject} onChange={(_e, v) => setDnsProject(v)} />
                <Help>{t('fields.dnsProjectHelp')}</Help>
              </FormGroup>
              <FormGroup label={t('fields.dnsProjectDomain')} fieldId="ci-dns-pdom">
                <TextInput
                  id="ci-dns-pdom"
                  value={dnsProjectDomain}
                  onChange={(_e, v) => setDnsProjectDomain(v)}
                />
                <Help>{t('fields.dnsProjectDomainHelp')}</Help>
              </FormGroup>
              <FormGroup label={t('fields.dnsCreateProject')} fieldId="ci-dns-create">
                <Switch
                  id="ci-dns-create"
                  isChecked={dnsCreateProject}
                  onChange={(_e, v) => setDnsCreateProject(v)}
                  label={dnsCreateProject ? t('common.enabled') : t('common.disabled')}
                />
              </FormGroup>
              <FormGroup label={t('fields.dnsZoneQuota')} fieldId="ci-dns-quota">
                <TextInput
                  id="ci-dns-quota"
                  type="number"
                  value={dnsZoneQuota}
                  onChange={(_e, v) => setDnsZoneQuota(v)}
                  validated={zoneQuotaOk ? 'default' : 'error'}
                />
                <Help>{t('fields.dnsZoneQuotaHelp')}</Help>
              </FormGroup>
            </>
          )}
        </ExpandableSection>
      )}
    </>
  );
}

/* ------------------------------------------------------------------ CloudGateway (platform) */

export function CloudGatewayCreateFields({ onChange }: { onChange: SpecChange }): React.ReactElement {
  const { t } = useTranslation();
  const fabrics = useK8sResourceList<HybridFabric>('HybridFabric', { namespace: PLATFORM_NAMESPACE });
  const [fabric, setFabric] = useState('');
  const [site, setSite] = useState('');
  const [transport, setTransport] = useState<GatewayTransport>(DEFAULT_GATEWAY_TRANSPORT);
  const [vlan, setVlan] = useState('');
  const [listenPort, setListenPort] = useState('');
  const [peers, setPeers] = useState<ObjectListRow[]>([{}]);
  const [cidr, setCidr] = useState('');
  const [addresses, setAddresses] = useState<ObjectListRow[]>([]);
  const [mtu, setMtu] = useState('');
  const [gateway, setGateway] = useState('');
  const [upstream, setUpstream] = useState<string[]>([]);
  const [bfd, setBfd] = useState(DEFAULT_GATEWAY_BFD);
  const [bgpVault, setBgpVault] = useState('');

  const fabricObj = fabrics.items.find((f) => f.metadata.name === fabric);
  const fabricNodes = (fabricObj?.spec?.borderGateway?.nodes ?? []).map((n) => n.name).filter(Boolean);

  // A new fabric choice proposes one address row per border gateway node (addresses still to fill).
  useEffect(() => {
    if (fabricNodes.length && addresses.every((a) => !a.address)) {
      setAddresses(fabricNodes.map((node) => ({ node })));
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [fabric, fabricNodes.join(',')]);

  const wireguard = transport === 'wireguard';
  const cidrOk = isIpv4Cidr(cidr);
  const vlanOk = wireguard || isIntInRange(vlan, 1, 4094);
  const portOk = !wireguard || optionalInt(listenPort, 1024, 65535);
  const peerOk = (p: ObjectListRow) =>
    WIREGUARD_KEY.test(String(p.publicKey ?? '').trim()) &&
    Array.isArray(p.allowedIPs) &&
    p.allowedIPs.length > 0 &&
    (p.allowedIPs as string[]).every(isIpv4Cidr) &&
    (p.persistentKeepalive == null || (Number(p.persistentKeepalive) >= 0 && Number(p.persistentKeepalive) <= 3600));
  const peersOk = !wireguard || (peers.length >= 1 && peers.every(peerOk));
  const addrNodes = addresses.map((a) => String(a.node ?? ''));
  const addressesOk =
    addresses.length >= 1 &&
    addresses.length <= 8 &&
    addresses.every((a) => nonEmpty(a.node) && isIpv4(String(a.address ?? '')) && (!cidrOk || cidrContains(cidr, String(a.address)))) &&
    new Set(addrNodes).size === addrNodes.length;
  const mtuOk = optionalInt(mtu, MTU_MIN, MTU_MAX);
  const gatewayOk = !gateway.trim() || (isIpv4(gateway) && (!cidrOk || cidrContains(cidr, gateway)));
  const upstreamOk =
    upstream.length <= 4 &&
    upstream.every(isIpv4) &&
    (!cidrOk || !!gateway.trim() || upstream.every((ip) => cidrContains(cidr, ip)));

  const valid =
    !!fabric && !!site && cidrOk && vlanOk && portOk && peersOk && addressesOk && mtuOk && gatewayOk && upstreamOk;

  const spec = valid
    ? {
        fabricRef: { name: fabric },
        cloudRef: { name: site },
        transport,
        ...(wireguard
          ? {
              wireguard: {
                ...(listenPort.trim() ? { listenPort: Number(listenPort) } : {}),
                peers: peers.map((p) => ({
                  publicKey: String(p.publicKey).trim(),
                  ...(nonEmpty(p.endpoint) ? { endpoint: String(p.endpoint).trim() } : {}),
                  allowedIPs: p.allowedIPs as string[],
                  ...(p.persistentKeepalive != null ? { persistentKeepalive: Number(p.persistentKeepalive) } : {}),
                })),
              },
            }
          : {}),
        siteUnderlay: {
          ...(wireguard ? {} : { vlan: Number(vlan) }),
          cidr: cidr.trim(),
          addresses: addresses.map((a) => ({ node: String(a.node).trim(), address: String(a.address).trim() })),
          ...(mtu.trim() ? { mtu: Number(mtu) } : {}),
          ...(gateway.trim() ? { gateway: gateway.trim() } : {}),
          ...(upstream.length ? { upstreamRouteReflectors: upstream } : {}),
        },
        bfd,
        ...(bgpVault.trim() ? { bgp: { passwordRef: { vaultPath: bgpVault.trim() } } } : {}),
      }
    : null;
  useReportSpec(spec, onChange);

  const nodeColumn = fabricNodes.length
    ? {
        key: 'node',
        label: t('fields.borderGatewayNode'),
        type: 'select' as const,
        options: [{ value: '', label: t('form.selectPlaceholder') }, ...fabricNodes.map((n) => ({ value: n, label: n }))],
      }
    : { key: 'node', label: t('fields.borderGatewayNode') };

  return (
    <>
      <FabricSelect
        id="cg-fabric"
        label={t('fields.fabricRef')}
        value={fabric}
        onChange={setFabric}
        options={fabrics.items.map((f) => ({ value: f.metadata.name, label: f.metadata.name, ready: f.status?.ready }))}
        placeholder={t('form.fabricSelectPlaceholder')}
        emptyStateMessage={fabrics.loading ? t('common.loading') : t('form.fabricNone')}
        isRequired
      />
      <Help>{t('fields.gatewayFabricRefHelp')}</Help>
      <CloudInfrastructureSelect
        id="cg-site"
        label={t('fields.site')}
        value={site}
        onChange={setSite}
        types={['openstack', 'openshift', 'external']}
        capability="evpn"
        isRequired
      />
      <Help>{t('fields.gatewayCloudRefHelp')}</Help>
      <FormGroup label={t('fields.transport')} fieldId="cg-transport" isRequired>
        <FormSelect
          id="cg-transport"
          value={transport}
          onChange={(_e, v) => setTransport(v === 'wireguard' ? 'wireguard' : 'none')}
          aria-label={t('fields.transport')}
        >
          {GATEWAY_TRANSPORTS.map((tt) => (
            <FormSelectOption
              key={tt}
              value={tt}
              label={tt === 'none' ? t('fields.transportNone') : t('fields.transportWireguard')}
            />
          ))}
        </FormSelect>
        <Help>{t('fields.gatewayTransportHelp')}</Help>
      </FormGroup>
      {!wireguard && (
        <FormGroup label={t('fields.siteUnderlayVlan')} fieldId="cg-vlan" isRequired>
          <TextInput
            id="cg-vlan"
            type="number"
            value={vlan}
            onChange={(_e, v) => setVlan(v)}
            validated={!vlan || vlanOk ? 'default' : 'error'}
          />
          <Help>{t('fields.siteUnderlayVlanHelp')}</Help>
        </FormGroup>
      )}
      {wireguard && (
        <>
          <FormGroup label={t('fields.wireguardListenPort')} fieldId="cg-wg-port">
            <TextInput
              id="cg-wg-port"
              type="number"
              value={listenPort}
              onChange={(_e, v) => setListenPort(v)}
              validated={portOk ? 'default' : 'error'}
            />
            <Help>{t('fields.wireguardListenPortHelp')}</Help>
          </FormGroup>
          <ObjectListEditor
            id="cg-wg-peers"
            label={t('fields.wireguardPeers')}
            isRequired
            value={peers}
            onChange={setPeers}
            addLabel={t('form.addPeer')}
            removeLabel={t('form.removeRow')}
            columns={[
              {
                key: 'publicKey',
                label: t('fields.wireguardPublicKey'),
                validate: (v) => v == null || WIREGUARD_KEY.test(String(v).trim()),
              },
              { key: 'endpoint', label: t('fields.wireguardEndpoint'), placeholder: t('fields.wireguardEndpointPlaceholder') },
              {
                key: 'allowedIPs',
                label: t('fields.wireguardAllowedIps'),
                type: 'list',
                validate: (v) => v == null || (Array.isArray(v) && v.every((c) => isIpv4Cidr(String(c)))),
              },
              {
                key: 'persistentKeepalive',
                label: t('fields.wireguardKeepalive'),
                type: 'number',
                validate: (v) => v == null || (Number(v) >= 0 && Number(v) <= 3600),
              },
            ]}
            helperText={t('fields.wireguardPeersHelp')}
          />
        </>
      )}
      <FormGroup label={wireguard ? t('fields.tunnelCidr') : t('fields.siteUnderlayCidr')} fieldId="cg-cidr" isRequired>
        <TextInput
          id="cg-cidr"
          value={cidr}
          onChange={(_e, v) => setCidr(v)}
          validated={!cidr || cidrOk ? 'default' : 'error'}
        />
        <Help>{wireguard ? t('fields.tunnelCidrHelp') : t('fields.siteUnderlayCidrHelp')}</Help>
      </FormGroup>
      <ObjectListEditor
        id="cg-addresses"
        label={t('fields.siteUnderlayAddresses')}
        isRequired
        maxItems={8}
        keyField="node"
        value={addresses}
        onChange={setAddresses}
        addLabel={t('form.addAddress')}
        removeLabel={t('form.removeRow')}
        columns={[
          nodeColumn,
          {
            key: 'address',
            label: t('fields.nodeUnderlayAddress'),
            validate: (v) => v == null || (isIpv4(String(v)) && (!cidrOk || cidrContains(cidr, String(v)))),
          },
        ]}
        helperText={t('fields.siteUnderlayAddressesHelp')}
      />
      <FormGroup label={t('fields.siteUnderlayMtu')} fieldId="cg-mtu">
        <TextInput
          id="cg-mtu"
          type="number"
          value={mtu}
          onChange={(_e, v) => setMtu(v)}
          validated={mtuOk ? 'default' : 'error'}
        />
        <Help>{t('fields.siteUnderlayMtuHelp')}</Help>
      </FormGroup>
      <FormGroup label={t('fields.siteUnderlayGateway')} fieldId="cg-gw">
        <TextInput
          id="cg-gw"
          value={gateway}
          onChange={(_e, v) => setGateway(v)}
          validated={gatewayOk ? 'default' : 'error'}
        />
        <Help>{t('fields.siteUnderlayGatewayHelp')}</Help>
      </FormGroup>
      <FormGroup label={t('fields.upstreamRouteReflectors')} fieldId="cg-upstream">
        <ListTextArea
          id="cg-upstream"
          value={upstream}
          onChange={setUpstream}
          validated={upstreamOk ? 'default' : 'error'}
        />
        <Help>{t('fields.upstreamRouteReflectorsHelp')}</Help>
      </FormGroup>
      <FormGroup label={t('fields.bfd')} fieldId="cg-bfd">
        <Switch
          id="cg-bfd"
          isChecked={bfd}
          onChange={(_e, v) => setBfd(v)}
          label={bfd ? t('common.enabled') : t('common.disabled')}
        />
        <Help>{t('fields.bfdHelp')}</Help>
      </FormGroup>
      <FormGroup label={t('fields.bgpPasswordVaultPath')} fieldId="cg-bgp">
        <TextInput id="cg-bgp" value={bgpVault} onChange={(_e, v) => setBgpVault(v)} />
        <Help>{t('fields.gatewayBgpPasswordHelp')}</Help>
      </FormGroup>
    </>
  );
}

/* ------------------------------------------------------------------ HybridNetwork (tenant) */

export function HybridNetworkCreateFields({
  namespace,
  entityName,
  onChange,
}: {
  namespace: string;
  entityName: string;
  onChange: SpecChange;
}): React.ReactElement {
  const { t } = useTranslation();
  const fabrics = useK8sResourceList<HybridFabric>('HybridFabric', { namespace: PLATFORM_NAMESPACE });
  const rbacs = useK8sResourceList<K8sResource>('Rbac', { namespace, enabled: !!namespace });
  const [description, setDescription] = useState('');
  const [fabricRef, setFabricRef] = useState('');
  const [mtu, setMtu] = useState('');
  const [egress, setEgress] = useState(false);
  const [viewers, setViewers] = useState<string[]>([]);

  const fabricsReadable = !fabrics.error;
  const eligible = (f: HybridFabric) => {
    const refs = (f.spec?.entityRefs ?? []).map((r) => r.name);
    return refs.length === 0 || refs.includes(entityName);
  };
  const options = fabrics.items.map((f) => ({
    value: f.metadata.name,
    label: f.metadata.name,
    ready: f.status?.ready,
    isDisabled: !eligible(f),
  }));
  const eligibleFabrics = fabrics.items.filter(eligible);
  const fabricRequired = fabricsReadable && eligibleFabrics.length > 1;
  const effectiveFabric = fabricRef
    ? fabrics.items.find((f) => f.metadata.name === fabricRef)
    : eligibleFabrics.length === 1
      ? eligibleFabrics[0]
      : undefined;
  const fabricMtu = effectiveFabric?.spec?.overlayMtu;
  const mtuMax = typeof fabricMtu === 'number' ? Math.min(fabricMtu, MTU_MAX) : MTU_MAX;
  const mtuOk = optionalInt(mtu, MTU_MIN, mtuMax);

  const valid = mtuOk && (!fabricRequired || !!fabricRef);
  const spec = valid
    ? {
        ...(description.trim() ? { description: description.trim() } : {}),
        ...(fabricRef.trim() ? { fabricRef: { name: fabricRef.trim() } } : {}),
        ...(mtu.trim() ? { mtu: Number(mtu) } : {}),
        egress: { internet: egress },
        ...(viewers.length ? { networkViewerRbac: viewers } : {}),
      }
    : null;
  useReportSpec(spec, onChange);

  return (
    <>
      <FormGroup label={t('fields.description')} fieldId="hn-desc">
        <TextArea id="hn-desc" value={description} onChange={(_e, v) => setDescription(v)} rows={2} />
      </FormGroup>
      {fabricsReadable ? (
        <FabricSelect
          id="hn-fabric"
          label={t('fields.fabricRef')}
          value={fabricRef}
          onChange={setFabricRef}
          options={options}
          isRequired={fabricRequired}
          placeholder={fabricRequired ? t('form.fabricRequiredPlaceholder') : t('form.fabricDefaultPlaceholder')}
          emptyStateMessage={fabrics.loading ? t('common.loading') : t('form.fabricNone')}
        />
      ) : (
        <FormGroup label={t('fields.fabricRef')} fieldId="hn-fabric">
          <TextInput
            id="hn-fabric"
            value={fabricRef}
            onChange={(_e, v) => setFabricRef(v.trim())}
            placeholder={t('form.fabricDefaultPlaceholder')}
          />
        </FormGroup>
      )}
      <Help>{t('fields.networkFabricRefHelp')}</Help>
      <FormGroup label={t('fields.networkMtu')} fieldId="hn-mtu">
        <TextInput
          id="hn-mtu"
          type="number"
          value={mtu}
          onChange={(_e, v) => setMtu(v)}
          placeholder={
            typeof fabricMtu === 'number' ? t('form.mtuDefaultPlaceholder', { mtu: fabricMtu }) : t('form.mtuFabricDefault')
          }
          validated={mtuOk ? 'default' : 'error'}
        />
        <Help>{t('fields.networkMtuHelp')}</Help>
      </FormGroup>
      <FormGroup label={t('fields.egressInternet')} fieldId="hn-egress">
        <Switch
          id="hn-egress"
          isChecked={egress}
          onChange={(_e, v) => setEgress(v)}
          label={egress ? t('common.enabled') : t('common.disabled')}
        />
        <Help>{t('fields.egressInternetHelp')}</Help>
      </FormGroup>
      <RbacMultiSelect
        id="hn-viewers"
        label={t('fields.networkViewerRbac')}
        value={viewers}
        onChange={setViewers}
        options={names(rbacs.items)}
        placeholder={t('form.selectRbac')}
      />
    </>
  );
}

/* ------------------------------------------------------------- NetworkPlacement (tenant) */

export function NetworkPlacementCreateFields({
  namespace,
  onChange,
}: {
  namespace: string;
  onChange: SpecChange;
}): React.ReactElement {
  const { t } = useTranslation();
  const enabled = !!namespace;
  const networks = useK8sResourceList<K8sResource>('HybridNetwork', { namespace, enabled });
  const cloudvirts = useK8sResourceList<K8sResource>('CloudVirt', { namespace, enabled });
  const cloudosos = useK8sResourceList<K8sResource>('CloudOSO', { namespace, enabled });
  const [network, setNetwork] = useState('');
  const [backendKind, setBackendKind] = useState<NetworkPlacementBackendKind>('CloudVirt');
  const [backendName, setBackendName] = useState('');
  const [prefixes, setPrefixes] = useState<string[]>([]);

  const backendOptions = useMemo(
    () =>
      buildBackendOptions({ cloudosos: cloudosos.items, cloudvirts: cloudvirts.items }).filter(
        (o) => o.kind === backendKind,
      ),
    [cloudosos.items, cloudvirts.items, backendKind],
  );

  const prefixesOk =
    prefixes.length >= 1 &&
    prefixes.length <= 16 &&
    prefixes.every(isPlacementPrefix) &&
    new Set(prefixes).size === prefixes.length &&
    !prefixesOverlap(prefixes);
  const valid = !!network && !!backendName && prefixesOk;
  const spec = valid ? { network, backend: { kind: backendKind, name: backendName }, prefixes } : null;
  useReportSpec(spec, onChange);

  return (
    <>
      <FormGroup label={t('fields.network')} fieldId="np-network" isRequired>
        <FormSelect id="np-network" value={network} onChange={(_e, v) => setNetwork(v)} aria-label={t('fields.network')}>
          <FormSelectOption value="" label={networks.loading ? t('common.loading') : t('form.selectPlaceholder')} isDisabled />
          {names(networks.items).map((o) => (
            <FormSelectOption key={o.value} value={o.value} label={o.label} />
          ))}
        </FormSelect>
        <Help>{t('fields.placementNetworkHelp')}</Help>
      </FormGroup>
      <FormGroup label={t('fields.backendKind')} fieldId="np-backend-kind" isRequired>
        <FormSelect
          id="np-backend-kind"
          value={backendKind}
          onChange={(_e, v) => {
            setBackendKind(v === 'CloudOSO' ? 'CloudOSO' : 'CloudVirt');
            setBackendName('');
          }}
          aria-label={t('fields.backendKind')}
        >
          {PLACEMENT_BACKEND_KINDS.map((k) => (
            <FormSelectOption key={k} value={k} label={t(`kinds.${k}`, { defaultValue: k })} />
          ))}
        </FormSelect>
      </FormGroup>
      <BackendSelect
        id="np-backend"
        label={t('fields.backendName')}
        value={backendName ? { kind: backendKind, name: backendName } : null}
        onChange={(b) => setBackendName(b.name)}
        options={backendOptions}
        placeholder={t('form.selectPlaceholder')}
        isRequired
      />
      <Help>{t('fields.placementBackendHelp')}</Help>
      <FormGroup label={t('fields.prefixes')} fieldId="np-prefixes" isRequired>
        <ListTextArea
          id="np-prefixes"
          value={prefixes}
          onChange={setPrefixes}
          validated={prefixes.length === 0 || prefixesOk ? 'default' : 'error'}
        />
        <Help>{t('fields.placementPrefixesHelp')}</Help>
      </FormGroup>
      <Help>{t('form.placementImmutable')}</Help>
    </>
  );
}

/* --------------------------------------------------------------------- CloudVirt (tenant) */

const QUANTITY = /^\d+(\.\d+)?(m|k|Ki|M|Mi|G|Gi|T|Ti|P|Pi|E|Ei)?$/;

export function CloudVirtCreateFields({
  namespace,
  entityName,
  onChange,
}: {
  namespace: string;
  entityName: string;
  onChange: SpecChange;
}): React.ReactElement {
  const { t } = useTranslation();
  const enabled = !!namespace;
  const networks = useK8sResourceList<K8sResource>('HybridNetwork', { namespace, enabled });
  const rbacs = useK8sResourceList<K8sResource>('Rbac', { namespace, enabled });
  const [description, setDescription] = useState('');
  const [site, setSite] = useState('');
  const [siteObj, setSiteObj] = useState<CloudInfrastructure | undefined>();
  const [namespaces, setNamespaces] = useState<ObjectListRow[]>(DEFAULT_CLOUDVIRT_NAMESPACES.map((n) => ({ ...n })));
  const [cpu, setCpu] = useState('');
  const [memory, setMemory] = useState('');
  const [storage, setStorage] = useState('');
  const [vms, setVms] = useState('');
  const [storageClass, setStorageClass] = useState('');
  const [editRbac, setEditRbac] = useState<string[]>([]);
  const [viewRbac, setViewRbac] = useState<string[]>([]);

  const nsNames = namespaces.map((n) => String(n.name ?? ''));
  const nsOk =
    namespaces.length >= 1 &&
    namespaces.length <= 16 &&
    namespaces.every((n) => DNS_LABEL.test(String(n.name ?? '')) && String(n.name).length <= 30) &&
    new Set(nsNames).size === nsNames.length;
  const qOk = (v: string) => !v.trim() || QUANTITY.test(v.trim());
  const vmsOk = optionalInt(vms, 0, Number.MAX_SAFE_INTEGER);
  const valid = !!site.trim() && nsOk && qOk(cpu) && qOk(memory) && qOk(storage) && vmsOk;

  const quota = {
    ...(cpu.trim() ? { cpu: trim(cpu) } : {}),
    ...(memory.trim() ? { memory: trim(memory) } : {}),
    ...(storage.trim() ? { storage: trim(storage) } : {}),
    ...(vms.trim() ? { virtualMachines: Number(vms) } : {}),
  };
  const spec = valid
    ? {
        ...(description.trim() ? { description: description.trim() } : {}),
        cloudRef: { name: site.trim() },
        namespaces: namespaces.map((n) => ({
          name: String(n.name).trim(),
          ...(nonEmpty(n.network) ? { network: String(n.network) } : {}),
        })),
        ...(storageClass.trim() ? { storageClass: storageClass.trim() } : {}),
        ...(Object.keys(quota).length ? { quota } : {}),
        ...(editRbac.length || viewRbac.length
          ? {
              toolRbac: {
                ...(editRbac.length ? { editRbac } : {}),
                ...(viewRbac.length ? { viewRbac } : {}),
              },
            }
          : {}),
      }
    : null;
  useReportSpec(spec, onChange);

  const siteStorageClass = siteObj?.spec?.openshift?.storageClass;
  const rbacOptions = names(rbacs.items);

  return (
    <>
      <FormGroup label={t('fields.description')} fieldId="cv-desc">
        <TextArea id="cv-desc" value={description} onChange={(_e, v) => setDescription(v)} rows={2} />
      </FormGroup>
      <CloudInfrastructureSelect
        id="cv-site"
        label={t('fields.site')}
        value={site}
        onChange={(v) => {
          setSite(v);
          if (!v) setSiteObj(undefined);
        }}
        onSelectInfrastructure={setSiteObj}
        types={['openshift']}
        entityName={entityName}
        capability="virtualization"
        isRequired
      />
      <Help>{t('fields.cloudVirtSiteHelp')}</Help>
      <ObjectListEditor
        id="cv-namespaces"
        label={t('fields.vmNamespaces')}
        isRequired
        maxItems={16}
        keyField="name"
        value={namespaces}
        onChange={setNamespaces}
        addLabel={t('form.addNamespace')}
        removeLabel={t('form.removeRow')}
        columns={[
          {
            key: 'name',
            label: t('fields.namespaceName'),
            validate: (v) => v == null || (DNS_LABEL.test(String(v)) && String(v).length <= 30),
          },
          {
            key: 'network',
            label: t('fields.namespaceNetwork'),
            type: 'select',
            options: [
              { value: '', label: t('form.siteDefaultNetwork') },
              ...names(networks.items),
            ],
          },
        ]}
        helperText={t('fields.vmNamespacesHelp')}
      />
      <FormGroup label={t('fields.quota')} fieldId="cv-quota-cpu">
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(10rem, 1fr))', gap: '0.5rem' }}>
          <TextInput
            id="cv-quota-cpu"
            aria-label={t('fields.quotaCpu')}
            placeholder={t('fields.quotaCpu')}
            value={cpu}
            onChange={(_e, v) => setCpu(v)}
            validated={qOk(cpu) ? 'default' : 'error'}
          />
          <TextInput
            id="cv-quota-memory"
            aria-label={t('fields.quotaMemory')}
            placeholder={t('fields.quotaMemory')}
            value={memory}
            onChange={(_e, v) => setMemory(v)}
            validated={qOk(memory) ? 'default' : 'error'}
          />
          <TextInput
            id="cv-quota-storage"
            aria-label={t('fields.quotaStorage')}
            placeholder={t('fields.quotaStorage')}
            value={storage}
            onChange={(_e, v) => setStorage(v)}
            validated={qOk(storage) ? 'default' : 'error'}
          />
          <TextInput
            id="cv-quota-vms"
            type="number"
            aria-label={t('fields.quotaVirtualMachines')}
            placeholder={t('fields.quotaVirtualMachines')}
            value={vms}
            onChange={(_e, v) => setVms(v)}
            validated={vmsOk ? 'default' : 'error'}
          />
        </div>
        <Help>{t('fields.quotaHelp')}</Help>
      </FormGroup>
      <FormGroup label={t('fields.storageClass')} fieldId="cv-sc">
        <TextInput
          id="cv-sc"
          value={storageClass}
          onChange={(_e, v) => setStorageClass(v)}
          placeholder={siteStorageClass ? t('form.siteDefaultValue', { value: siteStorageClass }) : t('form.siteDefault')}
        />
        <Help>{t('fields.cloudVirtStorageClassHelp')}</Help>
      </FormGroup>
      <RbacMultiSelect
        id="cv-edit-rbac"
        label={t('fields.editRbac')}
        value={editRbac}
        onChange={setEditRbac}
        options={rbacOptions}
        placeholder={t('form.selectRbac')}
      />
      <Help>{t('fields.editRbacHelp')}</Help>
      <RbacMultiSelect
        id="cv-view-rbac"
        label={t('fields.viewRbac')}
        value={viewRbac}
        onChange={setViewRbac}
        options={rbacOptions}
        placeholder={t('form.selectRbac')}
      />
      <Help>{t('fields.viewRbacHelp')}</Help>
    </>
  );
}
