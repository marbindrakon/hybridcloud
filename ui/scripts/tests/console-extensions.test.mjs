// Generated console extensions: consistent sections, hrefs that have routes, no duplicate routes.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';

const UI = process.env.UI_ROOT;
const load = (p) => JSON.parse(fs.readFileSync(`${UI}/packages/${p}-console-plugin/console-extensions.json`, 'utf8'));
const tenant = load('tenant');
const admin = load('admin');
const all = [...tenant, ...admin];
const routes = all.filter((e) => e.type === 'console.page/route').map((e) => e.properties.path);
const toRegex = (p) => new RegExp(`^${p.replace(/:[a-zA-Z]+/g, '[^/]+')}$`);

test('no duplicate route paths across the two plugins', () => {
  assert.equal(new Set(routes).size, routes.length, routes.filter((r, i) => routes.indexOf(r) !== i).join(', '));
});

test('every nav section referenced is declared once, by the plugin that uses it', () => {
  for (const [name, ext] of [['tenant', tenant], ['admin', admin]]) {
    const sections = ext.filter((e) => e.type === 'console.navigation/section').map((e) => e.properties.id);
    assert.equal(new Set(sections).size, sections.length, name);
    for (const e of ext.filter((x) => x.type === 'console.navigation/href' && x.properties.section)) {
      assert.ok(sections.includes(e.properties.section), `${name}: ${e.properties.id} -> ${e.properties.section}`);
    }
  }
});

test('every nav href resolves to a route (namespaced hrefs get /ns/<ns> or /all-namespaces)', () => {
  const rx = routes.map(toRegex);
  for (const e of all.filter((x) => x.type === 'console.navigation/href')) {
    const h = e.properties.href;
    const forms = e.properties.namespaced ? [`${h}/ns/entity-x`, `${h}/all-namespaces`] : [h];
    for (const f of forms) assert.ok(rx.some((r) => r.test(f)), `${e.properties.id}: ${f}`);
  }
});

test('platform items are flag-gated; tenant items are namespaced; no retired pages', () => {
  for (const e of admin.filter((x) => x.type.startsWith('console.navigation/'))) {
    assert.deepEqual(e.flags?.required, ['HYBRIDSOVEREIGN_PLATFORM_ADMIN'], e.properties.id);
  }
  for (const e of tenant.filter((x) => x.type === 'console.navigation/href')) assert.equal(e.properties.namespaced, true, e.properties.id);
  const names = all.map((e) => String(e.properties.name || '')).join(' | ');
  for (const bad of ['Personas', 'Transport Links', 'Admin Monitor', 'Service URLs', 'Operators', '(all entities)', 'Network Placements']) {
    assert.ok(!names.includes(bad), bad);
  }
});

test('the plugins expose only the modules the extensions use', () => {
  for (const p of ['tenant', 'admin']) {
    const pkg = JSON.parse(fs.readFileSync(`${UI}/packages/${p}-console-plugin/package.json`, 'utf8'));
    const exposed = Object.keys(pkg.consolePlugin.exposedModules);
    const used = new Set(JSON.stringify(load(p)).match(/"\$codeRef":\s*"([^".]+)/g).map((m) => m.replace(/.*"([^"]+)$/, '$1')));
    for (const u of used) assert.ok(exposed.includes(u), `${p}: ${u} not exposed`);
    for (const x of exposed) assert.ok(used.has(x), `${p}: ${x} exposed but unused`);
  }
});
