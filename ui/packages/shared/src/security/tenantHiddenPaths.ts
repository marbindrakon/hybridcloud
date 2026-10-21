import type { HybridSovereignKind } from '../types/crds';

/**
 * Paths never shown on tenant pages: platform credentials, Vault path references and secret references (spec and
 * status). The tenant dashboard server already removes them for CloudOSO and PlatformOpenshift
 * (shared/server/tenantSafe.js); the console plugins read the API directly, so their views hide the same paths.
 */
export const TENANT_HIDDEN_PATHS: Partial<Record<HybridSovereignKind, string[]>> = {
  CloudOSO: [
    'spec.vaultPath',
    'spec.route53VaultPath',
    'status.accountVaultPath',
    'status.route53VaultPath',
    'status.vaultPaths',
    'status.dns.dnsConfigSecret',
  ],
  PlatformOpenshift: [
    'spec.openstack.pullSecretVaultPath',
    'spec.openstack.pullSecretVaultProperty',
    'spec.openstack.sshKeyVaultPath',
    'status.adminPasswordVaultPath',
    'status.kubeconfigVaultPath',
    'status.secretsVaultPath',
    'status.vaultPaths',
    'status.dns.accountVaultPath',
  ],
  CloudInfrastructure: ['spec.credentialsRef', 'spec.openshift.fabricCredentialsRef'],
  HybridFabric: ['spec.borderGateway.credentialsRef', 'spec.bgp'],
  CloudGateway: ['spec.bgp'],
  TransportLink: ['spec.vaultConfigRef'],
};

/** Status keys that are never shown to tenants on any kind, whatever their path. */
export const TENANT_SECRET_KEY_RE = /VaultPath|VaultProperty|vaultPaths|Secret$/;
