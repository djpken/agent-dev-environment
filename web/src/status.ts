import { spawn } from 'node:child_process';
import { randomUUID } from 'node:crypto';
import { readFile, mkdir, chmod, open, rename, rm, writeFile } from 'node:fs/promises';
import { join } from 'node:path';
import { withFileLock } from './file-lock.js';

const environmentVersion = '1';
const safePath = '/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin';
const runIdPattern = /^[0-9a-f-]{36}$/;

export interface StatusComponentConfig {
  id: string;
  label?: string;
  kind?: string;
  health: Record<string, unknown>;
  version_file?: string | null;
  version_command?: string[] | null;
  update_command?: string[] | null;
  target_version_arg?: string | null;
  restart_command?: string[] | null;
  rollback_command?: string[] | null;
  dependencies?: string[];
  enabled?: boolean;
  public?: boolean;
}

export interface StatusEnvironmentConfig {
  vm_id: string;
  public_origin: string;
  state_root: string;
  components: StatusComponentConfig[];
  wallets?: { authorized?: Array<{ address: string; role: string }> };
  snapshot?: { enabled?: boolean };
}

export class StatusError extends Error {
  constructor(
    readonly statusCode: number,
    message: string,
  ) {
    super(message);
  }
}

function isoNow(): string {
  return new Date().toISOString().replace(/\.\d{3}Z$/, 'Z');
}

function redact(value: string, limit = 280): string {
  const redacted = value
    .replace(/(token|secret|password|api[_-]?key|private[_-]?key)([=:])[^\s,;]+/gi, '$1$2[redacted]')
    .replace(/bearer\s+[^\s]+/gi, 'Bearer [redacted]')
    .trim()
    .replace(/\s+/g, ' ');
  return redacted.slice(-limit);
}

interface ProcessResult {
  code: number;
  stdout: string;
  stderr: string;
}

function runCommand(argv: string[], timeoutMs: number): Promise<ProcessResult> {
  return new Promise((resolvePromise, rejectPromise) => {
    if (!argv.length || !argv[0]) {
      rejectPromise(new Error('command is empty'));
      return;
    }
    const child = spawn(argv[0], argv.slice(1), {
      env: { PATH: safePath },
      stdio: ['ignore', 'pipe', 'pipe'],
      windowsHide: true,
    });
    const stdout: Buffer[] = [];
    const stderr: Buffer[] = [];
    let timedOut = false;
    const timer = setTimeout(() => {
      timedOut = true;
      child.kill('SIGKILL');
    }, timeoutMs);
    child.stdout.on('data', (chunk: Buffer) => stdout.push(chunk));
    child.stderr.on('data', (chunk: Buffer) => stderr.push(chunk));
    child.once('error', (error) => {
      clearTimeout(timer);
      rejectPromise(error);
    });
    child.once('close', (code) => {
      clearTimeout(timer);
      if (timedOut) {
        rejectPromise(new Error(`command timed out after ${timeoutMs / 1000} seconds`));
        return;
      }
      resolvePromise({
        code: code ?? 1,
        stdout: Buffer.concat(stdout).toString('utf8'),
        stderr: Buffer.concat(stderr).toString('utf8'),
      });
    });
  });
}

async function componentVersion(component: StatusComponentConfig): Promise<string | null> {
  try {
    let value: string;
    if (component.version_file) {
      value = (await readFile(component.version_file, 'utf8')).trim();
    } else if (component.version_command) {
      const result = await runCommand(component.version_command, 15_000);
      const output = (result.stdout || result.stderr).trim();
      if (result.code !== 0 || !output) return null;
      value = output.split(/\r?\n/, 1)[0];
    } else {
      return null;
    }
    return redact(value, 128) || null;
  } catch {
    return null;
  }
}

async function probeComponent(component: StatusComponentConfig): Promise<Record<string, unknown>> {
  const checkedAt = isoNow();
  const health = component.health || {};
  const type = health.type;
  try {
    let healthy: boolean;
    let detail: string;
    if (type === 'systemd') {
      const unit = typeof health.unit === 'string' ? health.unit : '';
      const result = await runCommand(['/usr/bin/systemctl', 'is-active', '--quiet', unit], 15_000);
      healthy = result.code === 0;
      detail = healthy ? 'active' : redact((result.stdout || result.stderr).trim() || 'inactive', 160);
    } else if (type === 'command') {
      const command = Array.isArray(health.command) ? health.command.filter((part): part is string => typeof part === 'string') : [];
      const result = await runCommand(command, 15_000);
      healthy = result.code === 0;
      detail = healthy ? 'ok' : redact((result.stderr || result.stdout).trim() || 'command failed', 160);
    } else {
      const url = typeof health.url === 'string' ? health.url : '';
      const headers = health.headers && typeof health.headers === 'object' && !Array.isArray(health.headers)
        ? health.headers as Record<string, string>
        : {};
      const response = await fetch(url, { headers, signal: AbortSignal.timeout(5_000) });
      healthy = response.status >= 200 && response.status < 400;
      detail = `HTTP ${response.status}`;
      await response.body?.cancel();
    }
    return {
      status: healthy ? 'healthy' : 'unhealthy',
      checked_at: checkedAt,
      detail,
      version: await componentVersion(component),
    };
  } catch (error) {
    return {
      status: 'unknown',
      checked_at: checkedAt,
      detail: redact(error instanceof Error ? error.message : String(error), 160),
      version: await componentVersion(component),
    };
  }
}

function publicComponent(component: StatusComponentConfig, probe: Record<string, unknown>): Record<string, unknown> {
  const rawStatus = probe.status;
  const status = ['healthy', 'unhealthy', 'unknown', 'degraded', 'updating'].includes(String(rawStatus))
    ? rawStatus as string
    : 'unknown';
  const detail = status === 'healthy' ? 'healthy' : status === 'unhealthy' ? 'unhealthy' : 'probe unavailable';
  return {
    id: component.id,
    label: component.label || component.id,
    kind: component.kind || 'service',
    status,
    version: probe.version ?? null,
    checked_at: probe.checked_at,
    detail,
    update_supported: component.update_command !== null && component.update_command !== undefined,
    target_version_supported: component.update_command !== null && component.update_command !== undefined &&
      component.target_version_arg !== null && component.target_version_arg !== undefined,
    restart_supported: component.restart_command !== null && component.restart_command !== undefined,
    rollback_supported: component.rollback_command !== null && component.rollback_command !== undefined,
    dependencies: component.dependencies || [],
    enabled: component.enabled === undefined || component.enabled === true,
  };
}

async function environmentHealth(config: StatusEnvironmentConfig): Promise<Record<string, unknown>> {
  const statuses: Record<string, unknown>[] = [];
  for (const component of config.components) {
    if ((component.enabled !== undefined && component.enabled !== true) ||
      (component.public !== undefined && component.public !== true)) continue;
    statuses.push(publicComponent(component, await probeComponent(component)));
  }
  const hasIssue = statuses.some((item) => item.status === 'unhealthy' || item.status === 'unknown');
  const hasHealthy = statuses.some((item) => item.status === 'healthy');
  return {
    service: 'agent-environment',
    version: environmentVersion,
    vm_id: config.vm_id,
    status: hasIssue ? (hasHealthy ? 'degraded' : 'unhealthy') : 'healthy',
    checked_at: isoNow(),
    components: statuses,
  };
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value);
}

function publicRun(runValue: unknown, publicIds: Set<string>): Record<string, unknown> | undefined {
  if (!isRecord(runValue)) return undefined;
  const fields = [
    'id', 'trigger', 'action', 'status', 'created_at', 'started_at', 'finished_at',
    'error', 'publication', 'target_versions',
  ];
  const result: Record<string, unknown> = {};
  for (const field of fields) {
    if (field in runValue) result[field] = runValue[field];
  }
  const componentIds = Array.isArray(runValue.component_ids)
    ? runValue.component_ids.filter((id) => publicIds.has(String(id)))
    : [];
  result.component_ids = componentIds;
  result.components = Array.isArray(runValue.components)
    ? runValue.components
      .filter((item) => isRecord(item) && publicIds.has(String(item.id)))
      .map((item) => {
        const selected: Record<string, unknown> = {};
        for (const field of ['id', 'status', 'action', 'returncode', 'version', 'health', 'rollback', 'detail']) {
          if (field in item) selected[field] = item[field];
        }
        return selected;
      })
    : [];
  return result;
}

async function readRuns(stateRoot: string): Promise<unknown[]> {
  await mkdir(stateRoot, { recursive: true, mode: 0o2750 });
  try {
    return await withFileLock(join(stateRoot, '.runs.lock'), 'exclusive', async () => {
      let state: unknown;
      try {
        state = JSON.parse(await readFile(join(stateRoot, 'runs.json'), 'utf8')) as unknown;
      } catch (error) {
        if ((error as NodeJS.ErrnoException).code === 'ENOENT') return [];
        throw new StatusError(502, 'invalid update run state');
      }
      if (!isRecord(state) || (state.runs !== undefined && !Array.isArray(state.runs))) {
        throw new StatusError(502, 'invalid update run state');
      }
      return (state.runs || []) as unknown[];
    });
  } catch (error) {
    if (error instanceof StatusError) throw error;
    throw new StatusError(502, 'update run state is unavailable');
  }
}

export async function buildStatus(
  config: StatusEnvironmentConfig,
  policy: Record<string, unknown>,
): Promise<Record<string, unknown>> {
  const [health, runs] = await Promise.all([
    environmentHealth(config),
    readRuns(config.state_root),
  ]);
  const publicIds = new Set(config.components
    .filter((component) => component.public === undefined || component.public === true)
    .map((component) => component.id));
  return {
    config: {
      version: environmentVersion,
      vm_id: config.vm_id,
      public_origin: config.public_origin,
      registration_required: !(config.wallets?.authorized || []).length,
      schedule: { time: '04:00', timezone: 'UTC+8', persistent: true },
      snapshot: { enabled: Boolean(config.snapshot?.enabled) },
    },
    health,
    runs: runs.slice(0, 50).map((run) => publicRun(run, publicIds)).filter(Boolean),
    policy,
  };
}

function sortKeys(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(sortKeys);
  if (!isRecord(value)) return value;
  return Object.fromEntries(Object.keys(value).sort().map((key) => [key, sortKeys(value[key])]));
}

async function writeRunState(path: string, value: unknown): Promise<void> {
  const temporary = join(path, '..', `.environment-${randomUUID()}`);
  try {
    await writeFile(temporary, `${JSON.stringify(sortKeys(value), null, 2)}\n`, { mode: 0o640, flag: 'wx' });
    await chmod(temporary, 0o640);
    await rename(temporary, path);
  } catch (error) {
    await rm(temporary, { force: true }).catch(() => undefined);
    throw error;
  }
}

export async function failRun(stateRoot: string, runId: string, errorMessage: string): Promise<Record<string, unknown>> {
  if (!runIdPattern.test(runId)) throw new StatusError(400, 'invalid update run id');
  await mkdir(stateRoot, { recursive: true, mode: 0o2750 });
  try {
    return await withFileLock(join(stateRoot, '.runs.lock'), 'exclusive', async () => {
      let state: unknown;
      try {
        state = JSON.parse(await readFile(join(stateRoot, 'runs.json'), 'utf8')) as unknown;
      } catch {
        throw new StatusError(502, 'invalid update run state');
      }
      if (!isRecord(state) || !Array.isArray(state.runs)) throw new StatusError(502, 'invalid update run state');
      const index = state.runs.findIndex((run) => isRecord(run) && run.id === runId);
      if (index < 0) throw new StatusError(502, `update run not found: ${runId}`);
      const run = state.runs[index];
      if (!isRecord(run)) throw new StatusError(502, 'invalid update run state');
      const updated = { ...run, status: 'failed', finished_at: isoNow(), error: redact(errorMessage) };
      const runs = [...state.runs];
      runs[index] = updated;
      await writeRunState(join(stateRoot, 'runs.json'), { ...state, runs });
      if (['completed', 'partial_failure', 'failed', 'blocked'].includes(String(updated.status))) {
        const historyPath = join(stateRoot, 'runs.jsonl');
        const history = await open(historyPath, 'a', 0o640);
        try {
          await history.write(`${JSON.stringify(sortKeys(updated))}\n`, undefined, 'utf8');
          await history.chmod(0o640);
        } finally {
          await history.close();
        }
      }
      return updated;
    });
  } catch (error) {
    if (error instanceof StatusError) throw error;
    throw new StatusError(502, 'could not update failed run state');
  }
}
