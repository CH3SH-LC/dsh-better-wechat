// Component split contract.
//
// These tests never touch a real host, Python process, message store or user
// profile. They install tiny in-memory doubles for the two host packages the
// real runtime resolves (`@deepseek-ai/dsh-tools`, `@deepseek-ai/schemastery`)
// so the shipped modules are exercised unchanged.
import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import Module from 'node:module';

const ROOT = fileURLToPath(new URL('..', import.meta.url));
const url = relative => pathToFileURL(ROOT + relative).href;
const read = async relative => (await import('node:fs/promises')).readFile(ROOT + relative, 'utf8');
const componentName = file => `dsh-better-wechat-${file.split('/').pop().replace(/\.js$/, '')}`;
const pattern = text => text.replace(/[.*+?^${}()|[\]\\/]/g, character => `\\${character}`);
// Assembled at run time so this file never contains a Windows network path.
const lineBreak = `${String.fromCharCode(92)}n${String.fromCharCode(92)}s+`;

const CORE = 'components/core.js';
const COMPONENTS = Object.freeze([
  CORE,
  'components/stage-one.js',
  'components/read.js',
  'components/tasks.js',
  'components/preferences.js',
]);
const FEATURES = Object.freeze(COMPONENTS.filter(file => file !== CORE));
const TOOL_GROUPS = Object.freeze({
  [CORE]: Object.freeze([]),
  'components/stage-one.js': Object.freeze(['wechat_prepare', 'wechat_refresh']),
  'components/read.js': Object.freeze(['wechat_status', 'wechat_search', 'wechat_context', 'wechat_review', 'wechat_conversations']),
  'components/tasks.js': Object.freeze(['wechat_save_tasks', 'wechat_list_tasks']),
  'components/preferences.js': Object.freeze(['wechat_get_preferences', 'wechat_set_preferences']),
});
const ALL_TOOLS = Object.freeze(Object.values(TOOL_GROUPS).flat());
const SUBPATH_EXPORTS = Object.freeze({
  '.': './index.js',
  './core': './components/core.js',
  './stage-one': './components/stage-one.js',
  './read': './components/read.js',
  './tasks': './components/tasks.js',
  './preferences': './components/preferences.js',
});
const PATCH_ENTRIES = Object.freeze([
  ['dsh-better-wechat-core', 'dsh-better-wechat/core'],
  ['dsh-better-wechat-stage-one', 'dsh-better-wechat/stage-one'],
  ['dsh-better-wechat-read', 'dsh-better-wechat/read'],
  ['dsh-better-wechat-tasks', 'dsh-better-wechat/tasks'],
  ['dsh-better-wechat-preferences', 'dsh-better-wechat/preferences'],
]);
const HOST_PEERS = new Set(['@deepseek-ai/dsh-tools', '@deepseek-ai/schemastery']);
const HOST_DOUBLES = Object.freeze({
  '@deepseek-ai/dsh-tools': 'export const defineTool = definition => definition;\n',
  '@deepseek-ai/schemastery': `export default {
  object: fields => ({ type: 'object', fields }),
  boolean: () => ({ type: 'boolean', default(value) { this.value = value; return this; }, description() { return this; } }),
  string: () => ({ type: 'string', description() { return this; } }),
};
`,
});

// The plugin resolves these two packages from the DSH runtime. Under plain Node
// they are absent, so the suite answers the same specifiers with inert doubles.
let hooksInstalled = false;
function installHostDoubles() {
  if (hooksInstalled) return;
  hooksInstalled = true;
  const resolve = (specifier, context, nextResolve) => HOST_PEERS.has(specifier)
    ? { url: `node-double:${specifier}`, shortCircuit: true } : nextResolve(specifier, context);
  const load = (target, context, nextLoad) => target.startsWith('node-double:')
    ? { format: 'module', source: HOST_DOUBLES[target.slice('node-double:'.length)], shortCircuit: true }
    : nextLoad(target, context);
  assert.equal(typeof Module.registerHooks, 'function', 'Node 22+ module hooks are required for the host doubles');
  Module.registerHooks({ resolve, load });
}
installHostDoubles();

/** Minimal in-memory stand-in for the Cordis context and its tools service. */
function harness() {
  const tools = new Map();
  const listeners = new Map();
  const effects = [];
  const disposers = [];
  const approval = { calls: [], request: async request => { approval.calls.push(request); return 'allowed-once'; } };
  const presets = { current: () => 'default' };
  const services = new Map([['approval', approval], ['permissionPresets', presets]]);
  const toolsService = {
    register: definition => {
      tools.set(definition.name, definition);
      return () => tools.delete(definition.name);
    },
  };
  const contextFor = () => ({
    tools: toolsService,
    get: name => services.get(name),
    provide: (name, value) => {
      services.set(name, value);
      const dispose = () => { if (services.get(name) === value) services.delete(name); };
      disposers.push(dispose);
      return dispose;
    },
    on: (event, callback) => {
      const set = listeners.get(event) || new Set();
      set.add(callback);
      listeners.set(event, set);
      return () => set.delete(callback);
    },
    effect: factory => { const cleanup = factory(); effects.push(cleanup); return cleanup; },
  });
  const emit = (event, ...args) => { for (const callback of listeners.get(event) || []) callback(...args); };
  const session = () => ({
    seq: 0,
    append(type, data = {}) {
      const event = { seq: this.seq++, type, data };
      emit('session/event', this, event);
      return event;
    },
  });
  // A turn starts, then ordinary audit events may follow without ending it.
  const sessionFor = () => {
    const value = session();
    value.append('turn/start');
    return value;
  };
  // The real host hands the same agent object to every call of one session, and
  // the shared grant is keyed by that object, so the harness must reuse it too.
  const agents = new Map();
  const agentFor = sessionValue => {
    if (!agents.has(sessionValue)) agents.set(sessionValue, { session: sessionValue });
    return agents.get(sessionValue);
  };
  const executionFor = (name, sessionValue) => ({
    agent: agentFor(sessionValue), name, callId: `synthetic-${name}`, signal: new AbortController().signal,
  });
  return {
    tools, approval, services, listeners,
    coreContext: contextFor(),
    contextFor,
    sessionFor,
    execute: (name, args, sessionValue) => tools.get(name).execute(args, executionFor(name, sessionValue)),
    registerCore: async api => {
      const ctx = contextFor();
      await api.apply(ctx, { confirmReads: true });
      return { ctx, api: services.get(api.SERVICE) };
    },
    dispose: () => {
      for (const cleanup of [...effects].reverse()) { try { cleanup(); } catch { /* test teardown */ } }
      for (const dispose of [...disposers].reverse()) dispose();
    },
  };
}

async function loadComponents() {
  const modules = new Map();
  for (const file of COMPONENTS) modules.set(file, await import(url(file)));
  return modules;
}

/** Deterministic local-engine double: no Python, no state directory, no data. */
function fakeEngine() {
  const calls = [];
  const engine = async (action, params = {}) => {
    calls.push([action, params]);
    if (action === 'status') {
      return { ok: true, data: { indexed: true, current_date: '2026-01-01', latest_snapshot_at: '2026-01-01T12:00:00+08:00', freshness: 'snapshot', refresh_required: false } };
    }
    if (action === 'search' || action === 'context' || action === 'review') return { ok: true, data: { messages: [], has_more: false, next_cursor: null } };
    if (action === 'conversations') return { ok: true, data: { conversations: [] } };
    if (action === 'list_tasks') return { ok: true, data: { tasks: [] } };
    if (action === 'get_preferences') return { ok: true, data: { exclude_conversation_ids: [], preferences_revision: 1 } };
    if (action === 'set_preferences') return { ok: true, data: { exclude_conversation_ids: params.exclude_conversation_ids ?? [], preferences_revision: 2 } };
    if (action === 'save_tasks') return { ok: true, data: { saved_task_ids: [], saved_count: 0 } };
    return { ok: true, data: { readiness: 'ready', source_configured: true, source_mode: 'snapshot', added: 0, updated: 0 } };
  };
  return { calls, engine };
}

/** Start core plus the four feature components against one shared service store. */
async function loadSplitPlugins() {
  const modules = await loadComponents();
  const h = harness();
  const engine = fakeEngine();
  const core = modules.get(CORE);
  const { ctx: coreCtx, api } = await h.registerCore(core);
  // Only the test seam replaces the local engine: the shipped components call
  // the plain two-argument `register` in exactly the same way.
  h.services.set(core.SERVICE, {
    consentCtx: api.consentCtx,
    register: (ctx, component) => api.register(ctx, component, engine.engine),
    announce: api.announce,
  });
  const contexts = new Map([[CORE, coreCtx]]);
  for (const file of FEATURES) {
    const ctx = h.contextFor();
    contexts.set(file, ctx);
    await modules.get(file).apply(ctx);
  }
  return { ...h, modules, core, api, coreCtx, contexts, engine };
}

test('tool groups derive exactly one non-empty group per component and never duplicate', async () => {
  const groups = await import(url('lib/tool-groups.js'));
  assert.equal(groups.assertCompleteToolPartition(), true);
  assert.deepEqual([...groups.DECLARED_TOOL_NAMES].sort(), [...ALL_TOOLS].sort());
  const owned = [];
  for (const file of COMPONENTS) {
    const specs = groups.toolSpecsFor(file);
    assert.deepEqual(specs.map(spec => spec.name), [...TOOL_GROUPS[file]],
      `${file} must own exactly its declared tools`);
    owned.push(...specs.map(spec => spec.name));
  }
  assert.equal(owned.length, 11);
  assert.deepEqual([...owned].sort(), [...ALL_TOOLS].sort());
  assert.equal(new Set(owned).size, 11);
  assert.throws(() => groups.toolSpecsFor('components/synthetic-unknown.js'));
});

test('each component registers exactly its own tools, and the union is all eleven', async () => {
  const environment = await loadSplitPlugins();
  assert.deepEqual([...environment.tools.keys()].sort(), [...ALL_TOOLS].sort());
  for (const file of COMPONENTS) {
    const group = TOOL_GROUPS[file];
    const registered = [...environment.tools.values()].filter(definition => group.includes(definition.name));
    assert.deepEqual(registered.map(definition => definition.name).sort(), [...group].sort(),
      `${file} must register exactly ${group.length} tool(s)`);
    const module = environment.modules.get(file);
    assert.equal(module.name, componentName(file));
    assert.deepEqual(module.inject, file === CORE ? ['tools'] : ['tools', 'wechatAssistant']);
    assert.equal(Object.hasOwn(module, 'Config'), file === CORE);
  }
});

test('disabling one feature component releases only its own tools', async () => {
  const environment = await loadSplitPlugins();
  const release = async file => {
    const ctx = environment.contexts.get(file);
    const disposers = TOOL_GROUPS[file].map(name => ctx.tools.register({ name }));
    for (const dispose of disposers) dispose(); // exactly what Cordis runs on unload
  };
  await release('components/read.js');
  const remaining = [...ALL_TOOLS].filter(name => !TOOL_GROUPS['components/read.js'].includes(name));
  assert.deepEqual([...environment.tools.keys()].sort(), [...remaining].sort());
  for (const file of FEATURES.filter(file => file !== 'components/read.js')) {
    for (const name of TOOL_GROUPS[file]) assert.ok(environment.tools.has(name), `${name} from ${file} must survive`);
  }
  await release('components/preferences.js');
  assert.deepEqual([...environment.tools.keys()].sort(),
    [...ALL_TOOLS].filter(name => !TOOL_GROUPS['components/read.js'].includes(name)
      && !TOOL_GROUPS['components/preferences.js'].includes(name)).sort());
  assert.equal(new Set(environment.tools.keys()).size, environment.tools.size);
});

test('two different component contexts share exactly one read confirmation per turn', async () => {
  const environment = await loadSplitPlugins();
  const first = environment.sessionFor();
  const second = environment.sessionFor();
  const messageId = `msg_${'a'.repeat(24)}`;
  // The read component and the tasks component live in different contexts and
  // register their tools separately, yet they share one read grant per turn.
  const sharedGrant = [
    ['wechat_search', { query: '验收' }, first],
    ['wechat_context', { message_id: messageId }, first],
    ['wechat_get_preferences', {}, first],
    ['wechat_list_tasks', {}, first],
    ['wechat_conversations', {}, first],
    ['wechat_review', {}, first],
  ];
  for (const [name, args, sessionValue] of sharedGrant) {
    assert.equal((await environment.execute(name, args, sessionValue)).ok, true, name);
  }
  assert.equal(environment.approval.calls.length, 1, 'reads from separate feature contexts share one grant');
  assert.equal(environment.engine.calls.filter(([action]) => action === 'status').length, 0);
  // A brand new component context on the same session still reuses that grant.
  const ctx = environment.contextFor();
  environment.contexts.set('synthetic-extra', ctx);
  environment.api.register(ctx, 'components/tasks.js', environment.engine.engine);
  assert.equal((await environment.execute('wechat_list_tasks', {}, first)).ok, true);
  assert.equal(environment.approval.calls.length, 1);

  // Stage one lives in its own context and its own session keeps its own grant.
  assert.equal((await environment.execute('wechat_refresh', {}, second)).ok, true);
  assert.equal(environment.approval.calls.length, 2, 'a different agent never inherits another agent grant');
  // A write always asks on its own, even inside the same turn and component.
  assert.equal((await environment.execute('wechat_save_tasks', { tasks: [] }, first)).ok, true);
  assert.equal(environment.approval.calls.length, 3);
  // New user input inside the same runtime turn needs a fresh read grant.
  first.append('agent/inbox/spliced', { inserted: [{}] });
  assert.equal((await environment.execute('wechat_search', { query: '验收' }, first)).ok, true);
  assert.equal(environment.approval.calls.length, 4);
  // A new runtime turn also needs a fresh grant.
  first.append('turn/end');
  first.append('turn/start');
  assert.equal((await environment.execute('wechat_review', {}, first)).ok, true);
  assert.equal(environment.approval.calls.length, 5);
});

test('package exports, patch entries and component files agree', async () => {
  const pkg = JSON.parse(await read('package.json'));
  assert.deepEqual(pkg.exports, SUBPATH_EXPORTS);
  for (const file of COMPONENTS) assert.ok(pkg.files.includes(file), `${file} must ship in package.json files`);
  assert.ok(pkg.files.includes('lib/tool-groups.js'));
  assert.ok(pkg.files.includes('tests/components.test.mjs'));
  for (const path of Object.values(SUBPATH_EXPORTS)) await import(url(path.replace(/^\.\//, '')));

  const patch = await read('cordis.patch.yml');
  for (const [id, pluginName] of PATCH_ENTRIES) {
    const needle = `- id: ${id}${lineBreak}name: '${pattern(pluginName)}'`;
    assert.ok(patch.includes(`- id: ${id}`), `patch must insert ${id}`);
    assert.ok(new RegExp(needle).test(patch), `${id} must load ${pluginName}`);
  }
  assert.equal((patch.match(/- id: /g) || []).length, 5);
  assert.equal((patch.match(/name: 'dsh-better-wechat'\n/g) || []).length, 0,
    'the patch must not enable the aggregate entry');
  assert.equal((patch.match(/name: '@local\/dsh-better-wechat'\n/g) || []).length, 0,
    'the patch must not enable a scoped aggregate entry');
  assert.equal((patch.match(/confirmReads: true/g) || []).length, 1);
  assert.ok(/- id: dsh-better-wechat-core\n\s+name: '[^']+'\n\s+config:\n\s+confirmReads: true/.test(patch),
    'only core declares the plugin config');
});

test('the aggregate entry still registers all eleven tools', async () => {
  const aggregate = await import(url('index.js'));
  assert.equal(aggregate.name, 'dsh-better-wechat');
  assert.deepEqual(aggregate.inject, ['tools']);
  assert.deepEqual(Object.keys(aggregate.Config.fields).sort(), ['confirmReads', 'pythonExecutable', 'stateDirectory']);
  const registered = new Map();
  const ctx = {
    tools: { register: definition => { registered.set(definition.name, definition); return () => registered.delete(definition.name); } },
    get: () => undefined,
  };
  const state = mkdtempSync(join(tmpdir(), 'dsh-wechat-components-'));
  try {
    await aggregate.apply(ctx, { stateDirectory: state });
    assert.deepEqual([...registered.keys()].sort(), [...ALL_TOOLS].sort());
  } finally {
    rmSync(state, { recursive: true, force: true });
  }
});
