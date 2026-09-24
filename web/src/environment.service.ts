import { createHash, randomBytes, randomUUID } from 'node:crypto';
import { spawn } from 'node:child_process';
import { Inject, Injectable } from '@nestjs/common';
import type { Request } from 'express';
import { buildStatus, failRun, StatusError } from './status.js';
import { ENVIRONMENT_CONFIG, loadConfig, type EnvironmentConfig, type WalletRole } from './config.js';
import { ApiError } from './errors.js';
import type { PolicyDto, SignatureDto, UpdateRunDto } from './dto.js';

const CHILD_OUTPUT_LIMIT = 16 * 1024 * 1024;

interface CommandResult {
  stdout: string;
  stderr: string;
}

interface RegistrationChallenge {
  address: string;
  message: string;
  expiresAt: number;
}

interface CachedValue<T> {
  expiresAt: number;
  value: T;
}

export interface SessionRequest extends Request {
  walletSession?: Record<string, unknown>;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value);
}

function isoNow(): string {
  return new Date().toISOString().replace(/\.\d{3}Z$/, 'Z');
}

function normalizeAddress(value: unknown): string {
  if (typeof value !== 'string' || !/^0x[0-9a-fA-F]{40}$/.test(value)) {
    throw new ApiError(400, 'wallet address must be a 0x-prefixed Ethereum address');
  }
  return value.toLowerCase();
}

function statusFromAuthorizationError(message: string): number {
  if (message.includes('expired') || message.includes('session') || message.includes('enrolled')) {
    return 401;
  }
  if (message.includes('role') || message.includes('origin is not allowed')) {
    return 403;
  }
  if (message.includes('registration is already complete')) {
    return 409;
  }
  return 400;
}

export function readBearer(request: Request): string {
  const authorization = request.headers.authorization;
  if (typeof authorization !== 'string' || !authorization.startsWith('Bearer ')) {
    throw new ApiError(401, 'wallet session required');
  }
  return authorization.slice('Bearer '.length).trim();
}

function runProcess(
  executable: string,
  args: string[],
  input?: string,
  timeoutMs = 30000,
  outputLimit = CHILD_OUTPUT_LIMIT,
): Promise<CommandResult> {
  return new Promise((resolvePromise, rejectPromise) => {
    const child = spawn(executable, args, {
      cwd: process.env.ADE_SOURCE_ROOT || process.cwd(),
      env: process.env,
      stdio: ['pipe', 'pipe', 'pipe'],
      windowsHide: true,
    });
    const stdout: Buffer[] = [];
    const stderr: Buffer[] = [];
    let stdoutBytes = 0;
    let stderrBytes = 0;
    let failure: Error | undefined;
    const timer = setTimeout(() => {
      failure = new Error('child process timed out');
      child.kill('SIGKILL');
    }, timeoutMs);

    child.stdout.on('data', (chunk: Buffer) => {
      stdoutBytes += chunk.length;
      if (stdoutBytes > outputLimit) {
        failure = new Error('child process output exceeded its limit');
        child.kill('SIGKILL');
      } else {
        stdout.push(chunk);
      }
    });
    child.stderr.on('data', (chunk: Buffer) => {
      stderrBytes += chunk.length;
      if (stderrBytes > outputLimit) {
        failure = new Error('child process output exceeded its limit');
        child.kill('SIGKILL');
      } else {
        stderr.push(chunk);
      }
    });
    child.stdin.on('error', () => undefined);
    child.once('error', (error) => {
      clearTimeout(timer);
      rejectPromise(error);
    });
    child.once('close', (code) => {
      clearTimeout(timer);
      const result = {
        stdout: Buffer.concat(stdout).toString('utf8'),
        stderr: Buffer.concat(stderr).toString('utf8'),
      };
      if (failure) {
        rejectPromise(failure);
      } else if (code !== 0) {
        rejectPromise(new Error('child process exited with code ' + String(code)));
      } else {
        resolvePromise(result);
      }
    });
    if (input === undefined) child.stdin.end();
    else child.stdin.end(input, 'utf8');
  });
}

function parseJsonOutput<T>(output: string, label: string): T {
  try {
    return JSON.parse(output) as T;
  } catch {
    throw new ApiError(502, label + ' returned invalid JSON');
  }
}

@Injectable()
export class EnvironmentService {
  private config: EnvironmentConfig;
  private statusCache: CachedValue<Promise<Record<string, unknown>>> | undefined;
  private readonly registrationChallenges = new Map<string, RegistrationChallenge>();
  private readonly authenticationFailures = new Map<string, number[]>();

  constructor(@Inject(ENVIRONMENT_CONFIG) config: EnvironmentConfig) {
    this.config = config;
  }

  get vmId(): string {
    return this.config.vm_id;
  }

  get artifactsConfiguration() {
    return this.config.artifacts || { enabled: false };
  }

  async authorization<T>(operation: string, payload: Record<string, unknown> = {}): Promise<T> {
    const helper = this.config.authorization_trigger || '/usr/local/sbin/agent-environment-authorize';
    let result: CommandResult;
    try {
      result = await runProcess(
        '/usr/bin/sudo',
        ['-n', helper],
        JSON.stringify({ ...payload, operation }),
        30000,
        1024 * 1024,
      );
    } catch {
      throw new ApiError(503, 'wallet authorization helper is unavailable');
    }
    const value = parseJsonOutput<Record<string, unknown>>(result.stdout, 'wallet authorization helper');
    if (typeof value.error === 'string') {
      throw new ApiError(statusFromAuthorizationError(value.error), value.error);
    }
    return value as T;
  }

  async requireSession(request: Request, minimumRole: WalletRole = 'viewer'): Promise<Record<string, unknown>> {
    return this.authorization<Record<string, unknown>>('session', {
      token: readBearer(request),
      minimum_role: minimumRole,
    });
  }

  async status(token: string): Promise<Record<string, unknown>> {
    if (this.statusCache && this.statusCache.expiresAt > Date.now()) return this.statusCache.value;
    const pending = Promise.all([
      buildStatus(this.config, {}),
      this.authorization<Record<string, unknown>>('policy_status', { token }),
    ]).then(([status, policy]) => ({ ...status, policy }));
    this.statusCache = { expiresAt: Date.now() + 1000, value: pending };
    try {
      const result = await pending;
      this.statusCache = { expiresAt: Date.now() + 1000, value: Promise.resolve(result) };
      return result;
    } catch (error) {
      this.statusCache = undefined;
      if (error instanceof ApiError || error instanceof StatusError) throw error;
      throw new ApiError(502, 'ADE status could not be loaded');
    }
  }

  healthz(): Record<string, unknown> {
    return {
      status: 'ok',
      service: 'agent-environment',
      vm_id: this.config.vm_id,
      registration_required: !(this.config.wallets?.authorized || []).length,
    };
  }

  registrationStatus(): Record<string, unknown> {
    const authorized = this.config.wallets?.authorized || [];
    return { required: !authorized.length, vm_id: this.config.vm_id, role: authorized.length ? null : 'admin' };
  }

  registrationChallenge(addressValue: unknown): Record<string, unknown> {
    if ((this.config.wallets?.authorized || []).length) {
      throw new ApiError(409, 'registration is already complete');
    }
    const address = normalizeAddress(addressValue);
    this.cleanChallenges();
    const issuedAt = isoNow();
    const expiresAt = new Date(Date.now() + 10 * 60 * 1000).toISOString().replace(/\.\d{3}Z$/, 'Z');
    const challengeId = randomUUID();
    const nonce = randomBytes(24).toString('base64url');
    const vmBinding = createHash('sha256').update(this.config.vm_id, 'utf8').digest('hex');
    const payload = {
      action: 'register', address, expires_at: expiresAt, issued_at: issuedAt,
      nonce, role: 'admin', vm_binding: vmBinding,
    };
    const message = 'ADE-ENVIRONMENT-REGISTRATION-V1\n' + JSON.stringify(payload);
    this.registrationChallenges.set(challengeId, { address, message, expiresAt: Date.parse(expiresAt) });
    this.cleanChallenges();
    return { challenge_id: challengeId, address, role: 'admin', message, issued_at: issuedAt, expires_at: expiresAt };
  }

  async registerWallet(payload: SignatureDto): Promise<Record<string, unknown>> {
    const address = normalizeAddress(payload.address);
    this.cleanChallenges();
    const challenge = this.registrationChallenges.get(payload.challenge_id);
    if (!challenge || challenge.address !== address || challenge.message !== payload.message) {
      throw new ApiError(400, 'registration challenge is invalid');
    }
    const helper = this.config.registration_trigger || '/usr/local/sbin/agent-environment-enroll';
    let result: CommandResult;
    try {
      result = await runProcess(
        '/usr/bin/sudo', ['-n', helper],
        JSON.stringify({ address, message: payload.message, signature: payload.signature }),
        65000, 1024 * 1024,
      );
    } catch {
      throw new ApiError(400, 'wallet registration failed');
    }
    parseJsonOutput<Record<string, unknown>>(result.stdout, 'wallet registration helper');
    this.registrationChallenges.delete(payload.challenge_id);
    this.config = this.loadCurrentConfig();
    this.statusCache = undefined;
    return { registered: true, address, role: 'admin', login_required: true };
  }

  async authChallenge(addressValue: unknown, origin?: string): Promise<Record<string, unknown>> {
    const address = normalizeAddress(addressValue);
    return this.authorization<Record<string, unknown>>('challenge', {
      address,
      ...(origin === undefined ? {} : { origin }),
    });
  }

  async verifyAuth(payload: SignatureDto, ip: string): Promise<Record<string, unknown>> {
    const address = normalizeAddress(payload.address);
    if (!this.authFailureAllowed(ip)) throw new ApiError(429, 'too many authentication failures');
    try {
      return await this.authorization<Record<string, unknown>>('login', {
        challenge_id: payload.challenge_id,
        address,
        message: payload.message,
        signature: payload.signature,
      });
    } catch (error) {
      this.noteAuthFailure(ip);
      throw error;
    }
  }

  logout(request: Request): Promise<Record<string, unknown>> {
    return this.authorization<Record<string, unknown>>('logout', { token: readBearer(request) });
  }

  async startUpdate(request: Request, body: UpdateRunDto): Promise<Record<string, unknown>> {
    const run = await this.authorization<Record<string, unknown>>('authorize_run', {
      token: readBearer(request),
      action: body.action || 'update',
      component_ids: body.component_ids,
      target_versions: body.target_versions || {},
    });
    const runId = run.id;
    if (typeof runId !== 'string') throw new ApiError(502, 'wallet authorization helper returned an invalid run');
    const trigger = this.config.manual_trigger || '/usr/local/sbin/agent-environment-trigger';
    const child = spawn('/usr/bin/sudo', ['-n', trigger, runId], {
      detached: true,
      stdio: 'ignore',
      windowsHide: true,
    });
    try {
      await new Promise<void>((resolvePromise, rejectPromise) => {
        child.once('spawn', () => resolvePromise());
        child.once('error', rejectPromise);
      });
      child.unref();
    } catch {
      try {
        await failRun(this.config.state_root, runId, 'privileged updater trigger could not start');
      } catch (error) {
        console.error('failed to mark update run as failed', { error, runId });
      }
      throw new ApiError(400, 'update run was queued but could not start');
    }
    return run;
  }

  savePolicy(request: Request, body: PolicyDto): Promise<Record<string, unknown>> {
    const fields = ['enabled', 'components', 'target_versions', 'allow_restart', 'release_channel', 'expires_at'];
    const payload: Record<string, unknown> = { token: readBearer(request) };
    for (const field of fields) {
      if (field in body) payload[field] = body[field as keyof PolicyDto];
    }
    return this.authorization<Record<string, unknown>>('save_policy', payload);
  }

  private loadCurrentConfig(): EnvironmentConfig {
    return loadConfig(this.config.path);
  }

  private cleanChallenges(): void {
    const now = Date.now();
    for (const [id, challenge] of this.registrationChallenges) {
      if (challenge.expiresAt <= now) this.registrationChallenges.delete(id);
    }
    while (this.registrationChallenges.size > 32) {
      const oldest = this.registrationChallenges.keys().next().value;
      if (oldest) this.registrationChallenges.delete(oldest);
      else break;
    }
  }

  private authFailureAllowed(address: string): boolean {
    const cutoff = Date.now() - 5 * 60 * 1000;
    const recent = (this.authenticationFailures.get(address) || []).filter((time) => time > cutoff);
    this.authenticationFailures.set(address, recent);
    return recent.length < 10;
  }

  private noteAuthFailure(address: string): void {
    const values = this.authenticationFailures.get(address) || [];
    values.push(Date.now());
    this.authenticationFailures.set(address, values);
  }
}
