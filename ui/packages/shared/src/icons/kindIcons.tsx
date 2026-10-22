import React from 'react';
import {
  BuildingIcon,
  UsersIcon,
  FolderOpenIcon,
  ClusterIcon,
  CloudIcon,
  AwsIcon,
  GoogleIcon,
  ProjectDiagramIcon,
  UserEditIcon,
  LockIcon,
  SecurityIcon,
  KeyIcon,
  ProcessAutomationIcon,
  BundleIcon,
  MigrationIcon,
  CogIcon,
  GlobeIcon,
  TachometerAltIcon,
  CatalogIcon,
  ServerIcon,
  LayerGroupIcon,
  TopologyIcon,
  InfrastructureIcon,
} from '@patternfly/react-icons';
import { HybridSovereignKind } from '../types';
import { useTranslation } from '../i18n';

export interface KindVisual {
  icon: React.ComponentType<{ className?: string }>;
  label: string;
}

/** Distinctive icon per kind, no accent backgrounds; color is inherited. Labels are the display terms (fallback when no translation). */
export const KIND_VISUALS: Record<string, KindVisual> = {
  Overview: { icon: TachometerAltIcon, label: 'Overview' },
  Entity: { icon: BuildingIcon, label: 'Tenant' },
  Team: { icon: UsersIcon, label: 'Team setup' },
  Project: { icon: FolderOpenIcon, label: 'App project' },
  PlatformOpenshift: { icon: ClusterIcon, label: 'OpenShift cluster' },
  CloudOSO: { icon: LayerGroupIcon, label: 'OpenStack environment' },
  CloudAWS: { icon: AwsIcon, label: 'AWS environment' },
  CloudGCP: { icon: GoogleIcon, label: 'Google Cloud environment' },
  CloudVirt: { icon: ServerIcon, label: 'OpenShift Virtualization environment' },
  Cloud: { icon: CloudIcon, label: 'Cloud' },
  Assignment: { icon: ProjectDiagramIcon, label: 'Cluster assignment' },
  Persona: { icon: UserEditIcon, label: 'Identity role (deprecated)' },
  Rbac: { icon: LockIcon, label: 'Access group' },
  Vault: { icon: SecurityIcon, label: 'Vault' },
  VaultKV: { icon: KeyIcon, label: 'KV secrets engine' },
  AAPOrg: { icon: ProcessAutomationIcon, label: 'AAP organization' },
  QuayOrg: { icon: BundleIcon, label: 'Quay organization' },
  OpenStackMigration: { icon: MigrationIcon, label: 'VM migration to OpenStack' },
  AAPConfig: { icon: GlobeIcon, label: 'Automation connection' },
  RbacConfig: { icon: CogIcon, label: 'Identity provider connection' },
  QuayConfig: { icon: CatalogIcon, label: 'Registry connection' },
  CloudInfrastructure: { icon: InfrastructureIcon, label: 'Site' },
  HybridFabric: { icon: TopologyIcon, label: 'Fabric' },
  CloudGateway: { icon: GlobeIcon, label: 'Cloud Gateway' },
  TransportLink: { icon: ProjectDiagramIcon, label: 'Transport link' },
  HybridNetwork: { icon: TopologyIcon, label: 'Network' },
  NetworkPlacement: { icon: LayerGroupIcon, label: 'Network placement' },
  UIHealthChecker: { icon: TachometerAltIcon, label: 'Endpoint health check' },
  Server: { icon: ServerIcon, label: 'Server' },
};

export function getKindVisual(kind: HybridSovereignKind | string): KindVisual {
  return KIND_VISUALS[kind] ?? { icon: CatalogIcon, label: String(kind) };
}

export interface KindIconProps {
  kind: HybridSovereignKind | string;
  /** @deprecated Ignored — icons render without background tiles */
  tiled?: boolean;
  size?: 'sm' | 'md' | 'lg';
  className?: string;
  title?: string;
}

/** Kind-aware icon without background color (inherits text/link color). */
export function KindIcon({
  kind,
  size = 'md',
  className = '',
  title,
}: KindIconProps): React.ReactElement {
  const { t } = useTranslation();
  const visual = getKindVisual(kind);
  const label = t(`kindsOne.${kind}`, { defaultValue: visual.label });
  const Icon = visual.icon;
  const classes = ['sc-kind-icon', `sc-kind-icon--${size}`, className].filter(Boolean).join(' ');

  return (
    <span className={classes} title={title ?? label} aria-hidden={!title}>
      <Icon />
    </span>
  );
}
