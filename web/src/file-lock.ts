import { spawn } from 'node:child_process';
import type { ChildProcessByStdio } from 'node:child_process';
import type { Readable, Writable } from 'node:stream';

type LockChild = ChildProcessByStdio<Writable, Readable, Readable>;

const LOCK_MARKER = 'ADE_FILE_LOCK_ACQUIRED';

export async function withFileLock<T>(
  lockPath: string,
  mode: 'shared' | 'exclusive',
  operation: () => Promise<T>,
): Promise<T> {
  const lockMode = mode === 'exclusive' ? '-x' : '-s';
  const lockChild = spawn(
    '/usr/bin/flock',
    [lockMode, lockPath, process.execPath, ...process.execArgv, '-e', `process.stdout.write('${LOCK_MARKER}\\n'); process.stdin.resume();`],
    { stdio: ['pipe', 'pipe', 'pipe'], windowsHide: true },
  ) as LockChild;

  await new Promise<void>((resolvePromise, rejectPromise) => {
    let settled = false;
    let stdout = '';
    const reject = (error: Error) => {
      if (settled) return;
      settled = true;
      rejectPromise(error);
    };
    lockChild.once('error', reject);
    lockChild.stdout.setEncoding('utf8');
    lockChild.stdout.on('data', (chunk: string) => {
      stdout += chunk;
      if (!stdout.includes(LOCK_MARKER) || settled) return;
      settled = true;
      resolvePromise();
    });
    lockChild.once('close', (code, signal) => {
      if (settled) return;
      reject(new Error(`could not acquire ${mode} file lock: ${code ?? signal ?? 'child process closed'}`));
    });
  });

  try {
    return await operation();
  } finally {
    lockChild.stdin.end();
    await new Promise<void>((resolvePromise) => {
      if (lockChild.exitCode !== null || lockChild.signalCode !== null) {
        resolvePromise();
      } else {
        lockChild.once('close', () => resolvePromise());
      }
    });
  }
}
