import test from 'node:test';
import assert from 'node:assert/strict';
import { resolve } from 'node:path';
import { TOOL_SPECS, registerTools, publicResult, validateArgs, approve } from '../lib/tools.js';
import { defaultStateDirectory, stateDirectory } from '../lib/bridge.js';

function harness(approval, config = {}, handler = async () => ({ ok: true, data: { indexed: false } }), permissionPresets) {
  const definitions = new Map();
  const calls = [];
  const ctx = { tools: { register: tool => definitions.set(tool.name, tool) },
    get: name => name === 'approval' ? approval : name === 'permissionPresets' ? permissionPresets : undefined };
  registerTools(ctx, config, x => x, TOOL_SPECS, async (...args) => {
    calls.push(args);
    // Synthetic ready status isolates wrapper tests from capture orchestration.
    if (args[0] === 'status' && config.syntheticReady) return { ok: true, data: {
      indexed: true, current_date: '2026-01-01', latest_snapshot_at: '2026-01-01T12:00:00+08:00', freshness: 'snapshot', refresh_required: false } };
    return handler(...args);
  });
  return { ctx, definitions, calls };
}
const execution = () => ({ agent: {}, name: 'wechat_search', callId: 'synthetic-call', signal: new AbortController().signal });

test('registers eleven bounded tools without model-controlled local paths', () => {
  const h = harness();
  assert.equal(h.definitions.size, 11);
  for (const spec of TOOL_SPECS) for (const key of ['path', 'sql', 'command', 'raw', 'source'])
    assert.equal(Object.hasOwn(spec.parameters, key), false);
});

test('fixed stage one has no selectors and rejects them before approval or capture', async () => {
  let approvals = 0;
  const h = harness({ request: async () => { approvals++; return 'allowed-once'; } });
  const selectorExamples = { start_date: '2026-01-01', end_date: '2026-01-02',
    start_time: '2026-01-01T09:00:00+08:00', end_time: '2026-01-01T10:00:00+08:00',
    kind: 'group', kinds: ['group'], conversation_id: 'conv_' + 'a'.repeat(24),
    conversation_ids: ['conv_' + 'a'.repeat(24)], exclude_conversation_ids: [] };
  for (const action of ['prepare', 'refresh']) {
    const spec = TOOL_SPECS.find(s => s.action === action);
    assert.deepEqual(spec.parameters, {});
    assert.equal(validateArgs(spec, {}), true);
    for (const [key, value] of Object.entries(selectorExamples)) {
      assert.equal((await h.definitions.get(spec.name).execute({ [key]: value }, execution())).error.code, 'INVALID_ARGS');
    }
  }
  assert.equal(approvals, 0);
  assert.equal(h.calls.length, 0);
});

test('stage two keeps selectors and has no collection runtime budget', () => {
  const h = harness();
  for (const action of ['status', 'search', 'review', 'context']) {
    const spec = TOOL_SPECS.find(s => s.action === action);
    for (const key of ['start_time', 'end_time', 'kinds', 'conversation_ids', 'exclude_conversation_ids']) {
      assert.ok(Object.hasOwn(spec.parameters, key));
    }
    assert.equal(h.definitions.get(spec.name).timeoutMs, undefined);
  }
});
test('status can run without approval; reading cannot', async () => {
  const h = harness();
  assert.equal((await h.definitions.get('wechat_status').execute({}, {})).data.indexed, false);
  assert.equal((await h.definitions.get('wechat_search').execute({ query: '验收' }, execution())).error.code, 'APPROVAL_REQUIRED');
  assert.equal(h.calls.length, 1);
});
test('requires exact allowed-once outcome, denies rejected, unavailable and object lookalikes', async () => {
  for (const result of ['rejected', 'unavailable', 'allowed', undefined, { kind: 'allowed-once' }]) {
    const h = harness({ request: async () => result });
    assert.equal((await h.definitions.get('wechat_search').execute({ query: '验' }, execution())).ok, false);
    assert.equal(h.calls.length, 0);
  }
});
test('approval exception and aborted requests fail closed', async () => {
  const h = harness({ request: async () => { throw new Error('SYNTHETIC_PRIVATE_DETAIL'); } });
  const tool = h.definitions.get('wechat_search');
  assert.equal((await tool.execute({ query: '验收' }, execution())).ok, false);
  const controller = new AbortController(); controller.abort();
  assert.equal((await tool.execute({ query: '验收' }, { ...execution(), signal: controller.signal })).ok, false);
  assert.equal(h.calls.length, 0);
});
test('authorized read preserves real message fields, removing private source metadata', async () => {
  const realText = '示例成员：电话 13800000000。请周五前验收，不要改写这句话。';
  const h = harness({ request: async () => 'allowed-once' }, { syntheticReady: true }, async () => ({ ok: true, data: {
    messages: [{ message_id: 'msg_' + 'a'.repeat(24), text: realText, sender_label: '示例成员',
      conversation_label: '示例项目组', source_path: 'SYNTHETIC_PRIVATE_PATH', aeskey: 'SYNTHETIC_SECRET' }],
    source_path: 'SYNTHETIC_PRIVATE_PATH', has_more: false,
  } }));
  const result = await h.definitions.get('wechat_search').execute({ query: '验收' }, execution());
  assert.equal(result.data.messages[0].text, realText);
  assert.equal(result.data.messages[0].sender_label, '示例成员');
  assert.equal(JSON.stringify(result).includes('SYNTHETIC_PRIVATE'), false);
  assert.equal(JSON.stringify(result).includes('SYNTHETIC_SECRET'), false);
  assert.deepEqual(h.calls.map(c => c[0]), ['search']);
});
test('explicitly disabling read confirmation never disables save confirmation', async () => {
  const h = harness(undefined, { confirmReads: false, syntheticReady: true });
  assert.equal((await h.definitions.get('wechat_search').execute({ query: '项目' }, execution())).ok, true);
  assert.equal((await h.definitions.get('wechat_save_tasks').execute({ tasks: [] }, execution())).error.code, 'APPROVAL_REQUIRED');
  for (const name of ['wechat_prepare', 'wechat_refresh']) {
    assert.equal((await h.definitions.get(name).execute({}, execution())).error.code, 'APPROVAL_REQUIRED');
  }
  assert.deepEqual(h.calls.map(c => c[0]), ['search']);
});

test('reading missing or stale text never implicitly invokes stage one', async () => {
  for (const value of [{ ok: false, error: { code: 'NOT_INDEXED' } },
    { ok: true, data: { messages: [], refresh_required: true, next_action: 'wechat_refresh' } }]) {
    for (const [name, args] of [['wechat_search', { query: '示例' }], ['wechat_review', { start_date: '2026-01-01' }]]) {
      const h = harness(undefined, { confirmReads: false }, async () => value);
      const result = await h.definitions.get(name).execute(args, execution());
      assert.equal(result.ok, value.ok);
      assert.deepEqual(h.calls.map(c => c[0]), [name.slice('wechat_'.length)]);
    }
  }
});

test('host Auto preset removes redundant plugin prompts for both bounded stages only', async () => {
  let approvals = 0;
  const session = {};
  const requests = [];
  const h = harness({ request: async () => { approvals++; return 'rejected'; } }, {},
    async action => { requests.push(action); return { ok: true, data: { messages: [] } }; },
    { current: candidate => candidate === session ? 'auto' : 'default' });
  const executionFor = name => ({ ...execution(), name, agent: { session } });
  const calls = [
    ['wechat_prepare', {}], ['wechat_refresh', {}], ['wechat_search', { query: '示例' }],
    ['wechat_review', { start_date: '2026-01-01' }], ['wechat_context', { message_id: 'msg_' + 'a'.repeat(24) }],
    ['wechat_conversations', { query: '示例' }], ['wechat_get_preferences', {}], ['wechat_list_tasks', {}],
  ];
  for (const [name, args] of calls) assert.equal((await h.definitions.get(name).execute(args, executionFor(name))).ok, true);
  assert.equal(approvals, 0);
  assert.deepEqual(requests, calls.map(([name]) => name.slice('wechat_'.length)));
  for (const [name, args] of [['wechat_save_tasks', { tasks: [] }], ['wechat_set_preferences', { exclude_conversation_ids: [] }]]) {
    assert.equal((await h.definitions.get(name).execute(args, executionFor(name))).error.code, 'APPROVAL_REQUIRED');
  }
  assert.equal(approvals, 2);
  assert.equal(requests.length, calls.length);
});

test('initialization and refresh use long runtime budgets and explain local collection', async () => {
  const reasons = [];
  const h = harness({ request: async args => { reasons.push(args.reason); return 'allowed-once'; } }, {},
    async () => ({ ok: true, data: { readiness: 'ready', source_configured: true, source_mode: 'native',
      changed: true, added: 12, updated: 2, latest_snapshot_at: '2026-01-01T12:00:00+08:00',
      freshness: 'snapshot', is_realtime: false, source_path: 'SYNTHETIC_PRIVATE_PATH' } }));
  for (const name of ['wechat_prepare', 'wechat_refresh']) {
    assert.ok(h.definitions.get(name).timeoutMs >= 1200000);
    const result = await h.definitions.get(name).execute({}, execution());
    assert.equal(result.data.source_mode, 'native');
    assert.equal(result.data.added, 12);
    assert.equal(result.data.is_realtime, false);
    assert.equal(JSON.stringify(result).includes('SYNTHETIC_PRIVATE_PATH'), false);
  }
  assert.ok(reasons.every(reason => reason.includes('进程和数据库')));
});

test('not-indexed errors lead to in-plugin initialization', () => {
  const result = publicResult({ ok: false, error: { code: 'NOT_INDEXED', message: 'SYNTHETIC_PRIVATE_PATH' } });
  assert.ok(result.error.message.includes('wechat_prepare'));
  assert.equal(result.error.message.includes('SYNTHETIC_PRIVATE'), false);
});
test('unknown arguments rejected before approval or process launch', async () => {
  let approvals = 0;
  const h = harness({ request: async () => { approvals++; return 'allowed-once'; } });
  const result = await h.definitions.get('wechat_search').execute({ query: '验收', path: 'SYNTHETIC_PATH' }, execution());
  assert.equal(result.error.code, 'INVALID_ARGS');
  const emptyCursor = await h.definitions.get('wechat_search').execute({ query: '验收', cursor: '' }, execution());
  assert.equal(emptyCursor.error.code, 'INVALID_ARGS');
  assert.equal(approvals, 0); assert.equal(h.calls.length, 0);
});
test('invalid kinds, wrong limits and arbitrary object arguments rejected', () => {
  const spec = TOOL_SPECS.find(s => s.action === 'search');
  for (const args of [null, [], { query: 'a', kind: 'sql' }, { query: 'a', limit: 1.5 }, { query: 'a', limit: Infinity }])
    assert.equal(validateArgs(spec, args), false);
  assert.equal(validateArgs(spec, { query: '验', cursor: 'synthetic-cursor', limit: 10 }), true);
});

test('scope selectors validate before approval and cannot pass paths or arbitrary arrays', async () => {
  const spec = TOOL_SPECS.find(s => s.action === 'review');
  const id = 'conv_' + 'a'.repeat(24);
  assert.equal(validateArgs(spec, {start_time:'2026-01-01T09:00:00+08:00',end_time:'2026-01-01T10:00:00+08:00',kinds:['friend','group'],conversation_ids:[id],exclude_conversation_ids:[]}), true);
  const invalid = [
    {kinds:[]}, {conversation_ids:[]}, {kinds:['sql']}, {conversation_ids:['raw-private-id']},
    {conversation_id:id,conversation_ids:[id]}, {kind:'group',kinds:['group']},
    {start_date:'2026-01-01',end_time:'2026-01-01T10:00:00+08:00'}, {start_time:'yesterday'},
    {exclude_conversation_ids:Array(101).fill(id)}, {conversation_ids:{path:'SYNTHETIC_PRIVATE'}},
  ];
  let approvals=0;
  const h=harness({request:async()=>{approvals++;return 'allowed-once';}});
  for (const args of invalid) {
    assert.equal(validateArgs(spec,args),false);
    assert.equal((await h.definitions.get('wechat_review').execute(args,execution())).error.code,'INVALID_ARGS');
  }
  assert.equal(approvals,0); assert.equal(h.calls.length,0);
});

test('conversation and scope metadata preserve permitted names but not private mapping fields', () => {
  const id='conv_'+'b'.repeat(24);
  const result=publicResult({ok:true,data:{conversations:[{conversation_id:id,conversation_label:'示例会话',kind:'group',message_count:3,last_time:'2026-01-01T09:00:00+08:00',ignored:true,raw_id:'SYNTHETIC_SECRET'}],effective_scope:{conversation_ids:[id],kinds:['group'],source:'SYNTHETIC_PRIVATE'},exclude_conversation_ids:[id],preferences_revision:2,scope_refresh_required:true,scope_snapshot_at:null}});
  assert.equal(result.data.conversations[0].conversation_label,'示例会话');
  assert.deepEqual(result.data.effective_scope.conversation_ids,[id]);
  assert.deepEqual(result.data.exclude_conversation_ids,[id]);
  assert.equal(result.data.scope_refresh_required,true);
  assert.equal(JSON.stringify(result).includes('SYNTHETIC_'),false);
});

test('exclude_muted is an optional boolean scope selector on every second-stage read', () => {
  for (const action of ['status', 'search', 'review', 'conversations']) {
    const spec = TOOL_SPECS.find(s => s.action === action);
    const required = action === 'search' ? { query: '验收' } : {};
    assert.ok(Object.hasOwn(spec.parameters, 'exclude_muted'), action + ' must expose exclude_muted');
    assert.equal(spec.parameters.exclude_muted.type, 'boolean');
    assert.equal(validateArgs(spec, { ...required, exclude_muted: true }), true);
    assert.equal(validateArgs(spec, { ...required, exclude_muted: false }), true);
  }
  const search = TOOL_SPECS.find(s => s.action === 'search');
  const review = TOOL_SPECS.find(s => s.action === 'review');
  const conversations = TOOL_SPECS.find(s => s.action === 'conversations');
  assert.equal(validateArgs(search, { query: '验收', exclude_muted: true, start_date: '2026-01-01', kinds: ['group'] }), true);
  assert.equal(validateArgs(review, { exclude_muted: true, limit: 10 }), true);
  assert.equal(validateArgs(conversations, { exclude_muted: true, include_ignored: false }), true);
  // Booleans only: numbers, strings, null and lookalikes are rejected before any read.
  for (const value of [1, 0, 'true', 'false', null, [], {}, 'yes']) {
    for (const spec of [search, review, conversations, TOOL_SPECS.find(s => s.action === 'status')]) {
      assert.equal(validateArgs(spec, { ...(spec.action === 'search' ? { query: '验收' } : {}), exclude_muted: value }), false,
        spec.name + ' must reject ' + JSON.stringify(value));
    }
  }
});

test('exclude_muted reaches the local engine as a scope key for every text read', async () => {
  const h = harness(undefined, { confirmReads: false, syntheticReady: true }, async () => ({ ok: true, data: { messages: [], conversations: [] } }));
  const calls = [
    ['wechat_search', { query: '验收', exclude_muted: true }],
    ['wechat_review', { exclude_muted: false }],
    ['wechat_context', { message_id: 'msg_' + 'a'.repeat(24), exclude_muted: true }],
    ['wechat_status', { exclude_muted: true }],
  ];
  for (const [name, args] of calls) {
    assert.equal((await h.definitions.get(name).execute(args, { ...execution(), name })).ok, true, name);
    const call = h.calls.at(-1);
    assert.equal(call[1].exclude_muted, args.exclude_muted, name + ' must forward exclude_muted');
  }
});

test('public projection exposes muted, mute_data_available and the muted count without other keys', () => {
  const id = 'conv_' + 'c'.repeat(24);
  const mid = 'msg_' + 'd'.repeat(24);
  const result = publicResult({ ok: true, data: {
    messages: [
      { message_id: mid, conversation_id: id, conversation_label: '示例群', sender_label: '示例成员', time: '2026-01-01T09:00:00+08:00',
        kind: 'group', text: '示例正文', message_type: 'text', muted: true, source_path: 'SYNTHETIC_PRIVATE_PATH' },
      { message_id: 'msg_' + 'e'.repeat(24), conversation_id: id, muted: false },
      { message_id: 'msg_' + 'f'.repeat(24), conversation_id: id, muted: null },
      { message_id: 'msg_' + 'a'.repeat(24), conversation_id: id, raw_username: 'SYNTHETIC_SECRET' },
    ],
    conversations: [
      { conversation_id: id, conversation_label: '示例群', kind: 'group', message_count: 3, last_time: '2026-01-01T09:00:00+08:00', ignored: false, muted: true, raw_id: 'SYNTHETIC_SECRET' },
      { conversation_id: 'conv_' + 'd'.repeat(24), muted: false },
      { conversation_id: 'conv_' + 'e'.repeat(24), muted: null },
    ],
    mute_data_available: true, muted_conversation_count: 1,
    username: 'synthetic-muted@chatroom', source_path: 'SYNTHETIC_PRIVATE_PATH', effective_scope: { exclude_muted: true },
  } });
  assert.deepEqual(result.data.messages.map(row => row.muted), [true, false, null, undefined]);
  assert.equal(result.data.messages[0].conversation_label, '示例群');
  assert.deepEqual(result.data.conversations.map(row => row.muted), [true, false, null]);
  assert.equal(result.data.mute_data_available, true);
  assert.equal(result.data.muted_conversation_count, 1);
  assert.equal(result.data.effective_scope.exclude_muted, true);
  // Whitelists still drop unknown fields, raw identifiers and private paths.
  assert.equal(Object.hasOwn(result.data.messages[0], 'source_path'), false);
  assert.equal(Object.hasOwn(result.data.messages.at(-1), 'raw_username'), false);
  assert.equal(Object.hasOwn(result.data.conversations[0], 'raw_id'), false);
  assert.equal(Object.hasOwn(result.data, 'username'), false);
  assert.equal(JSON.stringify(result).includes('SYNTHETIC_'), false);
  // A legacy index without mute data must report unknown, never false.
  const legacy = publicResult({ ok: true, data: { messages: [{ message_id: mid, conversation_id: id, muted: null }],
    conversations: [{ conversation_id: id, muted: null }], mute_data_available: false, muted_conversation_count: 0 } });
  assert.equal(legacy.data.messages[0].muted, null);
  assert.equal(legacy.data.conversations[0].muted, null);
  assert.equal(legacy.data.mute_data_available, false);
  assert.equal(legacy.data.muted_conversation_count, 0);
});

test('the muted tool descriptions tell the model not to read null as false', () => {
  for (const action of ['search', 'review', 'conversations']) {
    const spec = TOOL_SPECS.find(s => s.action === action);
    assert.ok(spec.description.includes('muted'), action + ' description must document the muted field');
    assert.ok(spec.description.includes('null'), action + ' description must mark null as unknown');
    assert.ok(spec.description.includes('exclude_muted: true'), action + ' description must show exclude_muted usage');
  }
  assert.ok(TOOL_SPECS.find(s => s.action === 'status').description.includes('mute_data_available'));
});

test('changing ignored conversations always requires its own explicit approval', async () => {
  const denied=harness(undefined,{confirmReads:false});
  assert.equal((await denied.definitions.get('wechat_set_preferences').execute({exclude_conversation_ids:[]},execution())).error.code,'APPROVAL_REQUIRED');
  const requests=[];
  const h=harness({request:async arg=>{requests.push(arg.reason);return 'allowed-once';}}, {}, async()=>({ok:true,data:{exclude_conversation_ids:[],preferences_revision:1}}));
  for(let i=0;i<2;i++) assert.equal((await h.definitions.get('wechat_set_preferences').execute({exclude_conversation_ids:[]},execution())).ok,true);
  assert.equal(requests.length,2);
  assert.ok(requests.every(reason=>reason.includes('不关注名单')));
});
test('content versions and coverage survive the public projection', () => {
  const eid = 'msg_' + 'a'.repeat(24);
  const result = publicResult({ ok: true, data: { start_time: '2026-01-01T00:00:00+08:00',
    end_time: '2026-01-02T00:00:00+08:00', content_authenticity_verified: false,
    current_time: '2026-01-03T15:00:00+08:00', current_date: '2026-01-03', timezone: 'Asia/Shanghai',
    refresh_required: true, coverage: 'snapshot_before_requested_range', empty_result_reason: 'snapshot_before_requested_range',
    privacy: { message_content: 'original', source_paths_returned: false, internal_path: 'SYNTHETIC_PATH' },
    historical_version: true, context_truncated: false,
    tasks: [{ evidence_versions: { [eid]: 'v_' + 'a'.repeat(32), unwanted: 'SYNTHETIC_SECRET' }, evidence_changed: true }] } });
  assert.ok(result.data.start_time); assert.ok(result.data.end_time);
  assert.equal(result.data.current_date, '2026-01-03');
  assert.equal(result.data.refresh_required, true);
  assert.equal(result.data.empty_result_reason, 'snapshot_before_requested_range');
  assert.equal(result.data.privacy.message_content, 'original');
  assert.equal(result.data.content_authenticity_verified, false);
  assert.equal(result.data.tasks[0].evidence_versions[eid], 'v_' + 'a'.repeat(32));
  assert.equal(result.data.historical_version, true); assert.equal(result.data.context_truncated, false);
  assert.equal(JSON.stringify(result).includes('SYNTHETIC_'), false);
});

test('stage-one completion metadata survives projection without private details', () => {
  for (const stage_one_coverage of ['full', 'partial', 'unknown']) {
    const readable_text_ready = stage_one_coverage === 'full';
    const result = publicResult({ ok: true, data: { stage_one_coverage, readable_text_ready,
      source_path: 'SYNTHETIC_PRIVATE_PATH' } });
    assert.deepEqual(result, { ok: true, data: { stage_one_coverage, readable_text_ready } });
  }
});

test('long message offsets pass validation and survive projection without rewriting text', () => {
  const spec = TOOL_SPECS.find(s => s.action === 'context');
  const mid = 'msg_' + 'b'.repeat(24);
  assert.equal(validateArgs(spec, { message_id: mid, text_offset: 3000, before: 0, after: 0 }), true);
  assert.equal(validateArgs(spec, { message_id: mid, text_offset: 1048577 }), false);
  const result = publicResult({ ok: true, data: { messages: [{ message_id: mid,
    text: '这是长消息末尾的示例待办。', text_offset: 3000, text_length: 3013,
    next_text_offset: null, truncated: true, message_type: 'text', unread_content: false }] } });
  assert.equal(result.data.messages[0].text, '这是长消息末尾的示例待办。');
  assert.equal(result.data.messages[0].text_offset, 3000);
  assert.equal(result.data.messages[0].next_text_offset, null);
  assert.equal(result.data.messages[0].message_type, 'text');
});
test('backend exceptions and error messages never pass through', async () => {
  const result = publicResult({ ok: false, error: { code: 'not_indexed', message: 'SYNTHETIC_PRIVATE_DETAIL' } });
  assert.equal(result.error.code, 'NOT_INDEXED');
  assert.equal(JSON.stringify(result).includes('SYNTHETIC_PRIVATE_DETAIL'), false);
  const h = harness(undefined, { confirmReads: false }, async () => { throw new Error('SYNTHETIC_PRIVATE_DETAIL'); });
  assert.equal((await h.definitions.get('wechat_search').execute({ query: 'a' }, execution())).error.code, 'LOCAL_ENGINE_ERROR');
});
test('default paths are derived at runtime, not hardcoded to a person', () => {
  assert.ok(defaultStateDirectory({}, 'synthetic-home', 'linux').endsWith('dsh-better-wechat'));
  assert.ok(defaultStateDirectory({ XDG_DATA_HOME: 'synthetic-data' }, 'synthetic-home', 'linux').includes('synthetic-data'));
  assert.equal(defaultStateDirectory({ DSH_WECHAT_STATE_DIR: '~/synthetic-data' }, 'synthetic-home', 'linux'),
    resolve('synthetic-home', 'synthetic-data'));
  assert.throws(() => stateDirectory({ stateDirectory: new URL('..', import.meta.url).pathname.replace(/^\/(.:)/, '$1') }));
});
test('a private directory left by the previous project name keeps being used', async () => {
  const { mkdtempSync, mkdirSync, rmSync } = await import('node:fs');
  const { tmpdir } = await import('node:os');
  const { join } = await import('node:path');
  const root = mkdtempSync(join(tmpdir(), 'wechat-state-'));
  const env = { LOCALAPPDATA: root };
  try {
    // Fresh install uses the new name.
    assert.ok(defaultStateDirectory(env, 'synthetic-home', 'win32').endsWith('dsh-better-wechat'));
    mkdirSync(join(root, 'dsh-wechat-assistant'));
    // An index imported under the previous name is never orphaned by the rename.
    assert.ok(defaultStateDirectory(env, 'synthetic-home', 'win32').endsWith('dsh-wechat-assistant'));
    mkdirSync(join(root, 'dsh-better-wechat'));
    assert.ok(defaultStateDirectory(env, 'synthetic-home', 'win32').endsWith('dsh-better-wechat'));
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});
