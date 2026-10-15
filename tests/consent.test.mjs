import test from 'node:test';
import assert from 'node:assert/strict';
import { approve, initializeConsent } from '../lib/consent.js';

const spec = action => ({ action, name: `wechat_${action}` });
const tick = () => new Promise(resolve => setImmediate(resolve));
const deferred = () => { let resolve; const promise = new Promise(done => { resolve = done; }); return { promise, resolve }; };

function harness(handler = async () => 'allowed-once', tracked = true) {
  const requests = [];
  const listeners = new Map();
  const effects = [];
  let service = { request: async request => {
    requests.push(request);
    return handler(request, requests.length);
  } };
  let presets;
  const ctx = {
    get: name => name === 'approval' ? service : name === 'permissionPresets' ? presets : undefined,
    on(name, callback) {
      const callbacks = listeners.get(name) || new Set();
      callbacks.add(callback); listeners.set(name, callbacks);
      return () => callbacks.delete(callback);
    },
    effect(factory) { const cleanup = factory(); effects.push(cleanup); return cleanup; },
  };
  if (!tracked) { delete ctx.on; delete ctx.effect; }
  const emit = (name, ...args) => { for (const callback of listeners.get(name) || []) callback(...args); };
  const session = () => ({
    seq: 0,
    append(type, data = {}) {
      const event = { seq: this.seq++, type, data };
      emit('session/event', this, event);
      return event;
    },
  });
  const agent = { session: session() };
  initializeConsent(ctx);
  const run = (action = 'review', { signal, target = agent, config = {} } = {}) => approve(ctx, spec(action), {
    agent: target, name: `wechat_${action}`, callId: 'synthetic-call', signal,
  }, config);
  const start = (target = agent) => target.session.append('turn/start');
  return { ctx, requests, agent, session, run, start, emit,
    dispose: () => { for (const cleanup of effects) cleanup(); },
    setService: value => { service = value; }, setPresets: value => { presets = value; } };
}

test('effective Auto skips only redundant plugin prompts for fixed collection and local reads', async () => {
  const h = harness(); h.start();
  let reads = 0;
  h.setPresets({ current(session) { assert.equal(session, h.agent.session); reads++; return 'auto'; } });
  for (const action of ['prepare', 'refresh', 'search', 'review', 'context', 'list_tasks', 'conversations', 'get_preferences'])
    assert.equal(await h.run(action), true);
  assert.equal(reads, 8);
  assert.equal(h.requests.length, 0);
  // Direct approval-service availability does not change the host's effective
  // Auto result; the separate host pre-execute gate owns that lifecycle.
  h.setService(undefined);
  assert.equal(await h.run('refresh'), true);
  assert.equal(await h.run('review'), true);
});

test('Auto still requires a separate approval for every task or preference write', async () => {
  const h = harness(); h.start(); h.setPresets({ current: () => 'auto' });
  for (const action of ['save_tasks', 'set_preferences', 'save_tasks', 'set_preferences'])
    assert.equal(await h.run(action), true);
  assert.equal(h.requests.length, 4);
  h.setService(undefined);
  assert.equal(await h.run('save_tasks'), false);
  assert.equal(await h.run('set_preferences'), false);
});

test('Auto must come from the effective host preset, never ask policy or untrusted agent fields', async () => {
  for (const presets of [undefined, {}, { current: () => 'AUTO' }, { current: () => 'custom' },
    { current: () => 'danger-full-access' }, { current: () => true },
    { current() { throw new Error('SYNTHETIC_PRIVATE_DETAIL'); } }]) {
    const h = harness(); h.start(); h.setPresets(presets);
    h.agent.mode = 'auto'; h.agent.permissionPreset = 'auto';
    assert.equal(await h.run('review'), true);
    assert.equal(h.requests.length, 1);
  }
});

test('Auto does not admit agentless, sessionless, unknown, cancelled or disposed calls', async () => {
  const h = harness(); h.start(); h.setPresets({ current: () => 'auto' });
  assert.equal(await approve(h.ctx, spec('review'), {}), false);
  h.setService(undefined);
  assert.equal(await h.run('review', { target: {} }), false);
  assert.equal(await h.run('synthetic_unknown'), false);
  const controller = new AbortController(); controller.abort();
  assert.equal(await h.run('refresh', { signal: controller.signal }), false);
  assert.equal(await h.run('review', { signal: {} }), false);
  h.dispose();
  assert.equal(await h.run('review'), false);
  assert.equal(h.requests.length, 0);
});

test('switching out of Auto requires manual permission and cannot reuse an older grant', async () => {
  const h = harness(); h.start();
  let mode = 'workspace-write'; h.setPresets({ current: () => mode });
  assert.equal(await h.run('review'), true);
  assert.equal(h.requests.length, 1);
  mode = 'auto'; assert.equal(await h.run('review'), true);
  mode = 'workspace-write'; assert.equal(await h.run('review'), true);
  assert.equal(h.requests.length, 2);
  mode = 'auto'; assert.equal(await h.run('refresh'), true);
  h.setPresets(undefined); assert.equal(await h.run('refresh'), true);
  assert.equal(h.requests.length, 3);
});

test('one grant covers refresh, prepare and all read pages in one observed turn', async () => {
  const h = harness(); h.start();
  for (const action of ['refresh', 'prepare', 'search', 'review', 'context', 'list_tasks', ...Array(12).fill('review')])
    assert.equal(await h.run(action), true);
  assert.equal(h.requests.length, 1);
  const reason = h.requests[0].reason;
  for (const text of ['本轮', '分页', '刷新', '进程和数据库', '真实消息', '联系人', '单独确认', '不更改 DSH 全局权限'])
    assert.ok(reason.includes(text));
});

test('read-first grant explicitly includes later native refresh', async () => {
  const h = harness(); h.start();
  assert.equal(await h.run('search'), true);
  assert.equal(await h.run('refresh'), true);
  assert.equal(h.requests.length, 1);
  assert.ok(h.requests[0].reason.includes('原生连接'));
});

test('save tasks always asks separately and never creates a read grant', async () => {
  const h = harness(); h.start();
  for (const action of ['save_tasks', 'save_tasks', 'search', 'save_tasks', 'review'])
    assert.equal(await h.run(action), true);
  assert.equal(h.requests.length, 4);
  assert.ok(h.requests[0].reason.includes('每次单独确认'));
});

test('status needs no approval while agentless reads fail closed even with confirmReads false', async () => {
  const h = harness(); h.setService(undefined);
  assert.equal(await approve(h.ctx, spec('status'), {}, {}), true);
  for (const action of ['search', 'prepare', 'refresh', 'save_tasks'])
    assert.equal(await approve(h.ctx, spec(action), {}, { confirmReads: false }), false);
  assert.equal(h.requests.length, 0);
});

test('confirmReads false preserves read opt-out but does not grant native access or save', async () => {
  const h = harness(); h.start(); h.setService(undefined);
  for (const action of ['search', 'review', 'context', 'list_tasks'])
    assert.equal(await h.run(action, { config: { confirmReads: false } }), true);
  for (const action of ['prepare', 'refresh', 'save_tasks'])
    assert.equal(await h.run(action, { config: { confirmReads: false } }), false);
});

test('unknown actions fail closed even when read confirmation is disabled', async () => {
  const h = harness(); h.start();
  assert.equal(await h.run('synthetic_unknown', { config: { confirmReads: false } }), false);
  assert.equal(h.requests.length, 0);
});

test('exact allowed-once is required and denial cannot be cached', async () => {
  for (const answer of ['allowed', 'allowed-always', 'rejected', 'unavailable', undefined, true, { kind: 'allowed-once' }]) {
    const h = harness(async (_request, count) => count === 1 ? answer : 'allowed-once'); h.start();
    assert.equal(await h.run(), false);
    assert.equal(await h.run(), true);
    assert.equal(h.requests.length, 2);
  }
});

test('approval exceptions never cache permission or surface private details', async () => {
  const h = harness(async (_request, count) => {
    if (count === 1) throw new Error('SYNTHETIC_PRIVATE_DETAIL');
    return 'allowed-once';
  }); h.start();
  assert.equal(await h.run(), false);
  assert.equal(await h.run(), true);
  assert.equal(h.requests.length, 2);
});

test('parallel read and native calls share exactly one pending approval', async () => {
  const gate = deferred(); const h = harness(() => gate.promise); h.start();
  const results = ['review', 'context', 'refresh', 'search'].map(action => h.run(action));
  await tick(); assert.equal(h.requests.length, 1);
  gate.resolve('allowed-once');
  assert.deepEqual(await Promise.all(results), [true, true, true, true]);
  assert.equal(await h.run('review'), true);
  assert.equal(h.requests.length, 1);
});

test('one cancelled participant invalidates the shared prompt and late callback', async () => {
  const gate = deferred();
  const h = harness((_request, count) => count === 1 ? gate.promise : 'allowed-once'); h.start();
  const first = h.run();
  const controller = new AbortController();
  const second = h.run('context', { signal: controller.signal });
  await tick(); controller.abort();
  assert.deepEqual(await Promise.all([first, second]), [false, false]);
  assert.equal(h.requests[0].signal.aborted, true);
  gate.resolve('allowed-once'); await tick();
  assert.equal(await h.run(), true);
  assert.equal(h.requests.length, 2);
});

test('already aborted call cannot request or retain a prior grant', async () => {
  const h = harness(); h.start();
  assert.equal(await h.run(), true);
  const controller = new AbortController(); controller.abort();
  assert.equal(await h.run('review', { signal: controller.signal }), false);
  assert.equal(h.requests.length, 1);
  assert.equal(await h.run(), true);
  assert.equal(h.requests.length, 2);
});

test('turn end and next start expire the previous grant', async () => {
  const h = harness(); h.start(); assert.equal(await h.run(), true);
  h.agent.session.append('turn/end'); h.start();
  assert.equal(await h.run(), true);
  assert.equal(h.requests.length, 2);
});

test('new user input within a runtime turn expires permission, pure claim removal does not', async () => {
  const h = harness(); h.start();
  h.agent.session.append('agent/inbox/spliced', { inserted: [], removedCount: 1 });
  assert.equal(await h.run(), true);
  h.agent.session.append('agent/inbox/spliced', { inserted: [], removedCount: 1 });
  assert.equal(await h.run(), true); assert.equal(h.requests.length, 1);
  h.agent.session.append('agent/inbox/spliced', { inserted: [{}] });
  assert.equal(await h.run(), true); assert.equal(h.requests.length, 2);
});

test('turn changes during approval cancel the old callback without granting the new turn', async () => {
  const gate = deferred(); const h = harness((_request, count) => count === 1 ? gate.promise : 'allowed-once'); h.start();
  const pending = h.run(); await tick();
  h.agent.session.append('turn/end'); h.start();
  assert.equal(await pending, false);
  gate.resolve('allowed-once'); await tick();
  assert.equal(await h.run(), true); assert.equal(h.requests.length, 2);
});

test('input insertion while pending cancels permission without inspecting message content', async () => {
  const gate = deferred(); const h = harness(() => gate.promise); h.start();
  const pending = h.run(); await tick();
  const message = { get content() { throw new Error('Content must not be read'); } };
  h.agent.session.append('agent/inbox/spliced', { inserted: [message] });
  assert.equal(await pending, false); gate.resolve('allowed-once');
});

test('different agents or replacement sessions cannot share grants', async () => {
  const h = harness(); h.start(); assert.equal(await h.run(), true);
  const other = { session: h.agent.session };
  assert.equal(await h.run('search', { target: other }), true);
  h.agent.session = h.session(); h.start();
  assert.equal(await h.run(), true); assert.equal(h.requests.length, 3);
});

test('different plugin contexts cannot share a grant', async () => {
  const first = harness(); first.start(); assert.equal(await first.run(), true);
  const second = harness();
  assert.equal(await second.run('search', { target: first.agent }), true);
  assert.equal(second.requests.length, 1);
});

test('missing lifecycle support or missed turn start never enables caching', async () => {
  for (const tracked of [false, true]) {
    const h = harness(undefined, tracked);
    if (tracked) h.agent.session.append('tool/result');
    assert.equal(await h.run(), true); assert.equal(await h.run(), true);
    assert.equal(h.requests.length, 2);
    assert.ok(h.requests[0].reason.includes('无法确认可靠轮次标识'));
  }
});

test('ordinary audit events preserve permission but missing events and rollback invalidate it', async () => {
  const h = harness(); h.start(); assert.equal(await h.run(), true);
  h.agent.session.append('approval/requested'); h.agent.session.append('approval/decided');
  assert.equal(await h.run(), true); assert.equal(h.requests.length, 1);
  h.agent.session.seq += 1;
  assert.equal(await h.run(), true); assert.equal(h.requests.length, 2);
  h.agent.session.seq = 0;
  assert.equal(await h.run(), true); assert.equal(h.requests.length, 3);
});

test('plugin unload aborts pending approval and prevents stale callbacks', async () => {
  const gate = deferred(); const h = harness(() => gate.promise); h.start();
  const pending = h.run(); await tick(); h.dispose();
  assert.equal(await pending, false); assert.equal(h.requests[0].signal.aborted, true);
  gate.resolve('allowed-once'); await tick();
  assert.equal(await h.run(), false); assert.equal(h.requests.length, 1);
});

test('changing or removing the approval service does not retain old grants', async () => {
  const h = harness(); h.start(); assert.equal(await h.run(), true);
  h.setService(undefined); assert.equal(await h.run(), false);
  let calls = 0; h.setService({ request: async () => { calls++; return 'allowed-once'; } });
  assert.equal(await h.run(), true); assert.equal(calls, 1);
});

test('fresh Cordis tracing proxies share only the same underlying service grant', async () => {
  const h = harness(); let calls = 0;
  let original = { request: async function () {
    // The request must retain the tracing proxy as its receiver.
    assert.notEqual(this, original);
    calls++; return 'allowed-once';
  } };
  h.ctx.get = name => name !== 'approval' ? undefined : new Proxy(original, {
    get(target, key, receiver) {
      return key === Symbol.for('cordis.original') ? target : Reflect.get(target, key, receiver);
    },
  });
  h.start();
  assert.equal(await h.run('search'), true);
  assert.equal(await h.run('refresh'), true);
  assert.equal(calls, 1);
  original = { request: async () => { calls++; return 'allowed-once'; } };
  assert.equal(await h.run(), true);
  assert.equal(calls, 2);
});

test('a late approval from a replaced service cannot execute or populate the cache', async () => {
  const gate = deferred(); const h = harness(() => gate.promise); h.start();
  const pending = h.run(); await tick();
  let asks = 0; h.setService({ request: async () => { asks++; return 'allowed-once'; } });
  gate.resolve('allowed-once');
  assert.equal(await pending, false);
  assert.equal(await h.run(), true); assert.equal(asks, 1);
});

test('malformed cancellation signal fails closed and cannot leave a grant', async () => {
  const h = harness(); h.start();
  assert.equal(await h.run('review', { signal: {} }), false);
  assert.equal(h.requests.length, 0);
  assert.equal(await h.run(), true); assert.equal(h.requests.length, 1);
});

test('initialization is idempotent and does not read deprecated session history', async () => {
  const h = harness(); initializeConsent(h.ctx); initializeConsent(h.ctx);
  h.agent.session.eventAt = () => { throw new Error('Deprecated history must not be read'); };
  h.start(); assert.equal(await h.run(), true); assert.equal(await h.run(), true);
  assert.equal(h.requests.length, 1);
});
