import test from 'node:test';
import assert from 'node:assert/strict';
import { makeWorkflow, normalizeScope } from '../lib/workflow.js';

const result = { ok: true, data: { messages: [], has_more: false, next_cursor: null } };
const tick = () => new Promise(resolve => setImmediate(resolve));
const deferred = () => { let resolve; const promise = new Promise(done => { resolve = done; }); return { promise, resolve }; };

test('all stage-two operations are one direct local operation, never status plus collection', async () => {
  for (const action of ['status', 'search', 'review', 'context', 'conversations', 'get_preferences', 'list_tasks']) {
    const calls = [];
    const run = makeWorkflow(async (op, args) => { calls.push({ op, args }); return result; });
    assert.equal(await run(action, {}), result);
    assert.deepEqual(calls, [{ op: action, args: {} }]);
  }
});

test('stale, uncovered, and empty readable data never trigger hidden preparation or refresh', async () => {
  for (const response of [
    { ok: true, data: { indexed: false, scope_refresh_required: true } },
    { ok: true, data: { messages: [], coverage: 'scope_not_covered', refresh_required: true } },
    { ok: false, error: { code: 'NOT_INDEXED' } },
  ]) {
    for (const action of ['search', 'review']) {
      const calls = [];
      const run = makeWorkflow(async op => { calls.push(op); return response; });
      assert.equal(await run(action, {}), response);
      assert.deepEqual(calls, [action]);
    }
  }
});

test('time and selection parameters are normalized only for local text reads', async () => {
  const calls = [];
  const args = { query: 'synthetic', start_time: '2026-01-02T09:00:00+08:00',
    end_time: '2026-01-02T12:00:00+08:00', kinds: ['group', 'friend', 'group'],
    conversation_ids: ['synthetic-b', 'synthetic-a'], exclude_conversation_ids: ['synthetic-c'] };
  const run = makeWorkflow(async (action, data) => { calls.push({ action, data }); return result; });
  await run('search', args);
  assert.deepEqual(calls, [{ action: 'search', data: { query: 'synthetic', ...normalizeScope(args) } }]);
  assert.deepEqual(args.kinds, ['group', 'friend', 'group']);
});

test('scope normalization preserves empty include arrays for downstream validation', () => {
  assert.deepEqual(normalizeScope({ kinds: [], conversation_ids: [] }), { kinds: [], conversation_ids: [] });
  assert.deepEqual(normalizeScope({ kind: 'group', conversation_id: 'synthetic-a', exclude_conversation_ids: [] }),
    { kinds: ['group'], conversation_ids: ['synthetic-a'], exclude_conversation_ids: [] });
});

test('exclude_muted is forwarded as a non-array scope key for text reads', async () => {
  assert.deepEqual(normalizeScope({ exclude_muted: true }), { exclude_muted: true });
  assert.deepEqual(normalizeScope({ exclude_muted: false, query: 'synthetic' }), { exclude_muted: false });
  assert.deepEqual(normalizeScope({}), {});
  const calls = [];
  const run = makeWorkflow(async (action, data) => { calls.push({ action, data }); return result; });
  await run('search', { query: 'synthetic', exclude_muted: true });
  assert.deepEqual(calls, [{ action: 'search', data: { query: 'synthetic', exclude_muted: true } }]);
  // The key is a scope field, so it is never duplicated into the free-form args.
  assert.deepEqual(calls[0].data, { query: 'synthetic', ...normalizeScope({ exclude_muted: true }) });
});

test('fixed capture rejects every selector and arbitrary extra parameters before any bridge call', async () => {
  for (const action of ['prepare', 'refresh']) {
    for (const args of [{ kind: 'group' }, { exclude_conversation_ids: [] }, { start_date: '2026-01-01' },
      { command: 'synthetic' }, { path: 'synthetic' }, [], null]) {
      let calls = 0;
      const run = makeWorkflow(async () => { calls++; return result; });
      assert.equal((await run(action, args)).error.code, 'INVALID_ARGS');
      assert.equal(calls, 0);
    }
  }
});

test('explicit first stage invokes exactly one fixed operation with empty parameters', async () => {
  for (const action of ['prepare', 'refresh']) {
    const calls = [];
    const run = makeWorkflow(async (op, args) => { calls.push({ op, args }); return result; });
    assert.equal(await run(action, {}), result);
    assert.deepEqual(calls, [{ op: action, args: {} }]);
  }
});

test('parallel identical captures share one fixed operation', async () => {
  const gate = deferred(); const calls = [];
  const run = makeWorkflow(async (action, args) => { calls.push({ action, args }); await gate.promise; return result; });
  const first = run('refresh', {}); const second = run('refresh', {});
  await tick(); assert.deepEqual(calls, [{ action: 'refresh', args: {} }]);
  gate.resolve(); assert.deepEqual(await Promise.all([first, second]), [result, result]);
});

test('prepare never substitutes for refresh and capture operations do not overlap', async () => {
  const gate = deferred(); const calls = []; let active = 0; let peak = 0;
  const run = makeWorkflow(async action => {
    calls.push(action); active++; peak = Math.max(peak, active);
    if (action === 'prepare') await gate.promise;
    active--; return result;
  });
  const first = run('prepare', {}); const second = run('refresh', {});
  await tick(); assert.deepEqual(calls, ['prepare']);
  gate.resolve(); await Promise.all([first, second]);
  assert.deepEqual(calls, ['prepare', 'refresh']); assert.equal(peak, 1);
});

test('a pending capture does not cause text queries to join or start a collection', async () => {
  const gate = deferred(); const calls = [];
  const run = makeWorkflow(async action => { calls.push(action); if (action === 'refresh') await gate.promise; return result; });
  const refreshing = run('refresh', {});
  await tick();
  assert.equal(await run('review', { kinds: ['group'] }), result);
  assert.deepEqual(calls, ['refresh', 'review']);
  gate.resolve(); await refreshing;
});

test('continuation cursor and requested dates are forwarded unchanged without refresh', async () => {
  const calls = [];
  const args = { cursor: 'synthetic-cursor', start_date: '2026-01-01', end_date: '2026-01-02', kind: 'group' };
  const run = makeWorkflow(async (action, data) => { calls.push({ action, data }); return result; });
  await run('review', args);
  assert.deepEqual(calls, [{ action: 'review', data: { cursor: 'synthetic-cursor', ...normalizeScope(args) } }]);
});

test('local writes and preference operations remain direct operations', async () => {
  const calls = [];
  const run = makeWorkflow(async (action, args) => { calls.push({ action, args }); return result; });
  await run('set_preferences', { exclude_conversation_ids: ['synthetic-a'] });
  await run('save_tasks', { tasks: [] });
  assert.deepEqual(calls, [{ action: 'set_preferences', args: { exclude_conversation_ids: ['synthetic-a'] } },
    { action: 'save_tasks', args: { tasks: [] } }]);
});

test('pre-cancelled calls never access data', async () => {
  const controller = new AbortController(); controller.abort();
  const run = makeWorkflow(async () => { assert.fail('must not run'); });
  for (const action of ['status', 'review', 'refresh'])
    assert.equal((await run(action, {}, controller.signal)).error.code, 'CANCELLED');
});

test('cancelled reads cannot return late content', async () => {
  for (const action of ['status', 'review', 'context']) {
    const controller = new AbortController();
    const run = makeWorkflow(async () => { controller.abort(); return result; });
    assert.equal((await run(action, {}, controller.signal)).error.code, 'CANCELLED');
  }
});

test('cancelled shared capture keeps its physical serialization barrier until exit', async () => {
  const gate = deferred(); const calls = []; const controller = new AbortController();
  const run = makeWorkflow(async action => {
    calls.push(action); if (calls.length === 1) await gate.promise; return result;
  });
  const first = run('refresh', {}); const second = run('refresh', {}, controller.signal);
  const third = run('prepare', {});
  await tick(); controller.abort();
  for (const value of await Promise.all([first, second])) assert.equal(value.error.code, 'CANCELLED');
  assert.deepEqual(calls, ['refresh']);
  gate.resolve(); assert.equal(await third, result); assert.deepEqual(calls, ['refresh', 'prepare']);
});

test('failed capture does not poison later explicit operations or get replaced by old data', async () => {
  const calls = []; const failed = { ok: false, error: { code: 'COLLECTOR_FAILED' } };
  const run = makeWorkflow(async action => { calls.push(action); return action === 'refresh' ? failed : result; });
  assert.equal(await run('refresh', {}), failed);
  assert.equal(await run('review', {}), result);
  assert.deepEqual(calls, ['refresh', 'review']);
});

test('thrown capture error does not prevent the next fixed action', async () => {
  const run = makeWorkflow(async action => { if (action === 'prepare') throw new Error('SYNTHETIC_FAILURE'); return result; });
  const outcomes = await Promise.allSettled([run('prepare', {}), run('refresh', {})]);
  assert.equal(outcomes[0].status, 'rejected'); assert.equal(outcomes[1].value, result);
});

