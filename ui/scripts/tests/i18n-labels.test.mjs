// Display names and labels (Q13): every label key the registry uses resolves to a
// string in en and fr (kinds.<Kind> stays a string: the plural list title), every
// kind has a singular display name, and the two catalogues have the same keys.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';

const UI = process.env.UI_ROOT;
const reg = await import(path.join(process.env.SHELL_BUNDLE_DIR, 'registry.mjs'));
const load = (l) => JSON.parse(fs.readFileSync(path.join(UI, `packages/shared/src/i18n/locales/${l}.json`), 'utf8'));
const en = load('en');
const fr = load('fr');
const get = (o, k) => k.split('.').reduce((n, p) => (n && typeof n === 'object' ? n[p] : undefined), o);

function leaves(o, prefix = '') {
  return Object.entries(o).flatMap(([k, v]) => (v && typeof v === 'object' ? leaves(v, `${prefix}${k}.`) : [`${prefix}${k}`]));
}

test('every registry and section label resolves to a string in en and fr', () => {
  const keys = [...reg.NAV_REGISTRY.map((e) => e.labelKey), ...reg.NAV_SECTIONS.map((s) => s.labelKey)];
  for (const k of keys) {
    for (const [name, cat] of [['en', en], ['fr', fr]]) {
      assert.equal(typeof get(cat, k), 'string', `${name}: ${k}`);
    }
  }
});

test('kinds.<Kind> and kindsOne.<Kind> are strings for every kind', () => {
  for (const kind of Object.keys(en.kinds)) {
    assert.equal(typeof en.kinds[kind], 'string', `kinds.${kind}`);
    assert.equal(typeof fr.kinds?.[kind], 'string', `fr kinds.${kind}`);
  }
  for (const kind of Object.keys(en.kindsOne)) {
    assert.equal(typeof en.kindsOne[kind], 'string', `kindsOne.${kind}`);
    assert.equal(typeof fr.kindsOne?.[kind], 'string', `fr kindsOne.${kind}`);
  }
});

test('en and fr have the same keys under the stream-owned prefixes', () => {
  const owned = ['kinds', 'kindsOne', 'status', 'phase', 'form', 'fields', 'detail', 'list', 'attach', 'callout', 'toasts', 'wayin', 'sites', 'review', 'durations'];
  for (const p of owned) {
    if (!en[p]) continue;
    const a = new Set(leaves(en[p]));
    const b = new Set(leaves(fr[p] ?? {}));
    assert.deepEqual([...a].filter((k) => !b.has(k)), [], `${p}: missing in fr`);
    assert.deepEqual([...b].filter((k) => !a.has(k)), [], `${p}: missing in en`);
  }
});
