#!/usr/bin/env node
// The labels the UI builds at runtime, as JSON on stdout, for the runtime check of
// scripts/check-content.py (PGT-F2): every kind term in every form, an object reference
// of every kind, API text naming every kind, the catalogue templates that take a kind,
// and the field label of every field path given on the command line.
//
//   node ui/scripts/runtime-strings.mjs <paths.json>
//
// <paths.json>: {"fieldPaths": ["spec.network", ...]}. Output: [{"source", "text", "kind"}]; `kind` is
// the API kind a string names, so the check can keep to the kinds a tenant meets.
// The vocabulary module (ui/packages/shared/src/i18n/vocabulary.ts) is bundled with the
// esbuild of ui/node_modules; the lookups use the English catalogue the way i18next does.
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const ui = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const esbuild = await import(pathToFileURL(path.join(ui, 'node_modules/esbuild/lib/main.js')).href);
const built = await esbuild.build({
  entryPoints: [path.join(ui, 'packages/shared/src/i18n/vocabulary.ts')],
  bundle: true,
  format: 'esm',
  platform: 'node',
  write: false,
  logLevel: 'warning',
});
const vocab = await import(`data:text/javascript;base64,${Buffer.from(built.outputFiles[0].text).toString('base64')}`);

const en = JSON.parse(fs.readFileSync(path.join(ui, 'packages/shared/src/i18n/locales/en.json'), 'utf8'));
const input = process.argv[2] ? JSON.parse(fs.readFileSync(process.argv[2], 'utf8')) : {};

/** i18next-like lookup over the English catalogue: nested keys, defaultValue, {{x}} interpolation. */
export function t(key, opts = {}) {
  const v = key.split('.').reduce((n, p) => (n && typeof n === 'object' ? n[p] : undefined), en);
  const s = typeof v === 'string' ? v : opts.defaultValue ?? key;
  return String(s).replace(/\{\{\s*(\w+)\s*\}\}/g, (_m, k) => (opts[k] !== undefined ? String(opts[k]) : ''));
}

const out = [];
const add = (source, text, kind) => {
  if (text) out.push(kind ? { source, text, kind } : { source, text });
};

for (const kind of vocab.API_KINDS) {
  for (const form of ['one', 'many', 'dense']) {
    add(`kindTerm(${kind}, ${form})`, vocab.kindTerm(t, kind, form), kind);
    add(`kindTermInText(${kind}, ${form})`, vocab.kindTermInText(t, kind, form), kind);
  }
  add(`objectLabel(${kind}/x)`, vocab.objectLabel(t, `${kind}/x`), kind);
  add(`objectLabel(${kind} dense)`, vocab.objectLabel(t, { kind, name: 'x' }, { dense: true }), kind);
  add(`displayText(${kind} ref)`, vocab.displayText(t, `waits for ${kind}/x and ${kind} / y`), kind);
  add(`displayText(${kind} word)`, vocab.displayText(t, `network a via ${kind} b; two ${kind}s`), kind);
}
add('kindTerm(unknown)', vocab.kindTerm(t, 'NoSuchKind'));
// The deletionBlockedBy formats the teardowns write, labelled the way StatusWaitBlocks and the
// Deleting callout label them (hybridnetwork_teardown, networkplacement_teardown, cloudvirt_teardown);
// `kind` is the tenant kind whose page shows the entry.
for (const [kind, text] of [
  ['HybridNetwork', 'HybridFabric ns/x (internet egress)'],
  ['NetworkPlacement', 'CloudGateway ns/x (VRF y)'],
  ['CloudVirt', 'VirtualMachine ns/x'],
]) {
  add(`objectLabel(${text})`, vocab.objectLabel(t, text, { dense: true }), kind);
  add(`objectLabel(${text}, no kind)`, vocab.objectLabel(t, { name: text }), kind);
}
add('objectLabel(unknown)', vocab.objectLabel(t, 'NoSuchKind/x'));

// Catalogue templates that take a kind, filled the way the pages fill them.
function templates(o, prefix = '') {
  return Object.entries(o).flatMap(([k, v]) =>
    v && typeof v === 'object' ? templates(v, `${prefix}${k}.`) : typeof v === 'string' && /\{\{\s*kind\s*\}\}/.test(v) ? [`${prefix}${k}`] : [],
  );
}
for (const key of templates(en)) {
  for (const kind of vocab.API_KINDS) {
    add(`${key}(${kind})`, t(key, { kind: vocab.kindTermInText(t, kind, 'many'), name: 'x', namespace: 'ns', tenant: 'Example', site: 's' }), kind);
  }
}

for (const p of input.fieldPaths ?? []) add(`fieldLabel(${p})`, vocab.fieldLabel(t, p));
add('tenantCaption', vocab.tenantCaption(t, 'example-corp'));

process.stdout.write(JSON.stringify(out));
