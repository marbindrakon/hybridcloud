/**
 * Lab-aligned HybridFabric and CloudInfrastructure defaults.
 * HybridFabric values mirror gitops/apps/platform-fabric/templates/fabrics.yaml (localnet underlay);
 * CloudInfrastructure values mirror the CRD defaults.
 */
import type { CloudInfrastructureType, FabricTransportType } from '../types';

/** Site ↔ border gateway transport. Only none and wireguard are offered. */
export type FabricTunnelType = FabricTransportType;

export const FABRIC_TUNNEL_TYPES: FabricTunnelType[] = ['none', 'wireguard'];

/** How the border gateway's underlay NIC attaches on the hub (HybridFabric.spec.underlay.type). */
export type FabricUnderlayType = 'ovn-layer2' | 'localnet';

/** Hub underlay (HybridFabric.spec.underlay), localnet flavour. */
export interface FabricUnderlayDefaults {
  type: FabricUnderlayType;
  physicalNetworkName: string;
  nadName: string;
  cidr: string;
  hubVtepBlock: string;
  hubLegAddress: string;
  mtu: number;
}

export const DEFAULT_FABRIC_UNDERLAY: FabricUnderlayDefaults = {
  type: 'localnet',
  physicalNetworkName: 'physnet',
  nadName: 'fabric-localnet',
  cidr: '192.168.64.0/18',
  hubVtepBlock: '192.168.65.0/24',
  hubLegAddress: '192.168.65.1/24',
  mtu: 1400,
};

export const DEFAULT_FABRIC_TRANSPORT: { mtu: number; defaultTunnelType: FabricTunnelType } = {
  mtu: 1442,
  defaultTunnelType: 'none',
};

export const DEFAULT_FABRIC_BORDER_GATEWAY = {
  name: 'fabric-bgw',
  loopback: '10.255.10.10',
  wireguardAddress: '10.254.254.1/24',
  wireguardListenPort: 51820,
  cpu: 2,
  memory: '4Gi',
  image: 'docker://quay.io/containerdisks/centos-stream:9',
};

export const DEFAULT_FABRIC_VNI = { start: 51000, end: 52127 };
export const DEFAULT_FABRIC_DOMAIN_ASN = 65010;

export const CLOUD_INFRASTRUCTURE_TYPES: CloudInfrastructureType[] = ['openstack', 'openshift', 'aws'];

/** credentialsRef is required for these types; openshift (clusterRef local) needs none. */
export function cloudInfrastructureNeedsCredentials(type: CloudInfrastructureType | string): boolean {
  return type === 'openstack' || type === 'aws';
}

export const DEFAULT_CLOUD_INFRA_OPENSTACK = {
  region: 'regionOne',
  netConfigRef: 'openstacknetconfig',
  externalNetwork: 'public',
  projectDomain: 'Default',
};

export const DEFAULT_CLOUD_INFRA_OPENSHIFT = {
  clusterRef: 'local',
  bootImage: 'docker://quay.io/containerdisks/centos-stream:9',
  clusterNetworkPool: { cidr: '100.64.0.0/11', blockPrefixLength: 14 },
  serviceNetworkPool: { cidr: '100.96.0.0/11', blockPrefixLength: 16 },
};

export function parseCidrList(text: string): string[] {
  return text
    .split(/[,\n]/)
    .map((s) => s.trim())
    .filter(Boolean);
}
