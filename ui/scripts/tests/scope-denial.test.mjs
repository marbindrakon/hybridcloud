// Scope-level denials (PGT-F1): a tenant link for a non-member and a platform page for a tenant
// name who grants access and whom to ask, like the forbidden pages inside a tenant.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';

const m = await import(path.join(process.env.SHELL_BUNDLE_DIR, 'errorText.mjs'));
const en = JSON.parse(fs.readFileSync(path.join(process.env.UI_ROOT, 'packages/shared/src/i18n/locales/en.json'), 'utf8'));
const t = (key, opts = {}) => {
  const s = key.split('.').reduce((n, p) => (n && typeof n === 'object' ? n[p] : undefined), en);
  return String(typeof s === 'string' ? s : key).replace(/\{\{\s*(\w+)\s*\}\}/g, (_x, k) => String(opts[k] ?? ''));
};

test('a non-member on a tenant link is told which role adds people and whom to ask', () => {
  const out = m.scopeDenialText(t, { requested: 'tenant', ownTenants: [] });
  assert.deepEqual(out, [
    "Membership in one of the tenant's access groups grants it; the Tenant administrator role adds people to them.",
    "Ask the tenant's administrators or the platform team to add you to one.",
  ]);
});

test('a tenant user on a platform page gets the platform roles and the contacts of their own tenant', () => {
  const out = m.scopeDenialText(t, {
    requested: 'platform',
    ownTenants: [{ label: 'Example Corp', contacts: [{ name: 'Cloud desk', email: 'cloud@example.com' }] }, { label: 'No Contacts' }],
  });
  assert.deepEqual(out, [
    'Platform administrator or Platform viewer grants it.',
    'Contacts of Example Corp: Cloud desk cloud@example.com.',
  ]);
});

test('without any contacts the platform page says to ask the platform team', () => {
  assert.deepEqual(m.scopeDenialText(t, { requested: 'all', ownTenants: [] }), [
    'Platform administrator or Platform viewer grants it.',
    'Ask the platform team.',
  ]);
});
