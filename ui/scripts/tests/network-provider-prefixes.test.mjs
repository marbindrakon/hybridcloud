// A network's IPv6 from the cloud provider (status.ipv6.providerPrefixes): the helper in forms/ipv6.ts and the
// network page's read-only row group under the address space, rendered on the server (react-dom/server) from the
// shared detail panel the dashboards and console plugins (admin and tenant) use.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';

const UI = process.env.UI_ROOT;
const OUT = process.env.SHELL_BUNDLE_DIR;
const m = await import(path.join(OUT, 'ipv6.mjs'));
const load = (l) => JSON.parse(fs.readFileSync(path.join(UI, `packages/shared/src/i18n/locales/${l}.json`), 'utf8'));
const KIND_NAME = /\b(HybridNetwork|NetworkPlacement|CloudInfrastructure|CloudAWS|CloudGCP)\b/;

// the panel and react-dom/server in one bundle (one React); CSS and images are not needed for the markup
const entry = `import { renderToStaticMarkup } from 'react-dom/server';
import { createElement } from 'react';
import { KindStatusDetails, redactForTenant } from './components/KindStatusDetails';
export const render = (kind, item) => renderToStaticMarkup(createElement(KindStatusDetails, { kind, item }));
export { redactForTenant };`;
const bundle = path.join(OUT, 'kindStatusDetails.mjs');
execFileSync(path.join(UI, 'node_modules/.bin/esbuild'), [
  '--bundle', '--format=esm', '--platform=node', '--log-level=error', '--jsx=automatic', '--loader=tsx',
  '--loader:.json=json', '--loader:.css=empty', '--loader:.svg=dataurl', '--define:process.env.NODE_ENV="production"',
  "--banner:js=import { createRequire as __shcRequire } from 'module'; const require = __shcRequire(import.meta.url);",
  '--sourcefile=entry.tsx', `--outfile=${bundle}`,
], { cwd: path.join(UI, 'packages/shared/src'), input: entry });
const panel = await import(bundle);

const AWS = { prefix: '2001:db8:0:5800::/56', placement: 'web-aws', site: 'aws-1' };
const GCP = { prefix: '2001:db8:4090:c74::/64', placement: 'web-gcp', site: 'gcp-1' };
const network = (providerPrefixes) => ({
  apiVersion: 'hybridsovereign.redhat/v1alpha1',
  kind: 'HybridNetwork',
  metadata: { name: 'g', namespace: 'entity-t1' },
  spec: { prefixes: ['10.70.0.0/16'], platformPrefixes: [{ family: 6 }] },
  status: {
    ready: true,
    addressSpace: [
      { prefix: '10.70.0.0/16', family: 4, source: 'Tenant', allocations: 3, largestFree: 17 },
      { prefix: '2001:db8:4005:4900::/56', family: 6, source: 'Platform', allocations: 1, largestFree: 57 },
    ],
    ipv6: {
      mode: 'Platform',
      prefixes: ['2001:db8:4005:4900::/56'],
      block: { prefix: '2001:db8:4005:4900::/56', prefixLength: 56, source: 'Allocated' },
      ...(providerPrefixes === undefined ? {} : { providerPrefixes }),
    },
  },
});

/** The table with this aria-label: its header cells and its body rows' cells (text only). */
function table(html, label) {
  const start = html.indexOf(`<table aria-label="${label}"`);
  if (start < 0) return null;
  const t = html.slice(start, html.indexOf('</table>', start));
  const text = (s) => s.replace(/<[^>]+>/g, '').replace(/&#x27;/g, "'").trim();
  const head = [...t.slice(0, t.indexOf('</thead>')).matchAll(/<th[^>]*>(.*?)<\/th>/g)].map((x) => text(x[1]));
  const body = t.slice(t.indexOf('<tbody'));
  const rows = [...body.matchAll(/<tr[^>]*>(.*?)<\/tr>/g)].map((r) => [...r[1].matchAll(/<td[^>]*>(.*?)<\/td>/g)].map((c) => text(c[1])));
  return { head, rows };
}

test('the helper: the platform list in its order; nothing when absent, null or malformed', () => {
  assert.deepEqual(m.networkProviderPrefixes(network([GCP, AWS])), [GCP, AWS]);
  assert.deepEqual(m.networkProviderPrefixes(network(undefined)), []);
  assert.deepEqual(m.networkProviderPrefixes(network(null)), []);
  assert.deepEqual(m.networkProviderPrefixes(undefined), []);
  assert.deepEqual(m.networkProviderPrefixes({ status: { ipv6: { providerPrefixes: 'x' } } }), []);
  assert.deepEqual(m.networkProviderPrefixes(network([null, { placement: 'p' }, { prefix: '' }, { prefix: GCP.prefix, site: 7 }])),
    [{ prefix: GCP.prefix }]);
});

test('the network page lists them read-only under the address space: prefix, placement, site', () => {
  const html = panel.render('HybridNetwork', network([AWS, GCP]));
  const en = load('en');
  const pp = table(html, en.ipv6.providerPrefixes);
  assert.ok(pp, 'provider table rendered');
  assert.deepEqual(pp.head, [en.ipv6.providerPrefixes, en.kindsOne.NetworkPlacement, en.fields.site]);
  assert.deepEqual(pp.rows, [[AWS.prefix, AWS.placement, AWS.site], [GCP.prefix, GCP.placement, GCP.site]]);
  assert.ok(html.includes(en.ipv6.providerPrefixesNote.replace(/'/g, '&#x27;')));
  // the address space keeps its own rows and columns: no provider prefix in it
  const space = table(html, en.detail.addressSpace);
  assert.deepEqual(space.rows.map((r) => r[0]), ['10.70.0.0/16', '2001:db8:4005:4900::/56']);
  assert.ok(space.head.includes(en.detail.largestFree));
  assert.ok(!pp.head.includes(en.detail.largestFree) && !pp.head.includes(en.detail.allocations));
  // the row group follows the address space table, inside its section
  assert.ok(html.indexOf(`aria-label="${en.detail.addressSpace}"`) < html.indexOf(`aria-label="${en.ipv6.providerPrefixes}"`));
  assert.ok(html.indexOf(`aria-label="${en.ipv6.providerPrefixes}"`) < html.indexOf(`>${en.ipv6.title}</h3>`));
});

test('nothing is shown without provider prefixes', () => {
  const en = load('en');
  for (const lst of [undefined, null, []]) {
    const html = panel.render('HybridNetwork', network(lst));
    assert.equal(table(html, en.ipv6.providerPrefixes), null);
    assert.ok(!html.includes(en.ipv6.providerPrefixesNote.slice(0, 20)));
  }
});

test('an entry without a site shows a dash', () => {
  const pp = table(panel.render('HybridNetwork', network([{ prefix: GCP.prefix, placement: 'p' }])), load('en').ipv6.providerPrefixes);
  assert.deepEqual(pp.rows, [[GCP.prefix, 'p', '—']]);
});

test('tenants see the list (nothing in it is hidden on tenant pages)', () => {
  const red = panel.redactForTenant('HybridNetwork', network([AWS]));
  assert.deepEqual(red.status.ipv6.providerPrefixes, [AWS]);
});

test('the labels exist in en and fr and use display words', () => {
  for (const l of ['en', 'fr']) {
    const cat = load(l).ipv6;
    for (const k of ['providerPrefixes', 'providerPrefixesNote']) {
      assert.equal(typeof cat[k], 'string', `${l}: ipv6.${k}`);
      assert.doesNotMatch(cat[k], KIND_NAME, `${l}: ipv6.${k}`);
    }
  }
});
