// Local long-term "not interested" list. Reading it follows the shared
// per-turn read grant; replacing it is always confirmed on its own.
export const name = 'dsh-better-wechat-preferences';
export const inject = ['tools', 'wechatAssistant'];

export async function apply(ctx) {
  // Cordis starts this plugin only while `wechatAssistant` is provided by core.
  const api = ctx.get('wechatAssistant');
  api.register(ctx, 'components/preferences.js');
  await api.announce('components/preferences.js');
}
