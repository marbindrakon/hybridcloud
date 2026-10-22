/**
 * Which network an environment can take (GT-F3).
 *
 * An OpenShift Virtualization environment carries a network only into its own
 * namespaces that name it: a placement of a network none of its namespaces
 * names is accepted by the API but can never become ready on the site. The
 * other environment types (OpenStack, AWS, Google Cloud) take any network of
 * the tenant: the placement creates its own subnet there.
 *
 * The namespaces come from the environment's spec (what the platform realizes);
 * when the spec lists none yet, from its status.
 */
import type { K8sResource } from '../types';

export type AttachBlock = 'noNamespace';

interface NamespaceLike {
  name?: string;
  namespace?: string;
  network?: string;
}

function namespacesOf(env: K8sResource): NamespaceLike[] {
  const spec = (env.spec as { namespaces?: NamespaceLike[] } | undefined)?.namespaces;
  if (Array.isArray(spec) && spec.length) return spec;
  const status = (env.status as { namespaces?: NamespaceLike[] } | undefined)?.namespaces;
  return Array.isArray(status) ? status : [];
}

/**
 * The networks an environment can take, or null when it takes any network
 * (or the environment is not known: the API decides then).
 */
export function networksTakenBy(kind: string, env: K8sResource | undefined): Set<string> | null {
  if (kind !== 'CloudVirt' || !env) return null;
  const out = new Set<string>();
  for (const ns of namespacesOf(env)) if (ns && typeof ns.network === 'string' && ns.network) out.add(ns.network);
  return out;
}

/** Why the pair cannot be attached, or null when it can. */
export function attachBlock(kind: string, env: K8sResource | undefined, network: string): AttachBlock | null {
  if (!network) return null;
  const taken = networksTakenBy(kind, env);
  if (!taken) return null;
  return taken.has(network) ? null : 'noNamespace';
}
