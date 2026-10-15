import { failure } from './bridge.js';

const SCOPE_FIELDS = ['start_date', 'end_date', 'start_time', 'end_time', 'kind', 'kinds',
  'conversation_id', 'conversation_ids', 'exclude_conversation_ids', 'exclude_muted'];
const cancelled = () => failure('CANCELLED', '操作已取消。');

export function normalizeScope(args) {
  const scope = {};
  for (const key of ['start_date', 'end_date', 'start_time', 'end_time'])
    if (Object.hasOwn(args, key)) scope[key] = args[key];
  for (const [single, multiple] of [['kind', 'kinds'], ['conversation_id', 'conversation_ids']]) {
    if (Object.hasOwn(args, multiple)) scope[multiple] = args[multiple];
    else if (Object.hasOwn(args, single)) scope[multiple] = [args[single]];
  }
  if (Object.hasOwn(args, 'exclude_conversation_ids')) scope.exclude_conversation_ids = args.exclude_conversation_ids;
  if (Object.hasOwn(args, 'exclude_muted')) scope.exclude_muted = args.exclude_muted;
  for (const key of ['kinds', 'conversation_ids', 'exclude_conversation_ids']) {
    if (Array.isArray(scope[key])) scope[key] = [...new Set(scope[key])].sort();
  }
  return scope;
}

function withScope(args) {
  return { ...Object.fromEntries(Object.entries(args).filter(([key]) => !SCOPE_FIELDS.includes(key))),
    ...normalizeScope(args) };
}

/** Stage one is an explicit, parameterless local job. Stage two only reads
 * readable records: stale or empty results never launch collection implicitly.
 */
export function makeWorkflow(bridge) {
  const pending = new Map();
  let tail = Promise.resolve();
  async function update(action, signal) {
    if (signal?.aborted) return cancelled();
    let job = pending.get(action);
    if (!job) {
      const controller = new AbortController();
      let resolve, reject;
      job = { controller, cleanups: new Set(), settled: false,
        promise: new Promise((done, failed) => { resolve = done; reject = failed; }) };
      const active = job;
      const finish = (value, failed = false) => {
        if (active.settled) return;
        active.settled = true;
        if (pending.get(action) === active) pending.delete(action);
        for (const cleanup of active.cleanups) { try { cleanup(); } catch { /* no diagnostic data */ } }
        active.cleanups.clear();
        if (failed) reject(value); else resolve(value);
      };
      active.cancel = () => { controller.abort(); finish(cancelled()); };
      pending.set(action, active);
      const scheduled = tail.then(async () => {
        if (controller.signal.aborted) return cancelled();
        const value = await bridge(action, {}, controller.signal);
        return controller.signal.aborted ? cancelled() : value;
      });
      // Cancellation must not release the physical write barrier prematurely.
      tail = scheduled.catch(() => {});
      scheduled.then(value => finish(value), error => finish(error, true));
    }
    try {
      if (signal) {
        const cancel = () => job.cancel();
        signal.addEventListener('abort', cancel, { once: true });
        job.cleanups.add(() => signal.removeEventListener('abort', cancel));
        if (signal.aborted) job.cancel();
      }
    } catch { job.cancel(); }
    const value = await job.promise;
    return signal?.aborted ? cancelled() : value;
  }
  return async (action, args = {}, signal) => {
    if (signal?.aborted) return cancelled();
    if (['prepare', 'refresh'].includes(action)) {
      if (!args || Array.isArray(args) || typeof args !== 'object' || Object.keys(args).length)
        return failure('INVALID_ARGS', '第一步只接受固定全量操作；筛选参数请用于第二步读取。');
      return update(action, signal);
    }
    const queryArgs = ['status', 'search', 'review', 'context'].includes(action) ? withScope(args) : args;
    const result = await bridge(action, queryArgs, signal);
    return signal?.aborted ? cancelled() : result;
  };
}

