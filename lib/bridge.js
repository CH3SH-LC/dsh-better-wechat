import { spawn } from 'node:child_process';
import { existsSync } from 'node:fs';
import { homedir } from 'node:os';
import { dirname, join, resolve, sep } from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = dirname(dirname(fileURLToPath(import.meta.url)));
const BRIDGE = join(ROOT, 'python', 'bridge.py');
const LIMIT = 512 * 1024;
const SAFE_ENV = ['PATH', 'Path', 'PATHEXT', 'SystemRoot', 'SYSTEMROOT', 'WINDIR',
  'TEMP', 'TMP', 'HOME', 'USERPROFILE', 'LOCALAPPDATA', 'APPDATA', 'XDG_DATA_HOME',
  'USERNAME', 'USER', 'LANG', 'LC_ALL'];

// The project was renamed from dsh-wechat-assistant. A rename must never orphan
// an already imported private index (messages, captures and drafts), so an
// existing directory from the previous name keeps being used. Nothing is moved
// or deleted, and DSH_WECHAT_STATE_DIR still overrides both names.
export const STATE_DIRECTORY_NAMES = ['dsh-better-wechat', 'dsh-wechat-assistant'];

export function defaultStateDirectory(env = process.env, home = homedir(), platform = process.platform) {
  if (env.DSH_WECHAT_STATE_DIR) return expandPath(env.DSH_WECHAT_STATE_DIR, home);
  const parent = platform === 'win32' ? (env.LOCALAPPDATA || join(home, 'AppData', 'Local'))
    : platform === 'darwin' ? join(home, 'Library', 'Application Support')
      : (env.XDG_DATA_HOME || join(home, '.local', 'share'));
  for (const name of STATE_DIRECTORY_NAMES) {
    const candidate = join(parent, name);
    if (existsSync(candidate)) return candidate;
  }
  return join(parent, STATE_DIRECTORY_NAMES[0]);
}

function expandPath(value, home = homedir()) {
  if (value === '~') return resolve(home);
  if (/^~[\\/]/.test(value)) return resolve(home, value.slice(2));
  return resolve(value);
}

export function stateDirectory(config = {}) {
  const state = config.stateDirectory ? expandPath(config.stateDirectory) : defaultStateDirectory();
  const relative = state.toLowerCase();
  const root = resolve(ROOT).toLowerCase();
  if (relative === root || relative.startsWith(root + sep)) throw new Error('PRIVATE_STATE_INSIDE_PACKAGE');
  return state;
}

export const failure = (code, message) => ({ ok: false, error: { code, message } });

function childEnv() {
  const env = {};
  for (const key of SAFE_ENV) if (typeof process.env[key] === 'string') env[key] = process.env[key];
  env.PYTHONIOENCODING = 'utf-8';
  env.PYTHONUTF8 = '1';
  env.PYTHONDONTWRITEBYTECODE = '1';
  return env;
}

function run(command, args, stdin, signal, timeoutMs, maxOutput = LIMIT) {
  return new Promise(resolveResult => {
    let child, chunks = [], size = 0, finished = false, timer;
    const finish = result => {
      if (finished) return;
      finished = true;
      clearTimeout(timer);
      signal?.removeEventListener('abort', abort);
      resolveResult(result);
    };
    const stop = code => { child?.kill(); finish({ error: code }); };
    const abort = () => stop('CANCELLED');
    if (signal?.aborted) return finish({ error: 'CANCELLED' });
    try {
      child = spawn(command, args, { shell: false, windowsHide: true, cwd: ROOT,
        env: childEnv(), stdio: ['pipe', 'pipe', 'pipe'] });
      child.on('error', () => finish({ error: 'START_FAILED' }));
      child.stdout.on('data', chunk => {
        size += chunk.length;
        if (size > maxOutput) return stop('OUTPUT_LIMIT');
        chunks.push(chunk);
      });
      // Never forward subprocess diagnostics: they can contain local paths.
      child.stderr.on('data', () => {});
      child.on('close', code => finish({ code, stdout: Buffer.concat(chunks).toString('utf8') }));
      child.stdin.on('error', () => {});
      child.stdin.end(stdin);
      timer = setTimeout(() => stop('TIMEOUT'), timeoutMs);
      signal?.addEventListener('abort', abort, { once: true });
    } catch { finish({ error: 'START_FAILED' }); }
  });
}

export function makeBridge(config = {}) {
  let selected;
  async function interpreter(signal) {
    if (selected) return selected;
    const candidates = config.pythonExecutable
      ? [[config.pythonExecutable, []]]
      : process.platform === 'win32' ? [['python', []], ['py', ['-3']]] : [['python3', []], ['python', []]];
    for (const [command, prefix] of candidates) {
      if (signal?.aborted) return null;
      const p = await run(command, [...prefix, '-X', 'utf8', '-B', '-c',
        'import sys,sqlite3;print("READY" if sys.version_info >= (3,10) else "OLD")'], '', signal, 7000, 100);
      if (p.code === 0 && p.stdout?.trim() === 'READY') return selected = { command, prefix };
    }
    return null;
  }
  return async (action, params = {}, signal) => {
    let state;
    try { state = stateDirectory(config); }
    catch { return failure('PRIVATE_STATE_REQUIRED', '请把本地数据目录设在插件代码目录之外。'); }
    const python = await interpreter(signal);
    if (!python) return failure(signal?.aborted ? 'CANCELLED' : 'PYTHON_UNAVAILABLE',
      signal?.aborted ? '操作已取消。' : '需要 Python 3.10 或更高版本；请先完成本地配置。');
    const result = await run(python.command, [...python.prefix, '-X', 'utf8', '-B', BRIDGE, '--state', state],
      JSON.stringify({ action, params }) + '\n', signal, ['prepare', 'refresh'].includes(action) ? 1240000 : 20000);
    if (result.error) return failure(['CANCELLED', 'TIMEOUT'].includes(result.error) ? result.error : 'LOCAL_ENGINE_UNAVAILABLE',
      result.error === 'CANCELLED' ? '操作已取消。' : '本地检索没有完成；请检查导入状态。');
    try {
      const value = JSON.parse(result.stdout);
      if (!value || typeof value !== 'object' || typeof value.ok !== 'boolean') throw new Error();
      return value;
    } catch { return failure('LOCAL_ENGINE_UNAVAILABLE', '本地检索没有返回有效结果。'); }
  };
}
