import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
const script = fileURLToPath(new URL('./release.py', import.meta.url));
let ran = false;
for (const [command, prefix] of process.platform === 'win32' ? [['python', []], ['py', ['-3']]] : [['python3', []], ['python', []]]) {
  const probe = spawnSync(command, [...prefix, '-c', 'import sys;sys.exit(0 if sys.version_info >= (3,10) else 1)'], { windowsHide: true, timeout: 10000, stdio: 'ignore' });
  if (probe.status !== 0) continue;
  ran = true;
  const result = spawnSync(command, [...prefix, '-X', 'utf8', '-B', script], { windowsHide: true, timeout: 60000, encoding: 'utf8' });
  process.stdout.write(result.stdout || JSON.stringify({ ok: false, code: 'PACKAGING_FAILED' }) + '\n');
  process.exitCode = result.status === 0 ? 0 : 1;
  break;
}
if (!ran) { process.stdout.write(JSON.stringify({ ok: false, code: 'PYTHON_REQUIRED' }) + '\n'); process.exitCode = 1; }
