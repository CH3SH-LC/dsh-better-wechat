// Shared owner of the WeChat assistant plugin: configuration, the single Python
// bridge, the single capture/query workflow, the single per-turn consent
// manager and the local runtime receipt.
//
// This component deliberately registers no tool of its own. Cordis keeps one
// `wechatAssistant` service alive exactly as long as this component is loaded;
// the four feature components only read that service. Every feature component
// passes this context to `registerTools` as the approval context, so one turn
// still produces at most one read confirmation even though the tools now live
// in separate components with separate contexts.
import { readFileSync } from 'node:fs';
import Schema from '@deepseek-ai/schemastery';
import { defineTool } from '@deepseek-ai/dsh-tools';
import { registerTools } from '../lib/tools.js';
import { makeBridge } from '../lib/bridge.js';
import { makeWorkflow } from '../lib/workflow.js';
import { initializeConsent } from '../lib/consent.js';
import { toolSpecsFor } from '../lib/tool-groups.js';

const PACKAGE_VERSION = JSON.parse(readFileSync(new URL('../package.json', import.meta.url), 'utf8')).version;

/** The one service name the feature components depend on. */
export const SERVICE = 'wechatAssistant';

export const name = 'dsh-better-wechat-core';
export const inject = ['tools'];

// The only component that declares configurable options. The feature
// components are intentionally parameterless so enabling or disabling one can
// never change what the others read.
export const Config = Schema.object({
  confirmReads: Schema.boolean().default(true).description('非 Auto 模式每轮首次读取确认，分页复用；DSH Auto 模式的固定采集与文本读取不额外弹窗，仍遵守宿主策略。保存待办和修改偏好单独确认。'),
  stateDirectory: Schema.string().description('可选：本地私有数据目录，必须在插件代码目录外。不设置则使用系统应用数据目录。'),
  pythonExecutable: Schema.string().description('可选：Python 3.10+ 可执行文件；不设置则自动寻找。'),
});

export async function apply(ctx, config = {}) {
  const bridge = makeBridge(config);
  // Exactly one workflow per plugin instance: stage-one serialization and the
  // physical write barrier must not be duplicated by a second component.
  const workflow = makeWorkflow(bridge);
  // The single per-turn consent state. Feature components never create one.
  initializeConsent(ctx);

  // Diagnostic only: records which tools this host actually registered. It is
  // best-effort by design and must never disable tools or start any collection.
  // The receipt accumulates the union of announced groups for this host pid.
  const announce = async (component) => {
    const tools = toolSpecsFor(component).map(spec => spec.name);
    // The receipt deliberately rejects an empty tool list, so a component that
    // owns no tool has nothing to report and must not spawn a pointless
    // subprocess on every host load.
    if (!tools.length) return;
    try {
      await bridge('runtime_receipt', { version: PACKAGE_VERSION, tools, host_pid: process.pid });
    } catch { /* Private-state diagnostics are best-effort only. */ }
  };

  const api = Object.freeze({
    consentCtx: ctx,
    // `componentCtx` registers and therefore disposes the tools; `api.consentCtx`
    // keeps the shared consent state authoritative for every component. The
    // trailing `toolBridge` exists so the component-seam tests can swap the
    // local engine; the shipped components never pass it.
    register(componentCtx, component, toolBridge) {
      registerTools(componentCtx, config, defineTool,
        toolSpecsFor(component), toolBridge ?? bridge, api.consentCtx);
    },
    announce,
  });

  // Owned by this fiber: unloading core removes the service, which keeps every
  // dependent feature component from running without the shared consent state.
  ctx.provide(SERVICE, api);
  await announce('components/core.js');
}
