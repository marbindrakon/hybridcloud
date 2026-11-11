/**
 * The two uses of a network placed on an OpenStack environment: virtual machines (default on) and
 * router attachments (default off). Other environment kinds take the defaults and send nothing.
 */
import type { NetworkPlacement, NetworkPlacementBackendKind, PlacementUse } from '../types';

export const PLACEMENT_USE_DEFAULTS: Required<PlacementUse> = { virtualMachines: true, routerAttachments: false };

/** Only OpenStack environments offer the settings. */
export function placementUseOffered(kind: NetworkPlacementBackendKind | string | undefined): boolean {
  return kind === 'CloudOSO';
}

/** The use in effect (spec, else the defaults). */
export function placementUseOf(p: Pick<NetworkPlacement, 'spec'> | undefined): Required<PlacementUse> {
  const use = p?.spec?.use ?? {};
  return {
    virtualMachines: typeof use.virtualMachines === 'boolean' ? use.virtualMachines : PLACEMENT_USE_DEFAULTS.virtualMachines,
    routerAttachments: typeof use.routerAttachments === 'boolean' ? use.routerAttachments : PLACEMENT_USE_DEFAULTS.routerAttachments,
  };
}

/** What a create sends: `use` only on OpenStack and only when it differs from the defaults. */
export function placementUseSpec(kind: NetworkPlacementBackendKind | string | undefined, use: Required<PlacementUse>): { use?: PlacementUse } {
  if (!placementUseOffered(kind)) return {};
  if (use.virtualMachines === PLACEMENT_USE_DEFAULTS.virtualMachines && use.routerAttachments === PLACEMENT_USE_DEFAULTS.routerAttachments) return {};
  return { use: { virtualMachines: use.virtualMachines, routerAttachments: use.routerAttachments } };
}

/** The API refuses a placement with neither use. */
export function placementUseProblem(kind: NetworkPlacementBackendKind | string | undefined, use: Required<PlacementUse>): 'needOne' | null {
  return placementUseOffered(kind) && !use.virtualMachines && !use.routerAttachments ? 'needOne' : null;
}

/** One row of the "Uses" column: the wanted uses and, where it differs, the state the platform reports. */
export interface PlacementUseRow {
  use: 'virtualMachines' | 'routerAttachments';
  wanted: boolean;
  state?: string;
  reason?: string;
  message?: string;
}

export function placementUseRows(p: NetworkPlacement): PlacementUseRow[] {
  if (!placementUseOffered(p.spec?.backend?.kind)) return [];
  const wanted = placementUseOf(p);
  return (['virtualMachines', 'routerAttachments'] as const).map((use) => {
    const st = p.status?.use?.[use];
    return { use, wanted: wanted[use], state: st?.state, reason: st?.reason, message: st?.message };
  });
}
