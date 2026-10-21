// forms/entityFields.ts: the Entity form's display name, contacts and allowed sites (UFS-F9).
import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';

const dir = process.env.SHELL_BUNDLE_DIR;
const ef = await import(path.join(dir, 'entityFields.mjs'));

const site = (name, extra = {}) => ({
  apiVersion: 'hybridsovereign.redhat/v1alpha1',
  kind: 'CloudInfrastructure',
  metadata: { name, namespace: 'sovereign-cloud' },
  spec: { type: 'openshift', ...extra },
});

test('cleanContacts drops empty rows, trims, and flags the CRD rules', () => {
  const r = ef.cleanContacts([
    {},
    { name: '  Ops desk ', email: ' ops@example.com ' },
    { email: 'nobody@example.com' },
    { name: 'Queue', url: 'http://insecure.example.com' },
    { name: 'Mail', email: 'not-an-address' },
  ]);
  assert.deepEqual(r.contacts, [
    { name: 'Ops desk', email: 'ops@example.com' },
    { name: '', email: 'nobody@example.com' },
    { name: 'Queue', url: 'http://insecure.example.com' },
    { name: 'Mail', email: 'not-an-address' },
  ]);
  assert.deepEqual(r.problems, [[], ['nameRequired'], ['url'], ['email']]);
  assert.equal(ef.contactsValid(r), false);
  assert.equal(ef.contactsValid(ef.cleanContacts([{ name: 'A', url: 'https://desk.example.com' }])), true);
  assert.equal(ef.contactsValid(ef.cleanContacts([])), true);
  const many = ef.cleanContacts(Array.from({ length: 11 }, (_, i) => ({ name: `c${i}` })));
  assert.deepEqual(many.listProblems, ['tooMany']);
});

test('sitesForTenant offers open sites and the ones listing the tenant only', () => {
  const items = [
    site('zeta'),
    site('alpha', { entityRefs: [{ name: 'acme' }] }),
    site('beta', { entityRefs: [{ name: 'other' }] }),
    { metadata: { name: 'nospec' } },
  ];
  const r = ef.sitesForTenant(items, 'acme');
  assert.deepEqual(r.offered.map((s) => s.metadata.name), ['alpha', 'zeta']);
  assert.deepEqual(r.notListing, ['beta']);
  // A tenant still being named: restricted sites are not offered.
  assert.deepEqual(ef.sitesForTenant(items, '').offered.map((s) => s.metadata.name), ['zeta']);
});

test('siteCatalogueLabel names the site, its location and type', () => {
  assert.equal(ef.siteCatalogueLabel(site('s1')), 's1 (openshift)');
  assert.equal(ef.siteCatalogueLabel(site('s1', { displayName: 'North', location: 'Building 2' })), 'North [s1] (Building 2, openshift)');
});

test('entityOptionalSpec leaves out empty fields (allowedSites has minItems 1)', () => {
  assert.deepEqual(ef.entityOptionalSpec({ displayName: '  ', contacts: [], allowedSites: [] }), {});
  assert.deepEqual(ef.entityOptionalSpec({ displayName: ' Acme ', contacts: [{ name: 'A' }], allowedSites: ['b', 'a', 'b', ' '] }), {
    displayName: 'Acme',
    contacts: [{ name: 'A' }],
    allowedSites: ['a', 'b'],
  });
});

test('ENTITY_LIMITS match the Entity CRD', () => {
  const crd = fs.readFileSync(path.join(process.env.UI_ROOT, '..', 'operator', 'config', 'crd', 'bases', 'crd-entity.yaml'), 'utf8');
  const after = (key, n = 1200) => crd.slice(crd.indexOf(`${key}:`), crd.indexOf(`${key}:`) + n);
  assert.match(after('displayName', 300), new RegExp(`maxLength: ${ef.ENTITY_LIMITS.displayNameMax}\\b`));
  assert.match(after('contacts', 300), new RegExp(`maxItems: ${ef.ENTITY_LIMITS.contactsMax}\\b`));
  assert.match(after('allowedSites', 600), new RegExp(`maxItems: ${ef.ENTITY_LIMITS.allowedSitesMax}\\b`));
  assert.match(crd, new RegExp(`self\\.metadata\\.name\\.size\\(\\) <= ${ef.ENTITY_LIMITS.nameMax}\\b`));
});
