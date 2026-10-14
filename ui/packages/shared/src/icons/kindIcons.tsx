import React from 'react';
import {
  BuildingIcon,
  UsersIcon,
  FolderOpenIcon,
  ClusterIcon,
  CloudIcon,
  AwsIcon,
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

export interface KindVisual {
  icon: React.ComponentType<{ className?: string }>;
  label: string;
}

/** Distinctive icon per CR kind — no accent backgrounds; color is inherited. */
export const KIND_VISUALS: Record<string, KindVisual> = {
  Overview: { icon: TachometerAltIcon, label: 'Overview' },
  Entity: { icon: BuildingIcon, label: 'Entity' },
  Team: { icon: UsersIcon, label: 'Team' },
  Project: { icon: FolderOpenIcon, label: 'Project' },
  PlatformOpenshift: { icon: ClusterIcon, label: 'Platform Openshift' },
  CloudOSO: { icon: LayerGroupIcon, label: 'Cloud OSO' },
  CloudAWS: { icon: AwsIcon, label: 'Cloud AWS' },
  CloudVirt: { icon: ServerIcon, label: 'Cloud Virt' },
  Cloud: { icon: CloudIcon, label: 'Cloud' },
  Assignment: { icon: ProjectDiagramIcon, label: 'Assignment' },
  Persona: { icon: UserEditIcon, label: 'Persona' },
  Rbac: { icon: LockIcon, label: 'RBAC' },
  Vault: { icon: SecurityIcon, label: 'Vault' },
  VaultKV: { icon: KeyIcon, label: 'Vault KV' },
  AAPOrg: { icon: ProcessAutomationIcon, label: 'AAP Org' },
  QuayOrg: { icon: BundleIcon, label: 'Quay Org' },
  OpenStackMigration: { icon: MigrationIcon, label: 'Migration' },
  AAPConfig: { icon: GlobeIcon, label: 'Service URL' },
  RbacConfig: { icon: CogIcon, label: 'Operator' },
  QuayConfig: { icon: CatalogIcon, label: 'Quay Config' },
  CloudInfrastructure: { icon: InfrastructureIcon, label: 'Cloud Infrastructure' },
  HybridFabric: { icon: TopologyIcon, label: 'Hybrid Fabric' },
  CloudGateway: { icon: GlobeIcon, label: 'Cloud Gateway' },
  TransportLink: { icon: ProjectDiagramIcon, label: 'Transport Link' },
  HybridNetwork: { icon: TopologyIcon, label: 'Hybrid Network' },
  NetworkPlacement: { icon: LayerGroupIcon, label: 'Network Placement' },
  UIHealthChecker: { icon: TachometerAltIcon, label: 'UI Health' },
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
  const visual = getKindVisual(kind);
  const Icon = visual.icon;
  const classes = ['sc-kind-icon', `sc-kind-icon--${size}`, className].filter(Boolean).join(' ');

  return (
    <span className={classes} title={title ?? visual.label} aria-hidden={!title}>
      <Icon />
    </span>
  );
}
