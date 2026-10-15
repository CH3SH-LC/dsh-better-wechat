// DSH 0.2.0-rc.2 emits session/event(session, event) after appending an event.
// A turn starts at turn/start and ends at turn/end. New inbox insertions may
// happen inside that same runtime turn, so they start a fresh consent scope.
// Only event types, sequence numbers and insertion counts are inspected here.
const contexts = new WeakMap();
// Cordis returns a fresh caller-tracing proxy for each service lookup. Its
// documented original symbol identifies the underlying service across reads.
// Keep calling request on the proxy so Cordis retains the caller context.
const CORDIS_ORIGINAL = Symbol.for('cordis.original');
const READS = new Set(['search', 'review', 'context', 'list_tasks', 'conversations', 'get_preferences']);
const WRITES = new Set(['save_tasks', 'set_preferences']);
const SCOPED = new Set([...READS, 'prepare', 'refresh']);
const isObject = value => value !== null && ['object', 'function'].includes(typeof value);
const aborted = signal => signal?.aborted === true;
const serviceIdentity = approval => typeof approval?.request === 'function'
  ? approval[CORDIS_ORIGINAL] ?? approval : null;

// Ask only the host's public, effective session-preset service. Never infer
// Auto from a tool argument, model text, environment variable or approval
// policy (Auto and ordinary sessions can both have policy "ask"). The host's
// tools/pre-execute Auto review still runs before this plugin body; this only
// removes our redundant manual prompt after that host gate has allowed it.
function isAutoSession(ctx, agent) {
  try {
    if (!isObject(agent?.session)) return false;
    const presets = ctx.get?.('permissionPresets');
    return typeof presets?.current === 'function' && presets.current(agent.session) === 'auto';
  } catch { return false; }
}

function revoke(record) {
  if (record.status === 'pending') record.cancel();
  else {
    record.status = 'invalid';
    record.scope?.state.records.delete(record);
    if (record.manager.agents.get(record.agent) === record) record.manager.agents.delete(record.agent);
  }
}

function clearState(state) {
  if (!state) return;
  for (const record of [...state.records]) revoke(record);
  state.records.clear();
}

function sessionEvent(manager, session, event) {
  if (manager.disposed || !isObject(session)) return;
  const previous = manager.sessions.get(session);
  let seq, type, inserted;
  try {
    seq = event?.seq;
    type = event?.type;
    inserted = type === 'agent/inbox/spliced' ? event.data?.inserted : undefined;
    if (!Number.isSafeInteger(seq) || seq < 0 || typeof type !== 'string'
        || !Number.isSafeInteger(session.seq) || session.seq < seq + 1) throw new Error();
    if (type === 'agent/inbox/spliced' && !Array.isArray(inserted)) throw new Error();
  } catch {
    clearState(previous);
    if (previous) previous.active = false;
    for (const pending of [...manager.pending]) if (pending.session === session) pending.cancel();
    return;
  }
  const boundary = type === 'turn/start' || type === 'turn/end'
    || (type === 'agent/inbox/spliced' && inserted.length > 0);
  if (boundary) {
    clearState(previous);
    for (const pending of [...manager.pending]) if (pending.session === session) pending.cancel();
  }
  if (type === 'turn/start') {
    manager.sessions.set(session, { startSeq: seq, lastSeq: seq, generation: 0, active: true, records: new Set() });
  } else if (previous) {
    if (seq !== previous.lastSeq + 1) {
      clearState(previous);
      previous.active = false;
    }
    previous.lastSeq = seq;
    if (type === 'turn/end') previous.active = false;
    else if (type === 'agent/inbox/spliced' && inserted.length > 0) previous.generation++;
  }
}

/** Subscribe while the plugin is registered, before a user's next turn starts.
 * Missing lifecycle APIs or a missed turn/start deliberately disable caching.
 */
export function initializeConsent(ctx) {
  if (!isObject(ctx)) return undefined;
  if (contexts.has(ctx)) return contexts.get(ctx);
  const manager = { agents: new WeakMap(), sessions: new WeakMap(), pending: new Set(), tracking: false, disposed: false,
    approvalIdentity: () => serviceIdentity(ctx.get?.('approval')) };
  contexts.set(ctx, manager);
  if (typeof ctx.on !== 'function' || typeof ctx.effect !== 'function') return manager;
  let off;
  try {
    off = ctx.on('session/event', (session, event) => sessionEvent(manager, session, event));
    ctx.effect(() => () => {
      manager.disposed = true;
      manager.tracking = false;
      for (const pending of [...manager.pending]) pending.cancel();
    }, 'wechat.consent()');
    manager.tracking = true;
  } catch {
    if (typeof off === 'function') off();
  }
  return manager;
}

function currentScope(manager, agent) {
  try {
    if (!manager?.tracking || manager.disposed) return null;
    const session = agent.session;
    if (!isObject(session)) return null;
    const state = manager.sessions.get(session);
    // Never infer a turn by reading the event log, a call ID or tool signal.
    // A missing event, sequence rollback or replaced session cannot reuse it.
    if (!state?.active || session.seq !== state.lastSeq + 1) return null;
    return { session, state, startSeq: state.startSeq, generation: state.generation };
  } catch { return null; }
}

function sameScope(left, right) {
  return Boolean(left && right && left.session === right.session && left.state === right.state
    && left.startSeq === right.startSeq && left.generation === right.generation);
}

function reason(action, scoped) {
  if (action === 'set_preferences') return '按你的要求完整替换本机长期不关注名单；影响以后查询、上下文及待办证据读取，不影响固定全量采集，不删除已有记录。此操作每次单独确认，不与聊天读取授权混用。';
  if (action === 'save_tasks') return '把有消息依据的候选待办保存到本机，供你核对；不会执行任务。此操作每次单独确认。';
  const duration = scoped ? '允许本轮用户请求内的微信处理及后续分页；本轮结束或收到新输入后失效。'
    : '允许本次微信处理；当前无法确认可靠轮次标识，后续调用仍会分别确认。';
  return duration + '范围包括准备、刷新、检索、复核分页、上下文、会话列表、偏好查看和候选待办读取。'
    + '第一步原生连接按固定脚本只读访问登录微信的进程和数据库，全量转换并更新本机可读索引；第二步只从该索引筛选真实消息和联系人名称，返回给当前 DSH 模型。'
    + '不会发送消息或执行任务，保存候选待办和修改长期不关注名单仍单独确认；不更改 DSH 全局权限。';
}

function beginRequest(manager, approval, agent, spec, execution, scope, approvalIdentity) {
  const controller = new AbortController();
  const cleanups = new Set();
  let resolve;
  const promise = new Promise(done => { resolve = done; });
  const record = { manager, agent, scope, session: agent.session, approvalIdentity, status: 'pending', promise, cleanups };
  const finish = granted => {
    if (record.status !== 'pending') return;
    try {
      granted = granted && !controller.signal.aborted && !manager.disposed
        && manager.approvalIdentity() === approvalIdentity
        && (!scope || sameScope(scope, currentScope(manager, agent)));
    } catch { granted = false; }
    record.status = granted ? 'granted' : 'invalid';
    manager.pending.delete(record);
    for (const cleanup of cleanups) { try { cleanup(); } catch { /* no diagnostic data */ } }
    cleanups.clear();
    if (!granted || !scope) {
      scope?.state.records.delete(record);
      if (manager.agents.get(agent) === record) manager.agents.delete(agent);
    }
    resolve(granted);
  };
  record.cancel = () => { controller.abort(); finish(false); };
  manager.pending.add(record);
  if (scope) {
    manager.agents.set(agent, record);
    scope.state.records.add(record);
  }
  // Queue the callback so all initial cancellation listeners exist first.
  Promise.resolve().then(async () => {
    if (record.status !== 'pending') return;
    try {
      const outcome = await approval.request({ agent, toolName: execution.name || spec.name,
        callId: execution.callId, reason: reason(spec.action, Boolean(scope)), signal: controller.signal });
      finish(outcome === 'allowed-once');
    } catch { finish(false); }
  });
  return record;
}

function awaitRequest(record, signal) {
  try {
    if (aborted(signal)) record.cancel();
    else if (signal) {
      const cancel = () => record.cancel();
      signal.addEventListener('abort', cancel, { once: true });
      const cleanup = () => signal.removeEventListener('abort', cancel);
      record.cleanups.add(cleanup);
      if (aborted(signal)) record.cancel();
    }
  } catch { record.cancel(); }
  return record.promise.then(granted => {
    if (aborted(signal)) { revoke(record); return false; }
    return granted && record.status === 'granted'
      && (!record.scope || sameScope(record.scope, currentScope(record.manager, record.agent)));
  });
}

/** One in-memory grant per agent, session, runtime turn and user-input batch. */
export async function approve(ctx, spec, execution, config = {}) {
  let manager, agent;
  try {
    agent = execution?.agent;
    manager = initializeConsent(ctx);
    if (aborted(execution?.signal)) {
      const old = isObject(agent) ? manager?.agents.get(agent) : undefined;
      if (old) revoke(old);
      return false;
    }
    if (spec.action === 'status') return true;
    if (!isObject(agent) || !manager || manager.disposed) return false;
    if (!SCOPED.has(spec.action) && !WRITES.has(spec.action)) return false;
    if (SCOPED.has(spec.action) && isAutoSession(ctx, agent)) {
      const signal = execution.signal;
      if (signal && (typeof signal.addEventListener !== 'function' || typeof signal.removeEventListener !== 'function')) return false;
      const old = manager.agents.get(agent);
      if (old) revoke(old);
      // No cached grant: consult the effective session mode on every call.
      // Preference/task writes deliberately remain outside this allowance.
      return !aborted(signal);
    }
    if (READS.has(spec.action) && config.confirmReads === false) return true;
    const approval = ctx.get?.('approval');
    if (typeof approval?.request !== 'function') {
      const old = manager.agents.get(agent);
      if (old) revoke(old);
      return false;
    }
    const approvalIdentity = serviceIdentity(approval);
    const scope = SCOPED.has(spec.action) ? currentScope(manager, agent) : null;
    if (SCOPED.has(spec.action)) {
      const old = manager.agents.get(agent);
      if (old && (old.approvalIdentity !== approvalIdentity || !sameScope(old.scope, scope))) revoke(old);
      else if (old?.status === 'granted') return true;
      else if (old?.status === 'pending') return await awaitRequest(old, execution.signal);
    }
    return await awaitRequest(beginRequest(manager, approval, agent, spec, execution, scope, approvalIdentity), execution.signal);
  } catch {
    const old = isObject(agent) ? manager?.agents.get(agent) : undefined;
    if (old) revoke(old);
    return false;
  }
}
