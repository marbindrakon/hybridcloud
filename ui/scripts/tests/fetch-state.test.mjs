// Polling and refresh-failure rules of the list/object hooks (hooks/fetchState.ts),
// driven with fake timers the way useK8sResourceList schedules its refreshes.
import { test, mock } from 'node:test';
import assert from 'node:assert/strict';
import path from 'node:path';

const dir = process.env.SHELL_BUNDLE_DIR;
const fs = await import(path.join(dir, 'fetchState.mjs'));
const { K8sApiError } = await import(path.join(dir, 'errors.mjs'));

const inFlightItem = { metadata: { name: 'p1' }, status: { phase: 'Creating' } };
const readyItem = { metadata: { name: 'p1' }, status: { phase: 'Ready' } };

/**
 * A minimal copy of the hook's loop: fetch, apply the outcome, then schedule
 * the next refresh with pollDelayMs once the request finished.
 */
function startLoop({ url, answers, pollWhileInFlight = true, onState }) {
  let state = fs.initialFetchState(url);
  let calls = 0;
  let timer;
  const tick = () => {
    const answer = answers[Math.min(calls, answers.length - 1)];
    calls += 1;
    const outcome = answer instanceof Error ? { ok: false, error: answer } : { ok: true, data: answer, at: calls };
    state = fs.applyFetchOutcome(state, url, outcome);
    onState(state);
    const items = state.data ?? [];
    const inFlight = items.some((i) => i.status?.phase === 'Creating' || i.status?.phase === 'Deleting');
    const delay = fs.pollDelayMs({ pollWhileInFlight, inFlight });
    if (delay) timer = setTimeout(tick, delay);
  };
  tick();
  return {
    get calls() {
      return calls;
    },
    stop: () => clearTimeout(timer),
  };
}

test('a transient refresh failure keeps the rows and reports refreshError; the next poll clears it', () => {
  mock.timers.enable({ apis: ['setTimeout'] });
  try {
    const states = [];
    const loop = startLoop({
      url: '/api/networkplacements?namespace=entity-a',
      answers: [[inFlightItem], new K8sApiError(503, 'upstream down'), new K8sApiError(0, 'timeout'), [readyItem]],
      onState: (s) => states.push(s),
    });
    assert.equal(states.length, 1);
    assert.deepEqual(states[0].data, [inFlightItem]);
    // In flight: the next refresh comes after 5 s, not earlier.
    mock.timers.tick(4999);
    assert.equal(loop.calls, 1);
    mock.timers.tick(1);
    assert.equal(loop.calls, 2);
    assert.deepEqual(states[1].data, [inFlightItem], 'rows stay after a 503');
    assert.equal(states[1].error, null);
    assert.equal(states[1].refreshError.status, 503);
    mock.timers.tick(5000);
    assert.deepEqual(states[2].data, [inFlightItem], 'rows stay after a network error');
    assert.equal(states[2].refreshError.status, 0);
    mock.timers.tick(5000);
    assert.deepEqual(states[3].data, [readyItem]);
    assert.equal(states[3].refreshError, null);
    // Nothing in flight any more: the next refresh is 30 s away.
    mock.timers.tick(29999);
    assert.equal(loop.calls, 4);
    mock.timers.tick(1);
    assert.equal(loop.calls, 5);
    loop.stop();
  } finally {
    mock.timers.reset();
  }
});

test('the first load failing shows the error (full-page state)', () => {
  const s = fs.applyFetchOutcome(fs.initialFetchState('/u'), '/u', { ok: false, error: new K8sApiError(503, 'down') });
  assert.equal(s.data, undefined);
  assert.equal(s.error.status, 503);
  assert.equal(s.refreshError, null);
});

test('403, 404 and 401 replace the rows even after a good load', () => {
  for (const status of [401, 403, 404]) {
    let s = fs.applyFetchOutcome(fs.initialFetchState('/u'), '/u', { ok: true, data: [readyItem] });
    s = fs.applyFetchOutcome(s, '/u', { ok: false, error: new K8sApiError(status, 'no') });
    assert.equal(s.data, undefined, String(status));
    assert.equal(s.error.status, status);
  }
});

test('a new URL never shows the previous URL rows; late answers for an old URL are dropped into a fresh state', () => {
  let s = fs.applyFetchOutcome(fs.initialFetchState('/a'), '/a', { ok: true, data: [readyItem] });
  // The page moved to /b; a failure for /b must not inherit /a's rows.
  s = fs.applyFetchOutcome(s, '/b', { ok: false, error: new K8sApiError(503, 'down') });
  assert.equal(s.key, '/b');
  assert.equal(s.data, undefined);
  assert.equal(s.error.status, 503);
});

test('pollDelayMs: explicit interval wins, in-flight 5 s, idle 30 s, hidden or not asked 0', () => {
  assert.equal(fs.pollDelayMs({ pollIntervalMs: 7000, pollWhileInFlight: true, inFlight: true }), 7000);
  assert.equal(fs.pollDelayMs({ pollWhileInFlight: true, inFlight: true }), fs.IN_FLIGHT_POLL_MS);
  assert.equal(fs.pollDelayMs({ pollWhileInFlight: true, inFlight: false }), fs.IDLE_POLL_MS);
  assert.equal(fs.pollDelayMs({ pollWhileInFlight: true, inFlight: true, hidden: true }), 0);
  assert.equal(fs.pollDelayMs({ inFlight: true }), 0);
});
