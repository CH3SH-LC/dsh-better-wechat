#!/usr/bin/env node
/**
 * Verify the shipped DSH runtime in an independent Electron Node process.
 * Loads only immutable program modules. Never boots a DSH profile or model.
 * Does not install a plugin, edit the installation, or restart the desktop app.
 */
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { existsSync, mkdtempSync, readFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { basename, dirname, join, resolve } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
const args = process.argv.slice(2);
const checks = [];
const usage = 'Usage: node tools/verify-installed-runtime.mjs --installation <DSH installation directory> [--runtime-only]\nAlternatively, set DSH_INSTALLATION.';
const fail = (code, checksPassed = 0) => console.error(JSON.stringify({ ok: false, code, checksPassed }));
const option = (name, fallback) => {
  const i = args.indexOf(name);
  if (i < 0) return fallback;
  if (!args[i + 1] || args[i + 1].startsWith('--')) throw new Error(`Missing ${name} value`);
  return args[i + 1];
};
if (args.includes('--help')) {
  console.log(usage);
  process.exit(0);
}
let installation;
let pluginEntry;
try {
  const suppliedInstallation = option('--installation', process.env.DSH_INSTALLATION);
  if (!suppliedInstallation?.trim()) {
    console.error(usage);
    process.exit(2);
  }
  installation = resolve(suppliedInstallation);
  pluginEntry = resolve(option('--plugin', join(here, '..', 'index.js')));
} catch {
  fail('INVALID_ARGUMENTS');
  process.exit(2);
}
const executable = join(installation, 'DeepSeek Harness.exe');
const archive = join(installation, 'resources', 'app.asar');
const runtimeOnly = args.includes('--runtime-only');

if (!args.includes('--probe-child')) {
  if (!existsSync(executable) || !existsSync(archive)) {
    fail('INSTALLATION_UNAVAILABLE');
    process.exit(1);
  }
  const child = spawnSync(executable, [
    '--expose-internals', fileURLToPath(import.meta.url), '--probe-child',
    '--installation', installation, '--plugin', pluginEntry,
    ...(runtimeOnly ? ['--runtime-only'] : []),
  ], {
    env: { ...process.env, ELECTRON_RUN_AS_NODE: '1' },
    encoding: 'utf8', timeout: 30_000, windowsHide: true,
    maxBuffer: 1024 * 1024,
  });
  // Runtime and plugin diagnostics may contain machine-local paths. Never
  // forward child streams. Only this script's checked result is publishable.
  let report;
  try { report = JSON.parse(child.stdout || child.stderr || ''); } catch { /* fixed diagnostic below */ }
  if (child.error || child.status !== 0 || report?.ok !== true) {
    const count = Number.isSafeInteger(report?.checksPassed) && report.checksPassed >= 0
      ? report.checksPassed : 0;
    fail(child.error?.code === 'ETIMEDOUT' ? 'RUNTIME_PROBE_TIMEOUT' : 'RUNTIME_PROBE_FAILED', count);
    process.exit(1);
  }
  const knownChecks = new Set([
    'real-runtime-registration-and-domain-output', 'invalid-arguments-rejected-before-execute',
    'missing-approval-service-fails-closed', 'real-approval-service-rejection-does-not-execute',
    'real-approval-service-one-shot-grant-and-memory-audit', 'pre-cancelled-call-does-not-execute',
    'registration-disposer-cleans-up', 'workspace-plugin-activated-in-real-cordis',
    'workspace-plugin-tools-have-output-contracts', 'workspace-plugin-empty-status-with-no-path-disclosure',
    'workspace-plugin-agentless-read-fails-closed', 'workspace-plugin-unload-cleans-tools',
    'real-permission-preset-auto-detection', 'auto-fixed-collection-and-reads-without-plugin-prompt',
    'auto-writes-retain-separate-approval', 'manual-preset-restores-plugin-approval',
    'auto-host-pre-execute-denial-still-enforced',
  ]);
  const knownTools = new Set(['wechat_context', 'wechat_list_tasks', 'wechat_review', 'wechat_save_tasks', 'wechat_search', 'wechat_status', 'wechat_prepare', 'wechat_refresh', 'wechat_conversations', 'wechat_get_preferences', 'wechat_set_preferences']);
  if (!Array.isArray(report.checks) || report.checks.some(check => !knownChecks.has(check))
    || !Array.isArray(report.pluginTools) || report.pluginTools.some(name => !knownTools.has(name))
    || report.runtimeVersion !== '0.2.0-rc.2') {
    fail('INVALID_PROBE_RESPONSE');
    process.exit(1);
  }
  console.log(JSON.stringify({
    ok: true, runtimeVersion: report.runtimeVersion, checksPassed: report.checks.length,
    checks: report.checks, pluginTools: report.pluginTools,
    boundaries: {
      profilesRead: false, privateDataRead: false, modelStarted: false,
      installedIntoDesktop: false, liveDesktopAcceptance: false,
    },
  }, null, 2));
  process.exit(0);
}

async function runProbe() {
const runtime = join(archive, 'dsh');
const moduleFile = (name, file = 'lib/index.js') => join(runtime, 'node_modules', '@deepseek-ai', name, file);
const moduleUrl = (name, file) => pathToFileURL(moduleFile(name, file)).href;
const version = JSON.parse(readFileSync(join(archive, 'package.json'), 'utf8')).version;
assert.equal(version, '0.2.0-rc.2', 'This probe contract was verified against DSH 0.2.0-rc.2. Re-audit another runtime version.');

const { Context, Service } = await import(moduleUrl('cordis'));
const { default: SystemPrompt } = await import(moduleUrl('dsh-system-prompt'));
const { default: ToolRuntime, defineTool } = await import(moduleUrl('dsh-tools'));
const ctx = new Context();
const promptFiber = await ctx.plugin(SystemPrompt, {
  includeHarnessIdentity: false, includeRuntimeContext: false,
});
const toolsFiber = await ctx.plugin(ToolRuntime, { mode: 'native' });
let pluginFiber;
let resolutionFiber;
let approvalFiber;
let isolatedState;

try {
  let executions = 0;
  const unregister = ctx.tools.register(defineTool({
    name: 'wechat_runtime_contract_probe',
    description: 'Synthetic, memory-only runtime contract verification.',
    parameters: { text: { type: 'string', required: true } },
    output: {
      schema: {
        type: 'object', additionalProperties: false,
        properties: { length: { type: 'integer', required: true } },
      },
      render: (_args, value) => [{ type: 'text', text: JSON.stringify(value) }],
    },
    async execute(input, execution) {
      assert(execution.signal instanceof AbortSignal);
      executions++;
      return { length: input.text.length };
    },
  }));
  const invoke = (input, signal = new AbortController().signal, agent) => ctx.tools.execute({
    name: 'wechat_runtime_contract_probe', arguments: input, signal, ...(agent ? { agent } : {}),
  });

  const result = await invoke({ text: 'SYNTHETIC' });
  assert.equal(result.isError, false);
  assert.deepEqual(result.value, { length: 9 });
  assert.deepEqual(result.content, [{ type: 'text', text: '{"length":9}' }]);
  checks.push('real-runtime-registration-and-domain-output');

  const invalid = await invoke({ text: 7 });
  assert.equal(invalid.isError, true);
  assert.equal(invalid.error.info.code, 'INVALID_ARGS');
  assert.equal(executions, 1);
  checks.push('invalid-arguments-rejected-before-execute');

  const removeGate = ctx.on('tools/pre-execute', (execution, next) => (
    execution.name === 'wechat_runtime_contract_probe'
      ? { kind: 'ask', reason: 'Synthetic privacy approval verification.' }
      : next()
  ));
  const denied = await invoke({ text: 'SYNTHETIC' });
  assert.equal(denied.isError, true);
  assert.equal(executions, 1);
  checks.push('missing-approval-service-fails-closed');

  const { default: ApprovalService } = await import(moduleUrl('dsh-user-approval'));
  approvalFiber = await ctx.plugin(ApprovalService, { policy: 'ask' });
  // A synthetic agent and in-memory log exercise the real approval service.
  // There is no session persistence provider and no LLM/agent loop.
  const audit = [{ type: 'turn/start', data: {} }];
  const agent = {
    id: 'wechat-runtime-probe-agent', ctx,
    session: {
      get seq() { return audit.length; },
      eventAt(index) { return audit[index]; },
      append(type, data) { const event = { type, data }; audit.push(event); return event; },
    },
  };
  const reject = ctx.on('approval/request', () => 'rejected');
  const rejected = await invoke({ text: 'SYNTHETIC' }, undefined, agent);
  assert.equal(rejected.isError, true);
  assert.equal(executions, 1);
  reject();
  checks.push('real-approval-service-rejection-does-not-execute');
  const allow = ctx.on('approval/request', () => 'allowed-once');
  const approved = await invoke({ text: 'SYNTHETIC' }, undefined, agent);
  assert.equal(approved.isError, false);
  assert.equal(executions, 2);
  assert.equal(audit.at(-1).type, 'approval/decided');
  assert.equal(audit.at(-1).data.outcome, 'allowed-once');
  allow();
  removeGate();
  checks.push('real-approval-service-one-shot-grant-and-memory-audit');

  const controller = new AbortController();
  controller.abort();
  const cancelled = await invoke({ text: 'SYNTHETIC' }, controller.signal);
  assert.equal(cancelled.isError, true);
  assert.equal(executions, 2);
  checks.push('pre-cancelled-call-does-not-execute');
  unregister();
  assert.equal(ctx.tools.get('wechat_runtime_contract_probe'), undefined);
  checks.push('registration-disposer-cleans-up');

  let pluginTools = [];
  if (!runtimeOnly) {
    assert(existsSync(pluginEntry), 'Plugin entry is missing; use --plugin or --runtime-only.');
    isolatedState = mkdtempSync(join(tmpdir(), 'dsh-wechat-runtime-probe-'));
    // Use DSH's actual package resolver, with no profile and an explicit
    // temporary home. The only linked package is this workspace bundle.
    const { createRuntimeResolution, PluginPackages } = await import(moduleUrl('dsh-app-boot'));
    const resolution = await createRuntimeResolution({
      installAnchor: moduleFile('dsh', 'package.json'),
      home: join(isolatedState, 'empty-home'),
    });
    resolutionFiber = await ctx.plugin(PluginPackages, {
      resolution: {
        ...resolution,
        linkedRoots: [{ name: '@local/dsh-wechat-runtime-probe', realPath: dirname(pluginEntry) }],
      },
    });
    const plugin = await import(pathToFileURL(pluginEntry).href);
    pluginFiber = await ctx.plugin(plugin, { confirmReads: true, stateDirectory: join(isolatedState, 'state') });
    pluginTools = ctx.tools.schemas().map((tool) => tool.name).sort();
    assert(pluginTools.length > 0, 'The plugin did not register any tools.');
    for (const name of pluginTools) {
      const definition = ctx.tools.get(name);
      assert(definition.output?.schema && typeof definition.output.render === 'function');
      if (['wechat_prepare', 'wechat_refresh'].includes(name)) assert(definition.timeoutMs >= 1200000);
    }
    checks.push('workspace-plugin-activated-in-real-cordis');
    checks.push('workspace-plugin-tools-have-output-contracts');
    if (pluginTools.includes('wechat_status')) {
      const status = await ctx.tools.execute({
        name: 'wechat_status', arguments: {}, signal: new AbortController().signal,
      });
      assert.equal(status.isError, false, 'Empty isolated status call failed.');
      assert.equal(status.value?.ok, true, 'Empty isolated status returned a domain failure.');
      assert.equal(status.value?.data?.indexed, false, 'Isolated status unexpectedly found an index.');
      assert(!JSON.stringify(status).includes(isolatedState), 'Status leaked a local path.');
      checks.push('workspace-plugin-empty-status-with-no-path-disclosure');
    }
    if (pluginTools.includes('wechat_search')) {
      const deniedRead = await ctx.tools.execute({
        name: 'wechat_search', arguments: { query: 'SYNTHETIC' }, signal: new AbortController().signal,
      });
      assert.equal(deniedRead.isError, false, 'Approval denial must retain the declared domain result.');
      assert.equal(deniedRead.value?.ok, false);
      assert.equal(deniedRead.value?.error?.code, 'APPROVAL_REQUIRED');
      checks.push('workspace-plugin-agentless-read-fails-closed');
    }
    // Mount the shipped permission-preset service over synthetic, in-memory
    // shell/session/projection dependencies. This tests the real current()
    // contract and Cordis service proxy. It does not mount Auto's LLM reviewer,
    // open a real profile, run a collector, or change the desktop's preset.
    const supportFibers = [];
    let permissionFiber, removeAuto, rejectWrite, denyHost;
    try {
      class FixtureShell extends Service {
        constructor(child) { super(child, 'shell'); this.sandboxMode = 'workspace-write'; }
      }
      class FixtureSessions extends Service {
        constructor(child) { super(child, 'sessions'); }
        list() { return []; }
      }
      class FixtureProjections extends Service {
        constructor(child) { super(child, 'sessionProjections'); this.definitions = new Map(); }
        register(definition) { this.definitions.set(definition.key, definition); return () => this.definitions.delete(definition.key); }
        stateOf(session, key) {
          const definition = this.definitions.get(key);
          if (!definition) return undefined;
          let state = definition.init();
          for (let i = 0; i < session.seq; i++) state = definition.apply(state, session.eventAt(i));
          return state;
        }
      }
      for (const support of [FixtureShell, FixtureSessions, FixtureProjections]) supportFibers.push(await ctx.plugin(support));
      const { default: PermissionPresetService } = await import(moduleUrl('dsh-permission-presets'));
      permissionFiber = await ctx.plugin(PermissionPresetService, {});
      const presets = ctx.get('permissionPresets');
      assert(presets && typeof presets.current === 'function');
      removeAuto = presets.registerAuto(() => {});
      presets.set(agent.session, 'auto');
      assert.equal(ctx.get('permissionPresets').current(agent.session), 'auto');
      checks.push('real-permission-preset-auto-detection');
      let manualAsks = 0;
      rejectWrite = ctx.on('approval/request', () => { manualAsks++; return 'rejected'; });
      const { approve } = await import(pathToFileURL(join(dirname(pluginEntry), 'lib', 'consent.js')).href);
      for (const action of ['prepare', 'refresh', 'search', 'review', 'context', 'list_tasks', 'conversations', 'get_preferences']) {
        assert.equal(await approve(ctx, { action, name: `wechat_${action}` }, {
          agent, name: `wechat_${action}`, signal: new AbortController().signal,
        }), true);
      }
      assert.equal(manualAsks, 0);
      checks.push('auto-fixed-collection-and-reads-without-plugin-prompt');
      for (const action of ['save_tasks', 'set_preferences']) assert.equal(await approve(ctx, {
        action, name: `wechat_${action}`,
      }, { agent, name: `wechat_${action}`, signal: new AbortController().signal }), false);
      assert.equal(manualAsks, 2);
      checks.push('auto-writes-retain-separate-approval');
      // Even under Auto, a host pre-execute denial must be decisive. This
      // synthetic policy gate executes through the actual tool runtime.
      denyHost = ctx.on('tools/pre-execute', (exec, next) => exec.name === 'wechat_get_preferences'
        ? { kind: 'deny', reason: 'Synthetic host gate rejects this read.' } : next());
      const deniedByHost = await ctx.tools.execute({
        name: 'wechat_get_preferences', arguments: {}, agent, signal: new AbortController().signal,
      });
      assert.equal(deniedByHost.isError, true);
      assert.equal(manualAsks, 2);
      denyHost(); denyHost = undefined;
      checks.push('auto-host-pre-execute-denial-still-enforced');
      presets.set(agent.session, 'workspace-write');
      assert.equal(ctx.get('permissionPresets').current(agent.session), 'workspace-write');
      assert.equal(await approve(ctx, { action: 'review', name: 'wechat_review' }, {
        agent, name: 'wechat_review', signal: new AbortController().signal,
      }), false);
      assert.equal(manualAsks, 3);
      checks.push('manual-preset-restores-plugin-approval');
    } finally {
      if (denyHost) denyHost();
      if (rejectWrite) rejectWrite();
      if (removeAuto) await removeAuto();
      if (permissionFiber) await permissionFiber.dispose();
      for (const fiber of supportFibers.reverse()) await fiber.dispose();
    }
    await pluginFiber.dispose();
    pluginFiber = undefined;
    assert.deepEqual(ctx.tools.schemas().map((tool) => tool.name), []);
    checks.push('workspace-plugin-unload-cleans-tools');
  }
  console.log(JSON.stringify({
    ok: true, runtimeVersion: version, checks, pluginTools,
    boundaries: {
      profilesRead: false, privateDataRead: false, modelStarted: false,
      installedIntoDesktop: false, liveDesktopAcceptance: false,
    },
  }, null, 2));
} finally {
  if (pluginFiber) await pluginFiber.dispose();
  if (approvalFiber) await approvalFiber.dispose();
  if (resolutionFiber) await resolutionFiber.dispose();
  await toolsFiber.dispose();
  await promptFiber.dispose();
  if (isolatedState) {
    assert.equal(dirname(resolve(isolatedState)), resolve(tmpdir()));
    assert(basename(isolatedState).startsWith('dsh-wechat-runtime-probe-'));
    // Keep the small synthetic fixture for diagnosis. Never recursively remove
    // test trees: Electron filesystem wrappers may follow Windows junctions.
  }
}
}

try {
  await runProbe();
} catch {
  fail('RUNTIME_PROBE_FAILED', checks.length);
  process.exitCode = 1;
}
