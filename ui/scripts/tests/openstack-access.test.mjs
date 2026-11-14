// An OpenStack environment people can use (p-osoui): the environment's card "How to use it", the create and edit
// form's access groups (spec.toolRbac), the placement's network on the site and the cluster's site user. The real
// shared components are rendered on the server (react-dom/server) from objects shaped as the API writes them; the
// helpers in forms/openstackAccess.ts are tested on their own; the tenant dashboard's spec sanitizer must pass
// what the form writes.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';

const UI = process.env.UI_ROOT;
const OUT = process.env.SHELL_BUNDLE_DIR;
const m = await import(path.join(OUT, 'openstackAccess.mjs'));
const san = await import(path.join(UI, 'packages/tenant-dashboard/server/sanitizers.js'));
const load = (l) => JSON.parse(fs.readFileSync(path.join(UI, `packages/shared/src/i18n/locales/${l}.json`), 'utf8'));
const en = load('en');
const fr = load('fr');

// the components and react-dom/server in one bundle (one React)
const entry = `import { renderToStaticMarkup } from 'react-dom/server';
import { createElement } from 'react';
import { WayInCard } from './components/WayInPanel';
import { KindStatusDetails, KIND_STATUS_KEYS, redactForTenant } from './components/KindStatusDetails';
import { OsoAccessGroupsField } from './components/OsoAccessGroupsField';
import { KIND_SPEC_META } from './forms/specFieldMeta';
export const card = (kind, item, readerGroups) => renderToStaticMarkup(createElement(WayInCard, { kind, item, readerGroups }));
export const details = (kind, item) => renderToStaticMarkup(createElement(KindStatusDetails, { kind, item }));
export const field = (props) => renderToStaticMarkup(createElement(OsoAccessGroupsField, { onEditChange: () => {}, onViewChange: () => {}, ...props }));
export { KIND_STATUS_KEYS, redactForTenant, KIND_SPEC_META };`;
const bundle = path.join(OUT, 'openstackAccessPages.mjs');
execFileSync(path.join(UI, 'node_modules/.bin/esbuild'), [
  '--bundle', '--format=esm', '--platform=node', '--log-level=error', '--jsx=automatic', '--loader=tsx',
  '--loader:.json=json', '--loader:.css=empty', '--loader:.svg=dataurl', '--define:process.env.NODE_ENV="production"',
  "--banner:js=import { createRequire as __shcRequire } from 'module'; const require = __shcRequire(import.meta.url);",
  '--sourcefile=entry.tsx', `--outfile=${bundle}`,
], { cwd: path.join(UI, 'packages/shared/src'), input: entry });
const pages = await import(bundle);

/** The page's text: tags removed, entities decoded, spaces collapsed. */
function text(html) {
  return html
    .replace(/<[^>]+>/g, ' ')
    .replace(/&#x27;/g, "'")
    .replace(/&quot;/g, '"')
    .replace(/&lt;/g, '<')
    .replace(/&gt;/g, '>')
    .replace(/&amp;/g, '&')
    .replace(/\s+/g, ' ')
    .trim();
}
const fill = (s, vars) => s.replace(/\{\{(\w+)\}\}/g, (_x, k) => String(vars[k]));
/** The description of the row with this term (horizontal description list), or null. */
function row(html, term) {
  const re = /<dt[^>]*>(.*?)<\/dt>\s*<dd[^>]*>(.*?)<\/dd>/gs;
  for (const mm of html.matchAll(re)) if (text(mm[1]) === term) return text(mm[2]);
  return null;
}

const CONSOLE = 'https://identity.site-a.example/v3/auth/OS-FEDERATION/identity_providers/platform-tenants/protocols/openid/websso?origin=https://dashboard.site-a.example/auth/websso/';

// a CloudOSO as the environment job writes it after this round (DESIGN2 §4.2, without clusterIdentity: §12 G8)
function env({ start = 'IdentityProvider', groups, apiCredentials, conditions, signInGroups, method = 'TenantSSO' } = {}) {
  return {
    apiVersion: 'hybridsovereign.redhat/v1alpha1',
    kind: 'CloudOSO',
    metadata: { name: 'web', namespace: 'entity-acme' },
    spec: { cloudRef: { name: 'site-a' }, toolRbac: { editRbac: ['developers'], viewRbac: ['auditors'] } },
    status: {
      phase: 'Ready',
      projectName: 'acme_web', projectId: '8a1f0c', domainName: 'tenants', domainId: '51c0aa',
      appCredentialId: null,
      access: {
        signInClient: 'openstack-site-a',
        groupDomain: 'tenants',
        groups: groups ?? [
          { rbac: 'developers', group: 'acme.developers', access: 'Edit', role: 'member', state: 'Granted' },
          { rbac: 'auditors', group: 'acme.auditors', access: 'View', role: 'reader', state: 'Granted' },
        ],
      },
      endpoints: {
        console: CONSOLE,
        signIn: { method, idpName: 'Example Platform', start },
        signInGroups: signInGroups ?? ['acme.developers', 'acme.auditors'],
        api: 'https://identity.site-a.example',
        project: 'acme_web', projectId: '8a1f0c', domain: 'tenants', domainId: '51c0aa',
        region: 'RegionOne', location: 'Frankfurt',
        apiCredentials: apiCredentials === undefined ? { state: 'Offered', validMinutesAfterSignIn: 1440, source: 'Site' } : apiCredentials,
      },
      conditions: conditions ?? [{ type: 'AccessReady', status: 'True', reason: 'GroupsGranted', message: '2 access groups can use this environment.' }],
    },
  };
}

const W = en.wayin;
const O = en.wayin.oso;
const OLD_SENTENCE = 'Human sign-in is not offered';

test('the strings are the design\'s (DESIGN2 §8.4, §12), in en and fr', () => {
  assert.equal(W.open.CloudOSO, 'Open the OpenStack dashboard');
  assert.equal(O.signInIdentityProvider, 'You sign in with your platform identity.');
  assert.equal(O.signInDashboard, 'On the sign-in page, choose {{name}}. You sign in with your platform identity.');
  assert.equal(O.groups, 'Groups that can use it');
  assert.equal(O.groupEdit, '{{group}} (create and change)');
  assert.equal(O.groupView, '{{group}} (read only)');
  assert.equal(O.groupNotReady, '{{group}} (not ready: {{message}})');
  assert.equal(W.youAreIn, 'You are in {{groups}}.');
  assert.equal(O.youAreNotIn, "You are not in any of these groups. Ask your tenant's access administrator to add you to one.");
  assert.equal(O.noGroup, 'No access group can use this environment. Nobody can sign in to it. Add an access group to the environment.');
  assert.equal(O.apiCredentialsIntro, 'For command-line tools, create an application credential in the dashboard (Identity, Application Credentials) and download its clouds.yaml file. An application credential is a secret that lets a tool act as you in this project.');
  assert.equal(O.apiCredentialsPeriod, 'It works for {{period}} after your last sign-in to the dashboard. Sign in again to keep it working.');
  assert.equal(O.apiCredentialsUnknown, 'How long it works after your last sign-in is set by the site. Ask the platform team.');
  assert.equal(O.apiCredentialsNotOffered, 'People cannot create API credentials on this site. Use the dashboard, or ask the platform team.');
  assert.equal(O.siteNoSignIn, 'Sign-in with the platform identity is not set up on this site. People cannot use this environment directly. The platform can still build OpenShift clusters in it.');
  assert.equal(en.fields.osoAccessGroups, 'Groups that can use this environment');
  assert.equal(en.fields.osoEditRbac, 'Create and change');
  assert.equal(en.fields.osoViewRbac, 'Read only');
  assert.equal(en.fields.osoAccessGroupsHelp, "Members can sign in to the site's dashboard and work in the project. Pre-filled with your access groups in this tenant.");
  assert.equal(en.fields.osoSiteNoSignIn, 'People cannot sign in on this site yet. The platform can still build OpenShift clusters in the environment.');
  assert.equal(en.detail.siteNetwork, 'Network on the site: {{name}} ({{id}})');
  assert.equal(en.detail.siteNetworkUse, 'Choose this network when you create a server. It is shared with your project and owned by the platform.');
  assert.equal(en.detail.siteUserActions, "Actions by this user in the project are this cluster's.");
  // §12 G8: no row about cluster builds on the environment
  assert.equal(JSON.stringify(en).includes('Cluster builds'), false);
  for (const [k, v] of Object.entries(O)) assert.equal(typeof fr.wayin.oso[k], 'string', `fr wayin.oso.${k}: ${v}`);
});

test('card: sign-in set up, two granted groups, the reader in one, API credentials for 1 day', () => {
  const html = pages.card('CloudOSO', env(), ['acme.developers', 'acme-other.x']);
  const page = text(html);
  assert.ok(html.includes(`href="${CONSOLE.replace(/&/g, '&amp;')}"`), 'the button opens the console URL');
  assert.ok(page.includes('Open the OpenStack dashboard'));
  assert.ok(page.includes(O.signInIdentityProvider));
  assert.equal(row(html, O.groups), 'developers (create and change) auditors (read only)');
  assert.equal(row(html, W.you), 'You are in developers.');
  assert.equal(row(html, W.api), 'https://identity.site-a.example');
  assert.equal(row(html, W.region), 'RegionOne');
  assert.equal(row(html, W.project), 'acme_web (ID 8a1f0c)');
  assert.equal(row(html, W.domain), 'tenants (ID 51c0aa)');
  assert.equal(row(html, W.location), 'Frankfurt');
  assert.equal(row(html, O.apiCredentials), `${O.apiCredentialsIntro} ${fill(O.apiCredentialsPeriod, { period: '1 day' })}`);
  assert.ok(!page.includes(O.noGroup));
  assert.ok(!page.includes(OLD_SENTENCE));
  assert.ok(!page.includes(O.siteNoSignIn));
  assert.ok(!/App credential/i.test(page));
  assert.ok(!page.includes(W.signInGroups), 'the CloudVirt row name is not used');
});

test('card: start at the dashboard names the sign-in entry', () => {
  const page = text(pages.card('CloudOSO', env({ start: 'Dashboard' }), []));
  assert.ok(page.includes('On the sign-in page, choose Example Platform. You sign in with your platform identity.'));
  // without the entry's name the card says only how people sign in
  const noName = env({ start: 'Dashboard' });
  delete noName.status.endpoints.signIn.idpName;
  const bare = text(pages.card('CloudOSO', noName, []));
  assert.ok(bare.includes(O.signInIdentityProvider));
  assert.ok(!bare.includes('choose'));
});

test('card: the reader is in none of the groups; an unknown reader gets no row', () => {
  const html = pages.card('CloudOSO', env(), ['acme.others']);
  assert.equal(row(html, W.you), O.youAreNotIn);
  assert.equal(row(pages.card('CloudOSO', env(), undefined), W.you), null);
});

test('card: no access group warns and leaves out the row "You"', () => {
  const item = env({
    groups: [],
    signInGroups: [],
    conditions: [{ type: 'AccessReady', status: 'False', reason: 'NoAccessGroups', message: 'No access group can use this environment. Nobody can sign in to it.' }],
  });
  const html = pages.card('CloudOSO', item, ['acme.developers']);
  assert.ok(text(html).includes(O.noGroup));
  assert.equal(row(html, O.groups), null);
  assert.equal(row(html, W.you), null);
  // the same from the empty list alone (no condition yet)
  const bare = env({ groups: [], signInGroups: [], conditions: [] });
  assert.ok(text(pages.card('CloudOSO', bare, [])).includes(O.noGroup));
});

test('card: a group waiting for its directory group, and one the site refused', () => {
  const item = env({
    groups: [
      { rbac: 'developers', group: 'acme.developers', access: 'Edit', role: 'member', state: 'Granted' },
      { rbac: 'auditors', group: 'acme.auditors', access: 'View', role: 'reader', state: 'Waiting', message: 'The access group has no group in the sign-in directory yet.' },
      { rbac: 'ops', group: 'acme.ops', access: 'Edit', role: 'member', state: 'Failed', message: 'Could not find role: member.' },
      { rbac: 'qa', group: 'acme.qa', access: 'View', state: 'Waiting' },
    ],
    signInGroups: ['acme.developers'],
  });
  const html = pages.card('CloudOSO', item, ['acme.auditors']);
  assert.equal(
    row(html, O.groups),
    'developers (create and change) auditors (not ready: The access group has no group in the sign-in directory yet.) ops (not ready: Could not find role: member.) qa (not ready)',
  );
  // a reader only in the waiting group cannot sign in yet
  assert.equal(row(html, W.you), O.youAreNotIn);
  assert.ok(!text(html).includes(O.noGroup));
});

test('card: a site without sign-in says so and offers no credential text', () => {
  const item = env({
    method: 'NotAvailable',
    groups: [],
    signInGroups: [],
    apiCredentials: { state: 'NotOffered', source: 'Declared' },
    conditions: [{ type: 'AccessReady', status: 'False', reason: 'SignInNotSetUp', message: 'Sign-in with the platform identity is not set up on site site-a.' }],
  });
  delete item.status.endpoints.console;
  const html = pages.card('CloudOSO', item, ['acme.developers']);
  const page = text(html);
  assert.ok(page.includes(O.siteNoSignIn));
  assert.ok(!page.includes(O.noGroup), 'the warning is for sites with sign-in');
  assert.ok(!page.includes(O.signInIdentityProvider));
  assert.ok(!page.includes(W.notPublished));
  assert.ok(!page.includes(OLD_SENTENCE));
  assert.equal(row(html, O.apiCredentials), null);
  assert.equal(row(html, W.you), null);
  assert.equal(row(html, W.project), 'acme_web (ID 8a1f0c)');
  assert.ok(!html.includes('<a '), 'no dashboard button');
});

test('card: the three forms of the API credentials text', () => {
  const cases = [
    [{ state: 'Offered', validMinutesAfterSignIn: 480, source: 'Declared' }, `${O.apiCredentialsIntro} ${fill(O.apiCredentialsPeriod, { period: '8 hours' })}`],
    [{ state: 'Offered', validMinutesAfterSignIn: 10080, source: 'Site' }, `${O.apiCredentialsIntro} ${fill(O.apiCredentialsPeriod, { period: '7 days' })}`],
    [{ state: 'Offered', validMinutesAfterSignIn: 90 }, `${O.apiCredentialsIntro} ${fill(O.apiCredentialsPeriod, { period: '90 minutes' })}`],
    [{ state: 'Offered', validMinutesAfterSignIn: 60 }, `${O.apiCredentialsIntro} ${fill(O.apiCredentialsPeriod, { period: '1 hour' })}`],
    [{ state: 'Unknown' }, `${O.apiCredentialsIntro} ${O.apiCredentialsUnknown}`],
    [{ state: 'Offered' }, `${O.apiCredentialsIntro} ${O.apiCredentialsUnknown}`],
    [{ state: 'NotOffered', validMinutesAfterSignIn: 0, source: 'Site' }, O.apiCredentialsNotOffered],
    [null, null],
  ];
  for (const [ac, want] of cases) {
    const html = pages.card('CloudOSO', env({ apiCredentials: ac }), []);
    assert.equal(row(html, O.apiCredentials), want, JSON.stringify(ac));
  }
});

test('card: an environment written before this round renders what it carries', () => {
  // old job: no status.access, signIn NotAvailable, location filled from the region, no region, no ids in endpoints
  const old = {
    apiVersion: 'hybridsovereign.redhat/v1alpha1',
    kind: 'CloudOSO',
    metadata: { name: 'web', namespace: 'entity-acme' },
    spec: { cloudRef: { name: 'site-a' }, project: 'web' },
    status: {
      projectName: 'acme_web', projectId: '8a1f0c', domainName: 'tenants', domainId: '51c0aa', appCredentialId: 'ac-1',
      endpoints: { api: 'https://identity.site-a.example', project: 'acme_web', domain: 'tenants', location: 'ATUIN', signIn: { method: 'NotAvailable' } },
    },
  };
  const html = pages.card('CloudOSO', old, ['acme.developers']);
  const page = text(html);
  assert.equal(row(html, W.project), 'acme_web (ID 8a1f0c)');
  assert.equal(row(html, W.domain), 'tenants (ID 51c0aa)');
  assert.equal(row(html, W.location), 'ATUIN');
  assert.equal(row(html, W.region), null);
  assert.equal(row(html, O.groups), null);
  assert.ok(page.includes(O.siteNoSignIn));
  assert.ok(!page.includes(OLD_SENTENCE));
  assert.ok(!page.includes('ac-1'));
  // nothing published yet, and no status at all
  const empty = text(pages.card('CloudOSO', { ...old, status: {} }, []));
  assert.ok(empty.includes(W.title));
  assert.ok(!empty.includes(O.siteNoSignIn));
  assert.ok(text(pages.card('CloudOSO', { ...old, status: undefined }, undefined)).includes(W.title));
  // malformed entries are skipped, not fatal
  const odd = env({ groups: [null, 'x', { state: 'Granted' }, { rbac: 'developers', access: 'Edit', state: 'Granted' }] });
  assert.equal(row(pages.card('CloudOSO', odd, []), O.groups), 'developers (create and change)');
});

test('card: OpenShift Virtualization environments keep their rows', () => {
  const virt = {
    apiVersion: 'hybridsovereign.redhat/v1alpha1',
    kind: 'CloudVirt',
    metadata: { name: 'vms', namespace: 'entity-acme' },
    status: {
      endpoints: { console: 'https://console.virt.example', signIn: { method: 'TenantSSO', idpName: 'Tenants' }, signInGroups: ['acme.developers'] },
      access: { groups: ['acme.developers'] },
      namespaces: [{ name: 'web', namespace: 'acme-vms-web' }],
    },
  };
  const html = pages.card('CloudVirt', virt, ['acme.others']);
  assert.ok(text(html).includes(W.open.CloudVirt));
  assert.equal(row(html, W.signInGroups), 'acme.developers');
  assert.equal(row(html, W.you), W.youAreNotIn);
  assert.equal(row(html, W.namespaces), 'acme-vms-web');
});

test('the overview grid leaves out access and the former credential; tenant pages never carry it', () => {
  assert.ok(pages.KIND_STATUS_KEYS.CloudOSO.includes('access'));
  assert.ok(pages.KIND_STATUS_KEYS.NetworkPlacement.includes('siteNetwork'));
  assert.ok(pages.KIND_STATUS_KEYS.PlatformOpenshift.includes('credential'));
  const red = pages.redactForTenant('CloudOSO', { ...env(), status: { ...env().status, appCredentialId: 'ac-1' } });
  assert.equal('appCredentialId' in red.status, false);
  assert.deepEqual(red.status.access, env().status.access);
  const src = fs.readFileSync(path.join(UI, 'packages/shared/src/components/ResourceDetail.tsx'), 'utf8');
  assert.match(src, /LEGACY_STATUS_KEYS = \[[^\]]*'appCredentialId'/);
});

test('helpers: periods, groups of the reader, prefill, toolRbac', () => {
  assert.deepEqual(m.periodParts(1440), { unit: 'day', count: 1 });
  assert.deepEqual(m.periodParts(480), { unit: 'hour', count: 8 });
  assert.deepEqual(m.periodParts(10080), { unit: 'day', count: 7 });
  assert.deepEqual(m.periodParts(45), { unit: 'minute', count: 45 });
  assert.deepEqual(m.osoReaderGroups(env(), ['acme.auditors']), ['auditors']);
  assert.deepEqual(m.prefillAccessGroups(['acme.developers', 'acme.gone', 'other.developers', 'acme.developers'], 'acme', ['developers', 'auditors']), ['developers']);
  assert.deepEqual(m.prefillAccessGroups(['acme.developers'], '', ['developers']), []);
  assert.equal(m.cloudOsoToolRbac([], []), undefined);
  assert.deepEqual(m.cloudOsoToolRbac(['developers', ' developers '], ['auditors', 'developers']), { editRbac: ['developers'], viewRbac: ['auditors'] });
  assert.deepEqual(m.cloudOsoToolRbac([], ['auditors']), { viewRbac: ['auditors'] });
});

test('form: the create form writes spec.toolRbac and the tenant dashboard passes it on', () => {
  const spec = m.buildCloudOsoSpec({ site: 'site-a', project: 'web', landingzone: '', editRbac: ['developers'], viewRbac: ['auditors'] });
  assert.deepEqual(spec, { cloudRef: { name: 'site-a' }, project: 'web', toolRbac: { editRbac: ['developers'], viewRbac: ['auditors'] } });
  assert.deepEqual(m.buildCloudOsoSpec({ site: '', project: 'web', editRbac: [], viewRbac: [] }), { project: 'web' });
  const out = san.sanitizeCloudOSOSpec(spec);
  assert.equal(out.error, undefined);
  assert.deepEqual(out.spec.toolRbac, { editRbac: ['developers'], viewRbac: ['auditors'] });
  assert.ok(san.sanitizeCloudOSOSpec({ project: 'web', toolRbac: 'developers' }).error);
  assert.ok(san.sanitizeCloudOSOSpec({ project: 'web', toolRbac: { editRbac: [1] } }).error);
  assert.equal('toolRbac' in san.sanitizeCloudOSOSpec({ project: 'web', toolRbac: { editRbac: [] } }).spec, false);
  // the edit form offers both lists
  const paths = pages.KIND_SPEC_META.CloudOSO.fields.map((f) => [f.path, f.widget]);
  assert.deepEqual(paths.filter(([p]) => p.startsWith('toolRbac.')), [['toolRbac.editRbac', 'rbacRefList'], ['toolRbac.viewRbac', 'rbacRefList']]);
});

test('form: the field "Groups that can use this environment" and the hint for a site without sign-in', () => {
  const options = [{ value: 'developers', label: 'developers' }, { value: 'auditors', label: 'auditors' }];
  const html = pages.field({ editRbac: ['developers'], viewRbac: [], options, siteSignIn: false });
  const page = text(html);
  for (const s of [en.fields.osoAccessGroups, en.fields.osoEditRbac, en.fields.osoViewRbac, en.fields.osoAccessGroupsHelp, en.fields.osoSiteNoSignIn]) {
    assert.ok(page.includes(s), s);
  }
  assert.ok(page.includes('developers'), 'the pre-filled group is shown');
  assert.ok(!text(pages.field({ editRbac: [], viewRbac: [], options, siteSignIn: true })).includes(en.fields.osoSiteNoSignIn));
  assert.ok(!text(pages.field({ editRbac: [], viewRbac: [], options })).includes(en.fields.osoSiteNoSignIn));
  // where the hint comes from: the tenant's site summary, else the site (platform pages)
  assert.equal(m.siteOffersSignIn({ name: 'site-a', type: 'openstack', state: 'Available', capabilities: ['HybridNetworking', 'ConsoleSignIn'] }), true);
  assert.equal(m.siteOffersSignIn({ name: 'site-a', type: 'openstack', state: 'Available', capabilities: ['HybridNetworking'] }), false);
  assert.equal(m.siteOffersSignIn({ name: 'site-a', type: 'openstack', state: 'Available' }), false);
  assert.equal(m.siteOffersSignIn(undefined, { status: { capabilities: { consoleSignIn: true } } }), true);
  assert.equal(m.siteOffersSignIn(undefined, { status: {} }), false);
  assert.equal(m.siteOffersSignIn(undefined, undefined), undefined);
});

const placement = (siteNetwork, backendKind = 'CloudOSO') => ({
  apiVersion: 'hybridsovereign.redhat/v1alpha1',
  kind: 'NetworkPlacement',
  metadata: { name: 'core-web', namespace: 'entity-acme' },
  spec: { network: 'core', backend: { kind: backendKind, name: 'web' }, ipv4: { prefixLength: 24 } },
  status: {
    site: 'site-a', backendReady: true, prefixes: ['10.60.1.0/24'],
    ...(siteNetwork === undefined ? {} : { siteNetwork }),
  },
});

test('placement: the network on the site, the sentence and the copyable command', () => {
  const sn = { name: 'hn10042', id: '3e2b7c1d', project: 'hybrid-fabric', subnets: [{ family: 4, prefix: '10.60.1.0/24', gateway: '10.60.1.1', id: 's1' }] };
  const html = pages.details('NetworkPlacement', placement(sn));
  const page = text(html);
  assert.ok(page.includes('Network on the site: hn10042 (3e2b7c1d)'));
  assert.ok(page.includes(en.detail.siteNetworkUse));
  // the command is the copy field's value (a read-only input)
  const input = [...html.matchAll(/<input[^>]*>/g)].map((x) => x[0]).find((tag) => tag.includes(`aria-label="${en.detail.siteNetworkCommand}"`));
  assert.ok(input, 'copy field rendered');
  assert.equal(text(/value="([^"]*)"/.exec(input)[1]), 'openstack server create --network hn10042 --flavor <flavor> --image <image> --key-name <key> <server name>');
  assert.ok(text(pages.details('NetworkPlacement', placement({ name: 'hn10042' }))).includes('Network on the site: hn10042 '));
  for (const none of [undefined, null, {}, { id: 'x' }]) {
    assert.ok(!text(pages.details('NetworkPlacement', placement(none))).includes('Network on the site'), JSON.stringify(none));
  }
  assert.ok(!text(pages.details('NetworkPlacement', placement(sn, 'CloudVirt'))).includes('Network on the site'));
  assert.equal(m.serverCreateCommand("my net's"), `openstack server create --network 'my net'\\''s' --flavor <flavor> --image <image> --key-name <key> <server name>`);
});

const cluster = ({ credential, conditions } = {}) => ({
  apiVersion: 'hybridsovereign.redhat/v1alpha1',
  kind: 'PlatformOpenshift',
  metadata: { name: 'c1', namespace: 'entity-acme' },
  spec: { type: 'openstack', openstack: { environment: 'web' } },
  status: { phase: 'Ready', ...(credential ? { credential } : {}), conditions: conditions ?? [] },
});

test('cluster: the site user with its sentence; the refusal when ClusterCredential is False', () => {
  const credential = { id: 'ac9d', user: 'shc-c1-x7k2p', userId: '0f3c11', domain: 'tenants', createdAt: '2026-11-14T10:00:00Z' };
  const ok = pages.details('PlatformOpenshift', cluster({
    credential,
    conditions: [{ type: 'ClusterCredential', status: 'True', reason: 'OwnCredential', message: 'The cluster uses its own credential on the site, owned by the site user shc-c1-x7k2p.' }],
  }));
  assert.equal(row(ok, en.detail.siteUser), `shc-c1-x7k2p (ID 0f3c11) ${en.detail.siteUserActions}`);
  assert.equal(row(ok, W.domain), 'tenants');
  assert.ok(!text(ok).includes(en.detail.clusterCredentialRefused));
  const refused = pages.details('PlatformOpenshift', cluster({
    conditions: [{ type: 'ClusterCredential', status: 'False', reason: 'RoleRefused', message: 'The site refused the role member: You are not authorized. The cluster cannot be built.' }],
  }));
  assert.ok(text(refused).includes(en.detail.clusterCredentialRefused));
  assert.ok(text(refused).includes('The site refused the role member: You are not authorized. The cluster cannot be built.'));
  assert.equal(row(refused, en.detail.siteUser), null);
  // other clusters (no credential, no condition): nothing
  assert.equal(pages.details('PlatformOpenshift', cluster()), '');
  const aws = { ...cluster(), spec: { type: 'aws' } };
  assert.equal(pages.details('PlatformOpenshift', aws), '');
});
