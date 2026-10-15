// Stage one: the two parameterless local collection operations. Disabling this
// component removes only `wechat_prepare` and `wechat_refresh`; every stage-two
// read stays available against whatever the local index already holds.
export const name = 'dsh-better-wechat-stage-one';
export const inject = ['tools', 'wechatAssistant'];

export async function apply(ctx) {
  // Cordis starts this plugin only while `wechatAssistant` is provided by core.
  const api = ctx.get('wechatAssistant');
  api.register(ctx, 'components/stage-one.js');
  await api.announce('components/stage-one.js');
}
