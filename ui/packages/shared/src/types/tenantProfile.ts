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
