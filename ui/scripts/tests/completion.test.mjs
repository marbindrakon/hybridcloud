// Completion toasts (status/completion.ts): only transitions seen on the page toast.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import path from 'node:path';

const c = await import(path.join(process.env.SHELL_BUNDLE_DIR, 'completion.mjs'));

const obj = (name, phase, extra = {}) => ({ metadata: { uid: `u-${name}`, name }, status: { phase, ...extra } });

test('a first load never toasts; a finished in-flight object does once', () => {
  let r = c.completionEvents(new Map(), [obj('a', 'Creating'), obj('b', 'Ready'), obj('d', 'Deleting')], 'NetworkPlacement');
  assert.deepEqual(r.events.map((e) => e.type), []);
  r = c.completionEvents(r.next, [obj('a', 'Ready'), obj('b', 'Ready')], 'NetworkPlacement');
  assert.deepEqual(r.events, [
    { type: 'ready', name: 'a' },
    { type: 'deleted', name: 'd' },
  ]);
  r = c.completionEvents(r.next, [obj('a', 'Ready'), obj('b', 'Ready')], 'NetworkPlacement');
  assert.deepEqual(r.events, [], 'no repeat');
});

test('a failure carries the summary; Ready to Failed without passing in flight does not toast', () => {
  let r = c.completionEvents(new Map(), [obj('a', 'Updating'), obj('b', 'Ready')], 'CloudVirt');
  r = c.completionEvents(r.next, [obj('a', 'Failed', { summary: 'The site refused it.' }), obj('b', 'Failed')], 'CloudVirt');
  assert.deepEqual(r.events, [{ type: 'failed', name: 'a', summary: 'The site refused it.' }]);
});
