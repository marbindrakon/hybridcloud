/**
 * What a person needs to use an OpenStack environment, read from the objects the platform writes:
 * the environment's sign-in, access groups and API credentials (CloudOSO status.endpoints and
 * status.access), the access groups the create form fills in (spec.toolRbac), the network on the
 * site of a placement (NetworkPlacement status.siteNetwork) and the cluster's site user
 * (PlatformOpenshift status.credential). Pure functions: the pages render what they return.
 * Objects written before these fields existed give empty answers, never an error.
 */
import type {
  CloudInfrastructure,
  CloudOSOAccessGroup,
  CloudOSOToolRbac,
  EnvironmentEndpoints,
  K8sResource,
  PlacementSiteNetwork,
  PlatformOpenshiftCredential,
  StatusCondition,
  TenantProfileSite,
} from '../types';

type Obj = Record<string, unknown>;

function isObj(v: unknown): v is Obj {
  return !!v && typeof v === 'object' && !Array.isArray(v);
}

function str(v: unknown): string | undefined {
  return typeof v === 'string' && v.trim() ? v : undefined;
}

function conditionOf(item: K8sResource | undefined, type: string): StatusCondition | undefined {
  const list = (item?.status as { conditions?: unknown } | undefined)?.conditions;
  if (!Array.isArray(list)) return undefined;
  return list.find((c): c is StatusCondition => isObj(c) && c.type === type);
}

/* ------------------------------------------------------------------ environment card */

/** One row of "Groups that can use it". */
export interface OsoGroupRow {
  /** The access group's name (the Rbac), else the directory group */
  name: string;
  /** The group in the sign-in directory */
  group?: string;
  access: 'Edit' | 'View';
  /** Granted, or not ready (Waiting, Failed) */
  ready: boolean;
  message?: string;
}

/** status.access.groups as rows; empty for objects without it. */
export function osoAccessGroups(item: K8sResource | undefined): OsoGroupRow[] {
  const access = (item?.status as { access?: unknown } | undefined)?.access;
  const groups = isObj(access) && Array.isArray(access.groups) ? (access.groups as unknown[]) : [];
  const rows: OsoGroupRow[] = [];
  for (const g of groups) {
    if (!isObj(g)) continue;
    const e = g as CloudOSOAccessGroup;
    const name = str(e.rbac) ?? str(e.group);
    if (!name) continue;
    rows.push({
      name,
      group: str(e.group),
      access: e.access === 'View' ? 'View' : 'Edit',
      ready: e.state === 'Granted',
      message: str(e.message),
    });
  }
  return rows;
}

/**
 * The access groups whose removal from the site is not finished (status.access.removing), named as the card names
 * groups: the access group (the part after the tenant), else the directory group.
 */
export function osoRemovingGroups(item: K8sResource | undefined): string[] {
  const access = (item?.status as { access?: unknown } | undefined)?.access;
  const list = isObj(access) && Array.isArray(access.removing) ? (access.removing as unknown[]) : [];
  return list
    .map((g) => str(g))
    .filter((g): g is string => !!g)
    .map((g) => (g.includes('.') ? g.slice(g.indexOf('.') + 1) : g));
}

/** True when the object carries status.access (written by this round's job). */
export function hasAccessStatus(item: K8sResource | undefined): boolean {
  return isObj((item?.status as { access?: unknown } | undefined)?.access);
}

export type OsoSignInState =
  /** People sign in with their platform identity */
  | 'offered'
  /** The site has no sign-in for the platform identity */
  | 'notSetUp'
  /** Nothing published yet (an environment being created) */
  | 'unknown';

/** How people sign in to the environment's dashboard. */
export function osoSignIn(item: K8sResource | undefined): {
  state: OsoSignInState;
  /** IdentityProvider: the console link starts sign-in; Dashboard: choose the entry on the sign-in page */
  start: 'IdentityProvider' | 'Dashboard';
  /** Name of the entry on the dashboard's sign-in page */
  idpName?: string;
} {
  const ep = ((item?.status as { endpoints?: unknown } | undefined)?.endpoints ?? {}) as EnvironmentEndpoints;
  const signIn = isObj(ep.signIn) ? ep.signIn : {};
  const start = signIn.start === 'IdentityProvider' ? 'IdentityProvider' : 'Dashboard';
  const idpName = str(signIn.idpName);
  if (signIn.method === 'TenantSSO') return { state: 'offered', start, idpName };
  const reason = conditionOf(item, 'AccessReady')?.reason;
  if (signIn.method === 'NotAvailable' || reason === 'SignInNotSetUp') return { state: 'notSetUp', start, idpName };
  return { state: 'unknown', start, idpName };
}

/** No access group can use the environment (sign-in is set up, nobody can sign in). */
export function osoNoAccessGroup(item: K8sResource | undefined): boolean {
  if (osoSignIn(item).state !== 'offered') return false;
  // a group still being removed can still sign in until its removal is finished
  if (osoRemovingGroups(item).length) return false;
  if (conditionOf(item, 'AccessReady')?.reason === 'NoAccessGroups') return true;
  if (osoAccessGroups(item).length) return false;
  const ep = ((item?.status as { endpoints?: unknown } | undefined)?.endpoints ?? {}) as EnvironmentEndpoints;
  return !(Array.isArray(ep.signInGroups) && ep.signInGroups.length);
}

/**
 * Directory groups of the people who can sign in: status.endpoints.signInGroups, else the granted
 * groups of status.access.
 */
export function osoSignInGroups(item: K8sResource | undefined): string[] {
  const ep = ((item?.status as { endpoints?: unknown } | undefined)?.endpoints ?? {}) as EnvironmentEndpoints;
  if (Array.isArray(ep.signInGroups) && ep.signInGroups.length) return ep.signInGroups.filter((g) => typeof g === 'string' && g);
  return osoAccessGroups(item)
    .filter((r) => r.ready && r.group)
    .map((r) => r.group as string);
}

/**
 * The reader's sign-in groups among the environment's, named as the access groups are where the
 * environment says which access group a directory group is.
 */
export function osoReaderGroups(item: K8sResource | undefined, readerGroups: string[]): string[] {
  const rows = osoAccessGroups(item);
  const mine = osoSignInGroups(item).filter((g) => readerGroups.includes(g));
  return Array.from(new Set(mine.map((g) => rows.find((r) => r.group === g)?.name ?? g)));
}

/** Which text the card shows about API credentials (null: none). */
export type OsoApiCredentialsText =
  | { form: 'period'; minutes: number }
  | { form: 'unknown' }
  | { form: 'notOffered' }
  | null;

export function osoApiCredentials(item: K8sResource | undefined): OsoApiCredentialsText {
  if (osoSignIn(item).state !== 'offered') return null;
  const ep = ((item?.status as { endpoints?: unknown } | undefined)?.endpoints ?? {}) as EnvironmentEndpoints;
  const ac = ep.apiCredentials;
  if (!isObj(ac)) return null;
  const minutes = typeof ac.validMinutesAfterSignIn === 'number' && Number.isFinite(ac.validMinutesAfterSignIn) ? ac.validMinutesAfterSignIn : undefined;
  if (ac.state === 'NotOffered' || minutes === 0) return { form: 'notOffered' };
  if (ac.state === 'Offered' && minutes !== undefined && minutes > 0) return { form: 'period', minutes };
  return { form: 'unknown' };
}

/** A number of minutes in the largest whole unit: 1440 is 1 day, 480 is 8 hours, 90 is 90 minutes. */
export function periodParts(minutes: number): { unit: 'day' | 'hour' | 'minute'; count: number } {
  const m = Math.max(0, Math.round(minutes));
  if (m > 0 && m % 1440 === 0) return { unit: 'day', count: m / 1440 };
  if (m > 0 && m % 60 === 0) return { unit: 'hour', count: m / 60 };
  return { unit: 'minute', count: m };
}

/** The names and ids of the project and its domain: status.endpoints, else the older status keys. */
export function osoProjectAndDomain(item: K8sResource | undefined): {
  project?: string;
  projectId?: string;
  domain?: string;
  domainId?: string;
} {
  const st = (item?.status ?? {}) as Obj;
  const ep = (isObj(st.endpoints) ? st.endpoints : {}) as EnvironmentEndpoints;
  const project = Array.isArray(ep.project) ? ep.project.find((p) => typeof p === 'string' && p) : str(ep.project);
  return {
    project: project ?? str(st.projectName),
    projectId: str(ep.projectId) ?? str(st.projectId),
    domain: str(ep.domain) ?? str(st.domainName),
    domainId: str(ep.domainId) ?? str(st.domainId),
  };
}

/* ------------------------------------------------------------------ create and edit form */

/**
 * The access groups the form starts with: the reader's <tenant>.<access group> sign-in groups that
 * name access groups of this tenant (as the OpenShift Virtualization form does).
 */
export function prefillAccessGroups(readerGroups: string[], entityName: string, rbacNames: string[]): string[] {
  if (!entityName) return [];
  const prefix = `${entityName}.`;
  const mine = readerGroups
    .filter((g) => g.startsWith(prefix))
    .map((g) => g.slice(prefix.length))
    .filter((r) => rbacNames.includes(r));
  return Array.from(new Set(mine));
}

/**
 * The create form's pre-fill of "Create and change" (OpenStack environment): the creator's access groups in this
 * tenant, once, while the user has not changed the list and it is still empty; null leaves the field as it is.
 */
export function osoPrefillEdit(input: {
  type: string;
  touched: boolean;
  current: string[];
  readerGroups?: string[];
  entityName: string;
  rbacNames: string[];
}): string[] | null {
  if (input.type !== 'cloudoso' || input.touched || input.current.length || !input.readerGroups) return null;
  if (!input.rbacNames.length) return null;
  const mine = prefillAccessGroups(input.readerGroups, input.entityName, input.rbacNames);
  return mine.length ? mine : null;
}

/** spec.toolRbac of the environment, or undefined when both lists are empty. */
export function cloudOsoToolRbac(editRbac: string[], viewRbac: string[]): CloudOSOToolRbac | undefined {
  const clean = (l: string[]) => Array.from(new Set(l.map((s) => s.trim()).filter(Boolean)));
  const edit = clean(editRbac);
  const view = clean(viewRbac).filter((v) => !edit.includes(v));
  if (!edit.length && !view.length) return undefined;
  return { ...(edit.length ? { editRbac: edit } : {}), ...(view.length ? { viewRbac: view } : {}) };
}

/** What the create form holds for an OpenStack environment. */
export interface CloudOsoFormInput {
  site: string;
  project: string;
  landingzone?: string;
  baseDomain?: string;
  projectDomain?: string;
  externalNetwork?: string;
  regionName?: string;
  dnsProvider?: string;
  designateZoneId?: string;
  designateProjectId?: string;
  editRbac: string[];
  viewRbac: string[];
  /** Platform pages only */
  vaultPath?: string;
  route53VaultPath?: string;
}

/** The spec the create form sends for an OpenStack environment (empty values left out). */
export function buildCloudOsoSpec(input: CloudOsoFormInput): Record<string, unknown> {
  const optional = (key: string, value: string | undefined): Record<string, string> =>
    value && value.trim() ? { [key]: value.trim() } : {};
  const toolRbac = cloudOsoToolRbac(input.editRbac, input.viewRbac);
  return {
    ...(input.site.trim() ? { cloudRef: { name: input.site.trim() } } : {}),
    project: input.project.trim(),
    ...optional('landingzone', input.landingzone),
    ...optional('baseDomain', input.baseDomain),
    ...optional('projectDomain', input.projectDomain),
    ...optional('externalNetwork', input.externalNetwork),
    ...optional('regionName', input.regionName),
    ...optional('dnsProvider', input.dnsProvider),
    ...optional('designateZoneId', input.designateZoneId),
    ...optional('designateProjectId', input.designateProjectId),
    ...optional('vaultPath', input.vaultPath),
    ...optional('route53VaultPath', input.route53VaultPath),
    ...(toolRbac ? { toolRbac } : {}),
  };
}

/**
 * Whether the chosen site offers sign-in with the platform identity: the tenant's site summary
 * (capability ConsoleSignIn), else the site itself (platform pages: status.capabilities.consoleSignIn).
 * undefined: no site chosen, nothing to say.
 */
export function siteOffersSignIn(site: TenantProfileSite | undefined, infra?: CloudInfrastructure): boolean | undefined {
  if (site) return (site.capabilities ?? []).includes('ConsoleSignIn');
  if (infra) return infra.status?.capabilities?.consoleSignIn === true;
  return undefined;
}

/* ------------------------------------------------------------------ placement */

/** status.siteNetwork of a placement (null without a network name). */
export function placementSiteNetwork(item: K8sResource | undefined): PlacementSiteNetwork | null {
  const sn = (item?.status as { siteNetwork?: unknown } | undefined)?.siteNetwork;
  if (!isObj(sn)) return null;
  const name = str(sn.name);
  if (!name) return null;
  return { name, id: str(sn.id), project: str(sn.project) };
}

const SHELL_SAFE = /^[A-Za-z0-9._:/@+=-]+$/;

/** The command that starts a server on the placement's network (the network name quoted when it needs it). */
export function serverCreateCommand(network: string): string {
  const arg = SHELL_SAFE.test(network) ? network : `'${network.replace(/'/g, `'\\''`)}'`;
  return `openstack server create --network ${arg} --flavor <flavor> --image <image> --key-name <key> <server name>`;
}

/* ------------------------------------------------------------------ cluster */

/** status.credential of an OpenStack cluster (null without a user). */
export function clusterSiteUser(item: K8sResource | undefined): PlatformOpenshiftCredential | null {
  const c = (item?.status as { credential?: unknown } | undefined)?.credential;
  if (!isObj(c)) return null;
  const user = str(c.user);
  if (!user) return null;
  return {
    user,
    userId: str(c.userId),
    domain: str(c.domain),
    domainId: str(c.domainId),
    id: str(c.id),
    createdAt: str(c.createdAt),
  };
}

/** The ClusterCredential condition when it is False: what the site refused. */
export function clusterCredentialRefusal(item: K8sResource | undefined): { reason?: string; message?: string } | null {
  const c = conditionOf(item, 'ClusterCredential');
  if (!c || c.status !== 'False') return null;
  return { reason: str(c.reason), message: str(c.message) };
}
