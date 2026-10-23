// Dashboard chart: the oauth-proxy access review is the same for the session cookie path
// (--openshift-sar, OpenShift SubjectAccessReview keys), the bearer-token path
// (--openshift-delegate-urls, Kubernetes ResourceAttributes keys) and the server gate
// (SHC_PLATFORM_SAR). oauth-proxy drops unknown delegate keys, so a delegate entry that
// carries resourceAPIGroup instead of group would review the core API group.
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
  return execFileSync('helm', args, { encoding: 'utf8' });
}

function flag(out, name) {
  const m = out.match(new RegExp(`--${name}=(.*)$`, 'm'));
  return m ? JSON.parse(m[1]) : undefined;
}

function serverSar(out) {
  const m = out.match(/- name: SHC_PLATFORM_SAR\n\s+value: (".*")$/m);
  return m ? JSON.parse(JSON.parse(m[1])) : undefined;
}

const skip = helmAvailable() ? false : 'helm not installed';

test('delegate entry translates resourceAPIGroup to group', { skip }, () => {
  const out = render();
  const sar = flag(out, 'openshift-sar');
  const delegate = flag(out, 'openshift-delegate-urls');
  assert.ok(sar && delegate, 'both review flags rendered');
  const d = delegate['/'];
  assert.ok(d, 'delegate entry for /');
  assert.equal(d.group, sar.resourceAPIGroup);
  assert.equal(d.resource, sar.resource);
  assert.equal(d.verb, sar.verb);
  assert.equal(d.namespace, sar.namespace);
  for (const k of Object.keys(d)) {
    assert.ok(['namespace', 'group', 'resource', 'verb', 'name'].includes(k), `unexpected delegate key ${k}`);
  }
  assert.deepEqual(serverSar(out), sar, 'server gate gets the same review');
});

test('delegate entry translates resourceName to name', { skip }, () => {
  const out = render(['oauthProxy.openshiftSar.resourceName=one']);
  assert.equal(flag(out, 'openshift-sar').resourceName, 'one');
  assert.equal(flag(out, 'openshift-delegate-urls')['/'].name, 'one');
});

test('no review flags when the review is disabled', { skip }, () => {
  const out = render(['oauthProxy.openshiftSar=null']);
  assert.equal(flag(out, 'openshift-sar'), undefined);
  assert.equal(flag(out, 'openshift-delegate-urls'), undefined);
  assert.equal(serverSar(out), undefined);
});
