// Admin dashboard chart (PGT-F1 third case, OD46): the oauth-proxy serves its own sign-in and error
// pages from a ConfigMap (--custom-templates-dir). A signed-in user who fails the access review gets
// the product's page: the role that grants access and that the platform team adds members, with no
// tenant contacts.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import path from 'node:path';

const chart = path.resolve(process.env.UI_ROOT, '..', 'charts', 'charts', 'sovereign-cloud-dashboard');

function helmAvailable() {
  try {
    execFileSync('helm', ['version', '--short'], { stdio: 'ignore' });
    return true;
  } catch {
    return false;
  }
}

function render(sets = []) {
  const args = ['template', 't', chart];
  for (const s of sets) args.push('--set', s);
  return execFileSync('helm', args, { encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] });
}

/** The ConfigMap's files: `name: |` blocks under data. */
function pages(out) {
  const doc = out.split(/^---$/m).find((d) => /kind: ConfigMap/.test(d) && /-oauth-pages\n/.test(d));
  if (!doc) return null;
  const files = {};
  const re = /^ {2}([a-z_]+\.html): \|\n((?: {4}.*\n|\n)*)/gm;
  let m;
  while ((m = re.exec(doc))) files[m[1]] = m[2].replace(/^ {4}/gm, '');
  return files;
}

const skip = helmAvailable() ? false : 'helm not installed';

test('the proxy reads both pages from the mounted ConfigMap', { skip }, () => {
  const out = render();
  const files = pages(out);
  assert.ok(files, 'ConfigMap rendered');
  assert.deepEqual(Object.keys(files).sort(), ['error.html', 'sign_in.html'], 'oauth-proxy needs both files');
  assert.match(out, /- --custom-templates-dir=\/etc\/oauth-proxy\/pages$/m);
  assert.match(out, /- name: oauth-pages\n\s+mountPath: \/etc\/oauth-proxy\/pages/);
  assert.match(out, /oauth-pages-checksum: "[0-9a-f]{64}"/, 'a page change restarts the pod');
  for (const f of Object.values(files)) assert.doesNotMatch(f, /@@[A-Z_]+@@/, 'every placeholder is filled');
});

test('the denial names the granting role and the platform team, and no tenant contacts', { skip }, () => {
  const { 'error.html': err } = pages(render());
  // the branch the proxy takes after a failed access review (403 "Invalid Account")
  assert.match(err, /\{\{- if and \(eq \.Title "403 Permission Denied"\) \(eq \.Message "Invalid Account"\) \}\}/);
  const start = err.indexOf('}}', err.indexOf('{{- if and', err.indexOf('<main'))) + 2;
  const denial = err.slice(start, err.indexOf('{{- else }}', start));
  assert.match(denial, /<strong>Platform administrator<\/strong> role grants access/);
  assert.match(denial, /The platform team adds members/);
  assert.doesNotMatch(denial, /contact/i, 'OD46: no contacts');
  assert.doesNotMatch(denial, /Invalid Account|Permission Denied/, 'not the stock wording');
  assert.match(denial, /href="\{\{\.ProxyPrefix\}\}\/sign_out">Sign out</);
  // other errors keep the status and the detail
  assert.match(err, /<dd>\{\{\.Title\}\}<\/dd>/);
});

test('the role, the product and the tenant dashboard link come from values', { skip }, () => {
  const files = pages(render([
    'oauthProxy.pages.grantingRole=Platform operator',
    'oauthProxy.pages.productName=Example Console',
    'oauthProxy.pages.tenantDashboardUrl=https://tenants.example.com/',
  ]));
  assert.match(files['error.html'], /<strong>Platform operator<\/strong>/);
  assert.match(files['sign_in.html'], /<title>Sign in · Example Console<\/title>/);
  for (const f of Object.values(files)) assert.match(f, /<a href="https:\/\/tenants\.example\.com\/">Open the tenant dashboard<\/a>/);
  // no link by default (no installation-specific default)
  assert.doesNotMatch(pages(render())['error.html'], /Open the tenant dashboard/);
  // values that would break the page are refused
  for (const bad of ['oauthProxy.pages.tenantDashboardUrl=javascript:alert(1)', 'oauthProxy.pages.grantingRole=<b>x</b>']) {
    assert.throws(() => render([bad]));
  }
});

test('pages off: the stock proxy pages, nothing mounted', { skip }, () => {
  const out = render(['oauthProxy.pages.enabled=false']);
  assert.equal(pages(out), null);
  assert.doesNotMatch(out, /custom-templates-dir|oauth-pages/);
});
