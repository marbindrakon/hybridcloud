/**
 * How long things usually take, per kind, as i18n keys with English defaults.
 * The values are queue-honest ranges measured on the platform (design section 6);
 * they are shown next to the phase while something is in flight, never as a
 * promise.
 */
import type { Phase } from './phase';

export interface UsualDuration {
  key: string;
  defaultValue: string;
}

const CREATE: Record<string, UsualDuration> = {
  Entity: { key: 'durations.entity', defaultValue: 'Usually 3–5 minutes.' },
  HybridNetwork: { key: 'durations.network', defaultValue: 'Usually under a minute.' },
  NetworkPlacement: {
    key: 'durations.placement',
    defaultValue: "Usually 5–35 minutes; longer for a site's first network.",
  },
  CloudVirt: { key: 'durations.environment', defaultValue: 'Usually 2–10 minutes.' },
  CloudOSO: { key: 'durations.environment', defaultValue: 'Usually 2–10 minutes.' },
  CloudAWS: { key: 'durations.publicCloud', defaultValue: 'Usually 4–10 minutes the first time.' },
  CloudGCP: { key: 'durations.publicCloud', defaultValue: 'Usually 4–10 minutes the first time.' },
  PlatformOpenshift: { key: 'durations.cluster', defaultValue: 'Usually about an hour.' },
  Rbac: { key: 'durations.queueCycle', defaultValue: 'Usually within one queue cycle (a minute or two).' },
  Team: { key: 'durations.queueCycle', defaultValue: 'Usually within one queue cycle (a minute or two).' },
  Project: { key: 'durations.queueCycle', defaultValue: 'Usually within one queue cycle (a minute or two).' },
  Assignment: { key: 'durations.queueCycle', defaultValue: 'Usually within one queue cycle (a minute or two).' },
  VaultKV: { key: 'durations.queueCycle', defaultValue: 'Usually within one queue cycle (a minute or two).' },
  AAPOrg: { key: 'durations.queueCycle', defaultValue: 'Usually within one queue cycle (a minute or two).' },
  QuayOrg: { key: 'durations.queueCycle', defaultValue: 'Usually within one queue cycle (a minute or two).' },
};

const DELETE: Record<string, UsualDuration> = {
  HybridNetwork: {
    key: 'durations.networkDelete',
    defaultValue: 'Usually 15–35 minutes after its network placements are gone.',
  },
  NetworkPlacement: { key: 'durations.placementDelete', defaultValue: 'Usually 5–20 minutes.' },
  Entity: { key: 'durations.entityDelete', defaultValue: 'Takes as long as deleting everything the tenant holds.' },
};

/** The usual duration text for an object in this phase, or undefined. */
export function usualDuration(kind: string, phase: Phase): UsualDuration | undefined {
  if (phase === 'Deleting') return DELETE[kind];
  if (phase === 'Creating' || phase === 'Updating' || phase === 'Waiting') return CREATE[kind];
  return undefined;
}

/** The usual create duration of a kind (Review panels). */
export function usualCreateDuration(kind: string): UsualDuration | undefined {
  return CREATE[kind];
}
