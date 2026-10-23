/**
 * Display vocabulary for labels built at runtime (U-1..U-3, PGT-F2).
 *
 * The words come from the string catalogues (kinds.<Kind>, kindsOne.<Kind>,
 * kindsDense.<Kind>, fieldWords.<word>), which follow docs/content/terms.yaml;
 * scripts/check-content.py checks the catalogue terms against terms.yaml and
 * ui/scripts/tests/vocabulary.test.mjs checks what these formatters produce
 * against its banned words. Anything that puts an API kind, an object
 * reference ("Kind/name") or a raw field key on a page goes through here:
 * breadcrumbs, titles, table cells, map captions, toasts and field lists.
 *
 * Pure module (no React): `t` is any i18next-like translate function.
 */

/** The translate function the formatters need (i18next's TFunction fits). */
export type VocabT = (key: string, options?: Record<string, unknown>) => string;

/** Every API kind of the product (group hybridsovereign.redhat), for recognising kind words in text. */
export const API_KINDS = [
  'Entity',
  'TenantProfile',
  'Team',
  'Assignment',
  'Project',
  'Persona',
  'PlatformOpenshift',
  'CloudOSO',
  'CloudAWS',
  'CloudGCP',
  'CloudVirt',
  'OpenStackMigration',
  'Rbac',
  'RbacConfig',
  'AAPOrg',
  'AAPConfig',
  'QuayOrg',
  'QuayConfig',
  'DNSConfig',
  'Vault',
  'VaultKV',
  'CloudInfrastructure',
  'HybridFabric',
  'CloudGateway',
  'ProviderEdge',
  'AttachmentCircuit',
  'TransportLink',
  'HybridNetwork',
  'NetworkPlacement',
  'UIHealthChecker',
  'Iaac',
] as const;

const KIND_SET = new Set<string>(API_KINDS);

/**
 * Kinds of other API groups the status text names (the VMs a VM environment's delete waits for).
 * Their words are vocabulary.otherKinds.<Kind> (one) and vocabulary.otherKinds.<Kind>s (many).
 */
export const OTHER_KINDS = ['VirtualMachine'] as const;
const OTHER_SET = new Set<string>(OTHER_KINDS);
const isKnownKind = (word: string): boolean => KIND_SET.has(word) || OTHER_SET.has(word);
/** Kinds with more than one word in their name, longest first (for keys that start with one). */
const LEADING_KINDS = [...API_KINDS].filter((k) => /^[A-Z][a-z]+[A-Z]/.test(k)).sort((a, b) => b.length - a.length);

export function isApiKind(word: string | undefined | null): boolean {
  return !!word && KIND_SET.has(word);
}

export type KindForm = 'one' | 'many' | 'dense';

/**
 * The display term of a kind, sentence case ("Network placement", "Networks").
 * dense: the shorter term for dense tables and map captions when one exists
 * ("VM environment"), else the singular. An unknown kind gives the generic
 * word "Object", never the API word.
 */
export function kindTerm(t: VocabT, kind: string | undefined | null, form: KindForm = 'one'): string {
  if (kind && OTHER_SET.has(kind)) {
    const other = t(`vocabulary.otherKinds.${kind}${form === 'many' ? 's' : ''}`, { defaultValue: '' });
    if (other) return other;
  }
  if (!kind || !isApiKind(kind)) return t(form === 'many' ? 'vocabulary.objects' : 'vocabulary.object');
  const one = t(`kindsOne.${kind}`, { defaultValue: '' });
  const fallback = one || t(`kinds.${kind}`, { defaultValue: '' }) || t('vocabulary.object');
  if (form === 'many') return t(`kinds.${kind}`, { defaultValue: '' }) || fallback;
  if (form === 'dense') return t(`kindsDense.${kind}`, { defaultValue: '' }) || fallback;
  return fallback;
}

/** Leading words that stay capitalised in running text (product and brand names). */
const PROPER_WORDS = new Set(['Google', 'Vault', 'Vaults', 'Quay', 'OpenShift', 'OpenStack', 'AWS', 'AAP', 'KV', 'VM', 'Git']);

/**
 * A sentence-case term as written inside a sentence: "Network placement" ->
 * "network placement", "OpenShift cluster" and "Vaults" are kept. Matches the
 * `term` / `plural` of docs/content/terms.yaml for every kind (tested).
 */
export function inText(term: string): string {
  const words = term.split(/(\s+)/);
  const first = words[0] ?? '';
  const plain = (w: string): boolean => !PROPER_WORDS.has(w) && /^[A-ZÀ-Ý][a-zà-ÿ'’-]*$/.test(w);
  if (!first || !plain(first)) return term;
  // "Cloud Infrastructure" -> "cloud infrastructure", not "cloud Infrastructure".
  return words.map((w) => (plain(w) ? w.charAt(0).toLowerCase() + w.slice(1) : w)).join('');
}

/**
 * The display term for use inside a sentence ("No networks match …"). A kind
 * whose label is a platform word has a running-text word for the tenant pages
 * under vocabulary.inText.<Kind> / <Kind>s (CloudGateway: "site connection").
 */
export function kindTermInText(t: VocabT, kind: string | undefined | null, form: KindForm = 'one'): string {
  if (kind && isApiKind(kind)) {
    const own = t(`vocabulary.inText.${kind}${form === 'many' ? 's' : ''}`, { defaultValue: '' });
    if (own) return own;
  }
  return inText(kindTerm(t, kind, form));
}

export interface ObjectRef {
  kind?: string;
  name: string;
  namespace?: string;
  /** What the reference is about, as the API adds it: "(internet egress)", ": in use". */
  detail?: string;
}

/**
 * An object reference as the API writes it: "Kind/name", "Kind / name",
 * "Kind namespace/name" or "Kind name", optionally followed by a detail in
 * brackets or after a colon ("HybridFabric ns/fab (internet egress)",
 * "CloudGateway ns/gw (VRF x)"). Returns null when the text does not start with
 * a known kind (an API kind, or one of OTHER_KINDS).
 */
export function parseObjectRef(text: string | undefined | null): ObjectRef | null {
  if (!text) return null;
  const m = /^\s*([A-Z][A-Za-z]+)\s*(?:\/\s*|\s+)([^\s():]+)\s*(?:\((.*)\)|:\s*(.*?))?\s*$/.exec(text);
  if (!m || !isKnownKind(m[1])) return null;
  const rest = m[2];
  const detail = (m[3] ?? m[4] ?? '').trim();
  const slash = rest.indexOf('/');
  const ref: ObjectRef =
    slash > 0 ? { kind: m[1], namespace: rest.slice(0, slash), name: rest.slice(slash + 1) } : { kind: m[1], name: rest };
  if (detail) ref.detail = detail;
  return ref;
}

/**
 * "VM environment g-vmenv-1" (dense) or "OpenShift Virtualization environment g-vmenv-1",
 * with the detail in brackets. A reference without a kind (free text) is shown through
 * displayText, so a kind word inside it still reads as its term.
 */
export function objectLabel(
  t: VocabT,
  ref: ObjectRef | string | undefined | null,
  opts: { dense?: boolean } = {},
): string {
  if (!ref) return '';
  const r = typeof ref === 'string' ? parseObjectRef(ref) ?? { name: ref } : ref;
  const name = r.namespace ? `${r.namespace}/${r.name}` : r.name;
  const detail = r.detail ? ` (${displayText(t, r.detail)})` : '';
  if (!r.kind) return `${displayText(t, name)}${detail}`;
  return `${kindTerm(t, r.kind, opts.dense ? 'dense' : 'one')} ${name}`.trim() + detail;
}

const KIND_WORD = new RegExp(
  `\\b(${[...API_KINDS, ...OTHER_KINDS].sort((a, b) => b.length - a.length).join('|')})(s)?\\b`,
  'g',
);

/**
 * Free text from the API (condition messages, waitingFor entries, summaries)
 * with every API kind word replaced by its display term: "waits for
 * CloudVirt/g-vmenv-1" -> "waits for VM environment g-vmenv-1",
 * "network a via NetworkPlacement b" -> "network a via network placement b".
 */
export function displayText(t: VocabT, text: string | undefined | null): string {
  if (!text) return '';
  const linked = linkedSummary(t, text);
  if (linked) return linked;
  return text
    .replace(/\b([A-Z][A-Za-z]+)\s*\/\s*([a-z0-9][a-z0-9.-]*(?:\/[a-z0-9][a-z0-9.-]*)?)/g, (all, kind: string, name: string) =>
      isKnownKind(kind) ? `${inText(kindTerm(t, kind, 'dense'))} ${name}` : all,
    )
    .replace(KIND_WORD, (_all, kind: string, plural?: string) => kindTermInText(t, kind, plural ? 'many' : 'one'));
}

/** The cluster assignment's summary as its automation writes it: Team=a(ready=True), Projects=1, Openshift=b(ready=False). */
const LINKED_SUMMARY = /^\s*Team=([^\s(),]*)\(ready=(True|False)\),\s*Projects=(\d+),\s*Openshift=([^\s(),]*)\(ready=(True|False)\)\s*$/;

/**
 * That summary as a sentence: "Team setup platform is ready; 1 app project;
 * no OpenShift cluster is named yet." Null for any other text.
 */
export function linkedSummary(t: VocabT, text: string): string | null {
  const m = LINKED_SUMMARY.exec(text);
  if (!m) return null;
  const [, team, teamReady, projects, cluster, clusterReady] = m;
  const parts = [
    team
      ? t(teamReady === 'True' ? 'vocabulary.linked.teamReady' : 'vocabulary.linked.teamNotReady', { name: team })
      : t('vocabulary.linked.teamNone'),
    t(Number(projects) === 1 ? 'vocabulary.linked.projectsOne' : 'vocabulary.linked.projectsMany', { count: Number(projects) }),
    cluster
      ? t(clusterReady === 'True' ? 'vocabulary.linked.clusterReady' : 'vocabulary.linked.clusterNotReady', { name: cluster })
      : t('vocabulary.linked.clusterNone'),
  ];
  return sentence(`${parts.join('; ')}.`);
}

/** Words of field keys shown as-is in upper case. */
const ACRONYMS = new Set([
  'id', 'ids', 'mtu', 'url', 'urls', 'dns', 'vni', 'vnis', 'cidr', 'ip', 'ips', 'ipv4', 'ipv6', 'asn', 'aap', 'vm', 'vms',
  'kv', 'uid', 'api', 'bgp', 'vlan', 'tls', 'ca', 'nat', 'evpn', 'oidc', 'sso', 'ssh', 'cpu', 'gpu', 'vrf', 'ttl', 'http',
  'https', 'vpc', 'aws', 'gcp', 'uri',
]);

/**
 * A field key or dotted path as a label: "billingId" -> "Billing ID",
 * "attributes.entity" -> "Attributes · Tenant", "features.argo" ->
 * "Features · Argo". A whole word found under fieldWords.<word> in the
 * catalogue is replaced by it (entity -> tenant, rbac -> access group,
 * backend -> environment). A key that is already a label (has a space or
 * starts upper case) is kept.
 */
export function fieldLabel(t: VocabT, path: string): string {
  if (!path) return '';
  if (/\s/.test(path) || /^[A-ZÀ-Ý]/.test(path)) return displayText(t, path);
  return path
    .split('.')
    .filter(Boolean)
    .map((seg) => fieldSegment(t, seg))
    .join(' · ');
}

function fieldSegment(t: VocabT, seg: string): string {
  const clean = seg.replace(/\[\d+\]/g, '');
  const whole = t(`fieldWords.${clean}`, { defaultValue: '' });
  if (whole) return sentence(whole);
  const asKind = clean.charAt(0).toUpperCase() + clean.slice(1);
  if (KIND_SET.has(asKind)) return kindTerm(t, asKind);
  // cloudVirtRef -> "VM environment ref": a kind at the start of the key is named by its term.
  const lead = LEADING_KINDS.find((k) => asKind.startsWith(k) && /^[A-Z]/.test(asKind.slice(k.length)));
  if (lead) return sentence([kindTermInText(t, lead, 'dense'), ...keyWords(t, asKind.slice(lead.length))].join(' '));
  return sentence(keyWords(t, clean).join(' '));
}

/** The words of a key: camelCase, snake_case and kebab-case split; fieldWords and acronyms applied. */
function keyWords(t: VocabT, key: string): string[] {
  return key
    .replace(/[_-]+/g, ' ')
    .replace(/([a-z0-9])([A-Z])/g, '$1 $2')
    .replace(/([A-Z]+)([A-Z][a-z])/g, '$1 $2')
    .split(/\s+/)
    .filter(Boolean)
    .map((w) => {
      const lower = w.toLowerCase();
      const mapped = t(`fieldWords.${lower}`, { defaultValue: '' });
      if (mapped) return mapped;
      if (ACRONYMS.has(lower)) return lower.toUpperCase();
      return lower;
    });
}

function sentence(s: string): string {
  return s ? s.charAt(0).toUpperCase() + s.slice(1) : s;
}

/** "Tenant · <display name>" (map group, scope captions). */
export function tenantCaption(t: VocabT, tenant: string): string {
  return t('vocabulary.tenantCaption', { tenant });
}
