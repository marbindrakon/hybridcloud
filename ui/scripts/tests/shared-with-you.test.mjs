// Per-object grants (GT-F4): the rules review folded into names per kind.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import path from 'node:path';

const m = await import(path.join(process.env.SHELL_BUNDLE_DIR, 'sharedWithYou.mjs'));
const G = 'hybridsovereign.redhat';

test('only rules with resourceNames and get count; subresources and other groups do not', () => {
  const out = m.sharedNamesFromRules([
    { apiGroups: [G], resources: ['teams'], resourceNames: ['platform'], verbs: ['get', 'watch'] },
    { apiGroups: [G], resources: ['projects', 'projects/status'], resourceNames: ['webshop', 'api'], verbs: ['get', 'update'] },
    { apiGroups: [G], resources: ['projects'], resourceNames: ['webshop'], verbs: ['get'] },
    { apiGroups: [G], resources: ['cloudvirts'], verbs: ['get', 'list'] },
    { apiGroups: [G], resources: ['assignments'], resourceNames: ['a1'], verbs: ['update'] },
    { apiGroups: ['rbac.authorization.k8s.io'], resources: ['roles'], resourceNames: ['x'], verbs: ['get'] },
  ]);
  assert.deepEqual(out, { teams: ['platform'], projects: ['api', 'webshop'] });
});

test('wildcards in groups and verbs count', () => {
  assert.deepEqual(m.sharedNamesFromRules([{ apiGroups: ['*'], resources: ['teams'], resourceNames: ['t'], verbs: ['*'] }]), { teams: ['t'] });
  assert.deepEqual(m.sharedNamesFromRules(undefined), {});
});
