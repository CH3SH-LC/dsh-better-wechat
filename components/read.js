// Stage two: every local text read. Disabling this component removes only the
// read tools; consent, the bridge and stage one keep working unchanged.
export const name = 'dsh-better-wechat-read';
export const inject = ['tools', 'wechatAssistant'];

export async function apply(ctx) {
  // Cordis starts this plugin only while `wechatAssistant` is provided by core.
  const api = ctx.get('wechatAssistant');
  api.register(ctx, 'components/read.js');
  await api.announce('components/read.js');
}
