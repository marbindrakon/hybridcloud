#!/usr/bin/env node
/**
 * Generate the console plugins' console-extensions.json from the navigation
 * registry (ui/packages/shared/src/nav) and the English labels, merged after
 * the hand-kept console-extensions.base.json of each plugin.
 *
 *   node ui/scripts/gen-console-extensions.mjs           write both files
 *   node ui/scripts/gen-console-extensions.mjs --check   exit 1 when a file is out of date
 */
import { execFileSync } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const ui = path.resolve(here, '..');
const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'shc-console-ext-'));
try {
  const out = path.join(tmp, 'consoleExtensions.mjs');
  execFileSync(path.join(ui, 'node_modules/.bin/esbuild'), [
    path.join(ui, 'packages/shared/src/nav/consoleExtensions.ts'),
    '--bundle', '--format=esm', '--platform=node', '--log-level=warning', `--outfile=${out}`,
  ]);
  const { consoleExtensionsFor } = await import(out);
  const en = JSON.parse(fs.readFileSync(path.join(ui, 'packages/shared/src/i18n/locales/en.json'), 'utf8'));
  const label = (key) => {
    const v = key.split('.').reduce((a, k) => (a == null ? a : a[k]), en);
    if (typeof v !== 'string') throw new Error(`missing English label ${key}`);
    return v;
  };
  const plugins = [
    { name: 'tenant', dir: 'tenant-console-plugin', codeRef: 'ShcConsolePage' },
    { name: 'admin', dir: 'admin-console-plugin', codeRef: 'ShcConsolePage' },
  ];
  let stale = 0;
  for (const p of plugins) {
    const dir = path.join(ui, 'packages', p.dir);
    const base = JSON.parse(fs.readFileSync(path.join(dir, 'console-extensions.base.json'), 'utf8'));
    const generated = consoleExtensionsFor(p.name, label, p.codeRef);
    const text = `${JSON.stringify([...base, ...generated], null, 2)}\n`;
    const target = path.join(dir, 'console-extensions.json');
    const current = fs.existsSync(target) ? fs.readFileSync(target, 'utf8') : '';
    if (process.argv.includes('--check')) {
      if (current !== text) {
        console.error(`${target} is out of date: run node ui/scripts/gen-console-extensions.mjs`);
        stale += 1;
      }
    } else {
      fs.writeFileSync(target, text);
      console.log(`wrote ${target} (${base.length} base + ${generated.length} generated)`);
    }
  }
  if (stale) process.exit(1);
  if (process.argv.includes('--check')) console.log('console extensions up to date');
} finally {
  fs.rmSync(tmp, { recursive: true, force: true });
}
