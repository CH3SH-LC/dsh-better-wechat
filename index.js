// Aggregate entry point: one Cordis component that registers all eleven tools.
//
// It is kept for existing installations and for the existing test suite, so the
// installed `dsh-better-wechat` entry keeps behaving exactly as
// before. New installs use the split components instead: core plus the four
// feature components. Never enable this aggregate together with
// `dsh-better-wechat-core` and its feature components, because both bodies
// register the same eleven tool names.
import { readFileSync } from 'node:fs';
import Schema from '@deepseek-ai/schemastery';
import { defineTool } from '@deepseek-ai/dsh-tools';
import { registerTools, TOOL_SPECS } from './lib/tools.js';
import { makeBridge } from './lib/bridge.js';

const PACKAGE_VERSION = JSON.parse(readFileSync(new URL('./package.json', import.meta.url), 'utf8')).version;

export const name = 'dsh-better-wechat';
export const inject = ['tools'];
export const Config = Schema.object({
  confirmReads: Schema.boolean().default(true).description('非 Auto 模式每轮首次读取确认，分页复用；DSH Auto 模式的固定采集与文本读取不额外弹窗，仍遵守宿主策略。保存待办和修改偏好单独确认。'),
  stateDirectory: Schema.string().description('可选：本地私有数据目录，必须在插件代码目录外。不设置则使用系统应用数据目录。'),
  pythonExecutable: Schema.string().description('可选：Python 3.10+ 可执行文件；不设置则自动寻找。'),
});

export async function apply(ctx, config = {}) {
  registerTools(ctx, config, defineTool);
  // A small local receipt distinguishes code on disk from a running host's
  // actual registration. It contains no messages, names, paths or credentials.
  // Diagnostic failure must not disable tools or start any collection.
  try {
    await makeBridge(config)('runtime_receipt', {
      version: PACKAGE_VERSION, tools: TOOL_SPECS.map(spec => spec.name), host_pid: process.pid,
    });
  } catch { /* Private-state diagnostics are best-effort only. */ }
}
