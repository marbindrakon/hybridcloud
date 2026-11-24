/**
 * The rule for every name a person chooses that reaches a platform (pure, no
 * React): a letter first, then lower-case letters, digits and single hyphens,
 * a letter or digit last, at most 30 characters (a gateway's name 21). On a
 * Google Cloud cluster the name may not start with "goog" or contain a
 * "google"-like word (the installer refuses it). The API checks the same rule;
 * the job-side copy is eda/common/files/shc_names.py (a test compares them).
 */
import type { HybridSovereignKind } from '../types';

export const NAME_PATTERN = '^[a-z]([a-z0-9-]*[a-z0-9])?$';
export const NAME_MAX = 30;
export const GATEWAY_NAME_MAX = 21;

const NAME_RE = new RegExp(NAME_PATTERN);
/** The installer's rule for a cluster on Google Cloud (pkg/validate/validate.go, GCPClusterName). */
const GOOGLE_START = /^goog/;
const GOOGLE_LIKE = /g[o0]{2}gle/;

export interface NameRuleOptions {
  /** Longest name allowed (default NAME_MAX). */
  limit?: number;
  /** A cluster on Google Cloud: also no "goog" start and no "google"-like word. */
  google?: boolean;
}

/** The longest name of a kind. */
export function nameLimitFor(kind: HybridSovereignKind | string): number {
  return kind === 'CloudGateway' ? GATEWAY_NAME_MAX : NAME_MAX;
}

/** True when the name obeys the rule. */
export function nameAllowed(name: string, opts: NameRuleOptions = {}): boolean {
  const limit = opts.limit ?? NAME_MAX;
  if (!name || name.length > limit || !NAME_RE.test(name) || name.includes('--')) return false;
  if (opts.google && (GOOGLE_START.test(name) || GOOGLE_LIKE.test(name))) return false;
  return true;
}

/** The i18n key and values of the one sentence that says the rule (help text and error alike). */
export function nameRuleMessage(opts: NameRuleOptions = {}): { key: string; values: { max: number } } {
  return { key: opts.google ? 'form.nameRule.ruleGoogle' : 'form.nameRule.rule', values: { max: opts.limit ?? NAME_MAX } };
}

function cleanPart(s: string): string {
  return s
    .toLowerCase()
    .replace(/[^a-z0-9-]/g, '-')
    .replace(/-{2,}/g, '-')
    .replace(/^-+|-+$/g, '');
}

/**
 * The name a placement dialog proposes: <network>-<environment> when it obeys
 * the rule, else both parts cut so that the name fits (the longer part gives up
 * more). The person may change it; the API refuses a name that is taken.
 */
export function defaultPlacementName(network: string, environment: string, limit: number = NAME_MAX): string {
  const n = cleanPart(network);
  const e = cleanPart(environment);
  if (!n || !e) return (n || e).slice(0, limit).replace(/-+$/, '');
  if (n.length + 1 + e.length <= limit) return `${n}-${e}`;
  const room = limit - 1;
  let eLen = Math.min(e.length, Math.floor(room / 2));
  const nLen = Math.min(n.length, room - eLen);
  eLen = Math.min(e.length, room - nLen);
  const a = n.slice(0, nLen).replace(/-+$/, '');
  const b = e.slice(0, eLen).replace(/-+$/, '');
  return `${a}-${b}`;
}
