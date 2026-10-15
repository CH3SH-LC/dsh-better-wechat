// Candidate task drafts. Saving always keeps its own separate confirmation and
// never creates or reuses a read grant, even when it is its own component.
export const name = 'dsh-better-wechat-tasks';
export const inject = ['tools', 'wechatAssistant'];

export async function apply(ctx) {
  // Cordis starts this plugin only while `wechatAssistant` is provided by core.
  const api = ctx.get('wechatAssistant');
  api.register(ctx, 'components/tasks.js');
  await api.announce('components/tasks.js');
}
