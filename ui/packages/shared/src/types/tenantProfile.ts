/** TenantProfile: read-only summary of a tenant for its own users (one object named "shc" per tenant namespace). */
export const TENANT_PROFILE_NAME = 'shc';

export interface TenantContact {
  name: string;
  email?: string;
  url?: string;
}

export type TenantSiteType = 'openstack' | 'openshift' | 'aws' | 'gcp' | 'external';
export type TenantSiteCapability =
  | 'Virtualization'
  | 'HybridNetworking'
  | 'DNS'
  | 'IPv6'
  | 'IPv6Only'
  | 'IPv6NetworkSource'
  | 'IPv6ProviderSource'
  | 'IPv6Translation';

export interface TenantSiteStorageClass {
  name: string;
  default?: boolean;
}

export interface TenantProfileSite {
  name: string;
  displayName?: string;
  type: TenantSiteType;
  location?: string;
  region?: string;
  billable?: boolean;
  state: 'Available' | 'Unavailable';
  capabilities?: TenantSiteCapability[];
  storageClasses?: TenantSiteStorageClass[];
  /** OpenStack sites: external networks every project may use */
  externalNetworks?: TenantSiteExternalNetwork[];
}

/** An external network of an OpenStack site that every project may use */
export interface TenantSiteExternalNetwork {
  name: string;
  id?: string;
  /** Address families of its subnets */
  families?: number[];
  /** The site's default external network */
  default?: boolean;
}

/** An address range tenants may not use (Refused) or that the platform does not allocate from (SteppedAround) */
export interface TenantReservedRange {
  prefix: string;
  purpose?: string;
  rule?: 'Refused' | 'SteppedAround' | string;
  /** Only on this site */
  site?: string;
}

export interface TenantProfileCondition {
  type: string;
  status: 'True' | 'False' | 'Unknown';
  reason?: string;
  message?: string;
  lastTransitionTime?: string;
  observedGeneration?: number;
}

export interface TenantProfileStatus {
  displayName?: string;
  contacts?: TenantContact[];
  sites?: TenantProfileSite[];
  /** The platform IPv6 blocks the tenant's fabric offers; absent when it offers none (or does not say) */
  ipv6Platform?: { blockLengths?: number[]; defaultBlockLength?: number };
  /** Address ranges reserved by the platform */
  reservedRanges?: TenantReservedRange[];
  /** Ranges a network must lie in to host clusters the platform manages (0.0.0.0/0: any range not reserved);
   *  absent when not offered */
  managedClusterRanges?: string[];
  observedEntityGeneration?: number;
  lastUpdatedTime?: string;
  conditions?: TenantProfileCondition[];
}

export interface TenantProfile {
  apiVersion: string;
  kind: 'TenantProfile';
  metadata: { name: string; namespace: string; uid?: string; resourceVersion?: string; creationTimestamp?: string; labels?: Record<string, string> };
  spec?: Record<string, never>;
  status?: TenantProfileStatus;
}
