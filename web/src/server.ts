import Fastify, { type FastifyRequest } from 'fastify';
import fastifyStatic from '@fastify/static';
import { createHash, randomBytes, randomUUID } from 'node:crypto';
import { spawn } from 'node:child_process';
import { readFileSync } from 'node:fs';
import { dirname, isAbsolute, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const MAX_JSON_BYTES = 64 * 1024;
const MAX_ARTIFACT_BYTES = 15 * 1024 * 1024;
const CHILD_OUTPUT_LIMIT = 16 * 1024 * 1024;
const UV_BIN = process.env.ADE_UV_BIN || '/home/orca/.local/bin/uv';
const __dirname = dirname(fileURLToPath(import.meta.url));
const UI_ROOT = resolve(__dirname, 'ui');

const signatureBodySchema = {
  type: 'object',
  properties: {
    challenge_id: { type: 'string', minLength: 1, maxLength: 128 },
    address: { type: 'string', pattern: '^0x[0-9a-fA-F]{40}$' },
    message: { type: 'string', minLength: 1, maxLength: 4096 },
    signature: { type: 'string', minLength: 1, maxLength: 512 },
  },
  required: ['challenge_id', 'address', 'message', 'signature'],
  additionalProperties: false,
} as const;

const uploadBodySchema = {
  type: 'object',
  properties: {
    name: { type: 'string', minLength: 1, maxLength: 64, pattern: '^[a-z0-9][a-z0-9._-]*$' },
    entrypoint: { type: 'string', minLength: 1, maxLength: 1024 },
    overwrite: { type: 'boolean' },
    files: {
      type: 'array', minItems: 1, maxItems: 200,
      items: {
        type: 'object',
        properties: {
          path: { type: 'string', minLength: 1, maxLength: 1024 },
          content_base64: { type: 'string', maxLength: 13981016 },
        },
        required: ['path', 'content_base64'],
        additionalProperties: false,
      },
    },
  },
  required: ['name', 'entrypoint', 'files'],
  additionalProperties: false,
} as const;

const updateBodySchema = {
  type: 'object',
  properties: {
    action: { type: 'string', enum: ['update', 'restart'] },
    component_ids: { type: 'array', minItems: 1, uniqueItems: true, items: { type: 'string', minLength: 1 } },
    target_versions: { type: 'object', additionalProperties: { type: 'string' } },
  },
  required: ['component_ids'],
  additionalProperties: false,
} as const;

const policyBodySchema = {
  type: 'object',
  properties: {
    enabled: { type: 'boolean' },
    components: { type: 'array', minItems: 1, uniqueItems: true, items: { type: 'string', minLength: 1 } },
    target_versions: { type: 'object', additionalProperties: { type: 'string' } },
    allow_restart: { type: 'boolean' },
    release_channel: { type: 'string', enum: ['stable'] },
    expires_at: { type: ['string', 'null'] },
  },
  required: ['enabled'],
  additionalProperties: false,
} as const;

type WalletRole = 'viewer' | 'operator' | 'admin';

interface ComponentConfig {
  id: string;
  public?: boolean;
  enabled?: boolean;
  update_command?: string[] | null;
  restart_command?: string[] | null;
  [key: string]: unknown;
}

interface EnvironmentConfig {
  path: string;
  vm_id: string;
  source_root: string;
  state_root: string;
  listen: { host: string; port: number };
  public_origin: string;
  allow_http?: boolean;
  tls?: { cert?: string; key?: string };
  allowed_origins?: string[];
  wallets?: { authorized?: Array<{ address: string; role: WalletRole }> };
  authorization_trigger?: string;
  registration_trigger?: string;
  manual_trigger?: string;
  components: ComponentConfig[];
  artifacts?: { enabled?: boolean };
}

interface CommandResult {
  stdout: string;
  stderr: string;
}

class ProcessFailure extends Error {
  constructor(
    readonly exitCode: number | null,
    readonly stdout: string,
    readonly stderr: string,
  ) {
    super('child process exited with code ' + String(exitCode));
  }
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

class ApiError extends Error {
  constructor(
    readonly statusCode: number,
    message: string,
  ) {
    super(message);
  }
}

function parseArgs(argv: string[]): { configPath: string; host?: string; port?: number } {
  const parsed: { configPath: string; host?: string; port?: number } = {
    configPath: '/etc/ade/agent-environment.json',
  };
  for (let index = 0; index < argv.length; index += 1) {
    const value = argv[index];
    if (value === '--config' && argv[index + 1]) {
      parsed.configPath = resolve(argv[++index]);
    } else if (value === '--host' && argv[index + 1]) {
      parsed.host = argv[++index];
    } else if (value === '--port' && argv[index + 1]) {
      const port = Number(argv[++index]);
      if (!Number.isInteger(port) || port < 1 || port > 65535) {
        throw new Error('--port must be between 1 and 65535');
      }
      parsed.port = port;
    } else {
      throw new Error('unsupported command argument: ' + value);
    }
  }
  return parsed;
}

function loadConfig(configPath: string): EnvironmentConfig {
  const raw = JSON.parse(readFileSync(configPath, 'utf8')) as EnvironmentConfig;
  if (!raw || typeof raw !== 'object' || typeof raw.vm_id !== 'string') {
    throw new Error('environment config must be a JSON object with vm_id');
  }
  if (!isAbsolute(raw.source_root) || !isAbsolute(raw.state_root)) {
    throw new Error('source_root and state_root must be absolute paths');
  }
  if (!raw.listen || typeof raw.listen.host !== 'string' || !Number.isInteger(raw.listen.port)) {
    throw new Error('listen.host and listen.port are required');
  }
  if (!/^https?:\/\//.test(raw.public_origin)) {
    throw new Error('public_origin must be an HTTP(S) origin');
  }
  if (!Array.isArray(raw.components) || !raw.components.length) {
    throw new Error('components must be a non-empty list');
  }
  raw.path = configPath;
  raw.public_origin = raw.public_origin.replace(/\/+$/, '');
  raw.allowed_origins = raw.allowed_origins || [];
  return raw;
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
        rejectPromise(new ProcessFailure(code, result.stdout, result.stderr));
      } else {
        resolvePromise(result);
      }
    });
    if (input === undefined) {
      child.stdin.end();
    } else {
      child.stdin.end(input, 'utf8');
    }
  });
}

function parseJsonOutput<T>(output: string, label: string): T {
  try {
    return JSON.parse(output) as T;
  } catch {
    throw new ApiError(502, label + ' returned invalid JSON');
  }
}

let config: EnvironmentConfig;
let configPath = '/etc/ade/agent-environment.json';
let requestedHost: string | undefined;
let requestedPort: number | undefined;
let statusCache: CachedValue<Promise<Record<string, unknown>>> | undefined;
const registrationChallenges = new Map<string, RegistrationChallenge>();
const authenticationFailures = new Map<string, number[]>();
const trendingCache = new Map<string, CachedValue<Record<string, unknown>>>();
const trendingRequests = new Map<string, Promise<Record<string, unknown>>>();

function sourceRoot(): string {
  return config.source_root;
}

function uvArgs(args: string[]): string[] {
  return [
    'run',
    '--frozen',
    '--project',
    sourceRoot(),
    'python',
    '-m',
    'ade.cli',
    'environment',
    ...args,
    '--config',
    config.path,
  ];
}

async function runAdeJson<T>(args: string[], input?: string, timeoutMs = 120000): Promise<T> {
  let result: CommandResult;
  try {
    result = await runProcess(UV_BIN, uvArgs(args), input, timeoutMs);
  } catch (error) {
    if (args[0] === 'artifacts' && error instanceof ProcessFailure) {
      try {
        const failure = JSON.parse(error.stdout) as Record<string, unknown>;
        if (typeof failure.error === 'string') {
          const statusCode = typeof failure.status_code === 'number' && failure.status_code >= 400 && failure.status_code <= 599
            ? failure.status_code
            : 502;
          throw new ApiError(statusCode, failure.error);
        }
      } catch (parseError) {
        if (parseError instanceof ApiError) throw parseError;
      }
    }
    throw new ApiError(502, 'ADE operation could not be completed');
  }
  return parseJsonOutput<T>(result.stdout, 'ADE operation');
}

async function getStatus(): Promise<Record<string, unknown>> {
  if (statusCache && statusCache.expiresAt > Date.now()) {
    return statusCache.value;
  }
  const pending = runAdeJson<Record<string, unknown>>(['status']);
  statusCache = { expiresAt: Date.now() + 1000, value: pending };
  try {
    const result = await pending;
    statusCache = { expiresAt: Date.now() + 1000, value: Promise.resolve(result) };
    return result;
  } catch (error) {
    statusCache = undefined;
    throw error;
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value);
}

function readBearer(request: FastifyRequest): string {
  const authorization = request.headers.authorization;
  if (typeof authorization !== 'string' || !authorization.startsWith('Bearer ')) {
    throw new ApiError(401, 'wallet session required');
  }
  return authorization.slice('Bearer '.length).trim();
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

async function authorization<T>(operation: string, payload: Record<string, unknown> = {}): Promise<T> {
  const helper = config.authorization_trigger || '/usr/local/sbin/agent-environment-authorize';
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

async function requireSession(request: FastifyRequest, minimumRole: WalletRole = 'viewer') {
  return authorization<Record<string, unknown>>('session', {
    token: readBearer(request),
    minimum_role: minimumRole,
  });
}

function originAllowed(origin: string | undefined): boolean {
  if (!origin) return true;
  return origin === config.public_origin || (config.allowed_origins || []).includes(origin) ||
    (config.allowed_origins || []).includes('*');
}

function cleanChallenges(): void {
  const now = Date.now();
  for (const [id, challenge] of registrationChallenges) {
    if (challenge.expiresAt <= now) registrationChallenges.delete(id);
  }
  while (registrationChallenges.size > 32) {
    const oldest = registrationChallenges.keys().next().value;
    if (oldest) registrationChallenges.delete(oldest);
    else break;
  }
}

function isoNow(): string {
  return new Date().toISOString().replace(/\.\d{3}Z$/, 'Z');
}

function createRegistrationMessage(address: string, issuedAt: string, expiresAt: string, nonce: string): string {
  const vmBinding = createHash('sha256').update(config.vm_id, 'utf8').digest('hex');
  const payload = {
    action: 'register',
    address,
    expires_at: expiresAt,
    issued_at: issuedAt,
    nonce,
    role: 'admin',
    vm_binding: vmBinding,
  };
  return 'ADE-ENVIRONMENT-REGISTRATION-V1\n' + JSON.stringify(payload);
}

function normalizeAddress(value: unknown): string {
  if (typeof value !== 'string' || !/^0x[0-9a-fA-F]{40}$/.test(value)) {
    throw new ApiError(400, 'wallet address must be a 0x-prefixed Ethereum address');
  }
  return value.toLowerCase();
}

function queryValue(request: FastifyRequest, key: string): string {
  const url = new URL(request.raw.url || '/', 'http://localhost');
  const values = url.searchParams.getAll(key);
  if (values.length !== 1) throw new ApiError(400, key + ' must be specified once');
  return values[0];
}

function authFailureAllowed(address: string): boolean {
  const cutoff = Date.now() - 5 * 60 * 1000;
  const recent = (authenticationFailures.get(address) || []).filter((time) => time > cutoff);
  authenticationFailures.set(address, recent);
  return recent.length < 10;
}

function noteAuthFailure(address: string): void {
  const values = authenticationFailures.get(address) || [];
  values.push(Date.now());
  authenticationFailures.set(address, values);
}

function publicRun(run: unknown): Record<string, unknown> | undefined {
  if (!isRecord(run)) return undefined;
  const publicIds = new Set(config.components.filter((component) => component.public).map((component) => component.id));
  const fields = [
    'id', 'trigger', 'action', 'status', 'created_at', 'started_at', 'finished_at',
    'error', 'publication', 'target_versions',
  ];
  const result: Record<string, unknown> = {};
  for (const field of fields) {
    if (field in run) result[field] = run[field];
  }
  const componentIds = Array.isArray(run.component_ids) ? run.component_ids.filter((id) => publicIds.has(String(id))) : [];
  result.component_ids = componentIds;
  result.components = Array.isArray(run.components)
    ? run.components.filter((item) => isRecord(item) && publicIds.has(String(item.id))).map((item) => {
        const selected: Record<string, unknown> = {};
        for (const field of ['id', 'status', 'action', 'returncode', 'version', 'health', 'rollback', 'detail']) {
          if (field in item) selected[field] = item[field];
        }
        return selected;
      })
    : [];
  return result;
}

async function trending(source: string, since: string): Promise<Record<string, unknown>> {
  const key = source + ':' + since;
  const cached = trendingCache.get(key);
  if (cached && cached.expiresAt > Date.now()) return cached.value;
  const pending = trendingRequests.get(key);
  if (pending) return pending;
  const request = runAdeJson<Record<string, unknown>>(['trending', '--source', source, '--since', since], undefined, 45000)
    .then((value) => {
      trendingCache.set(key, { value, expiresAt: Date.now() + 45000 });
      return value;
    })
    .catch((error) => {
      throw error instanceof ApiError ? error : new ApiError(502, 'trending source is unavailable');
    })
    .finally(() => {
      trendingRequests.delete(key);
    });
  trendingRequests.set(key, request);
  return request;
}

async function startServer(): Promise<void> {
  const parsed = parseArgs(process.argv.slice(2));
  configPath = parsed.configPath;
  config = loadConfig(configPath);
  requestedHost = parsed.host;
  requestedPort = parsed.port;
  const host = requestedHost || config.listen.host;
  const port = requestedPort || config.listen.port;
  const tls = config.tls || {};
  if (Boolean(tls.cert) !== Boolean(tls.key)) {
    throw new Error('tls.cert and tls.key must be supplied together');
  }
  if (!tls.cert && !config.allow_http && !['127.0.0.1', '::1', 'localhost'].includes(host)) {
    throw new Error('TLS is required when the environment service is not loopback-only');
  }
  const options = {
    logger: { level: process.env.LOG_LEVEL || 'info' },
    bodyLimit: MAX_JSON_BYTES,
    trustProxy: ['127.0.0.1', '::1', '::ffff:127.0.0.1'],
    ...(tls.cert && tls.key ? {
      https: {
        cert: readFileSync(tls.cert),
        key: readFileSync(tls.key),
        minVersion: 'TLSv1.2' as const,
      },
    } : {}),
  };
  const app = Fastify(options);

  app.addHook('onRequest', async (request, reply) => {
    const origin = request.headers.origin;
    if (typeof origin === 'string' && !originAllowed(origin)) {
      return reply.code(403).send({ error: 'origin is not allowed' });
    }
  });
  app.addHook('onSend', async (request, reply, payload) => {
    reply.header('X-Content-Type-Options', 'nosniff');
    reply.header('X-Frame-Options', 'DENY');
    reply.header('Referrer-Policy', 'no-referrer');
    reply.header(
      'Content-Security-Policy',
      "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'none'",
    );
    if (request.url.startsWith('/assets/')) {
      reply.header('Cache-Control', 'public, max-age=31536000, immutable');
    } else {
      reply.header('Cache-Control', 'no-store');
    }
    const origin = request.headers.origin;
    if (typeof origin === 'string' && originAllowed(origin)) {
      reply.header('Access-Control-Allow-Origin', origin);
      reply.header('Vary', 'Origin');
    }
    return payload;
  });
  app.setErrorHandler((error, _request, reply) => {
    if (error instanceof ApiError) {
      return reply.code(error.statusCode).send({ error: error.message });
    }
    const status = isRecord(error) && typeof error.statusCode === 'number' && error.statusCode < 500
      ? error.statusCode
      : 500;
    const message = status === 413 ? 'request body is too large' : status === 400 ? 'request body is invalid' : 'internal server error';
    if (status >= 500) app.log.error({ err: error }, 'request failed');
    return reply.code(status).send({ error: message });
  });
  app.setNotFoundHandler(async (request, reply) => {
    if (request.method === 'GET' && !request.url.startsWith('/api/')) {
      return reply.type('text/html; charset=utf-8').sendFile('index.html');
    }
    return reply.code(404).send({ error: 'route not found' });
  });

  await app.register(fastifyStatic, {
    root: UI_ROOT,
    prefix: '/',
    wildcard: false,
    serveDotFiles: false,
  });

  app.options('/*', async (_request, reply) => {
    reply.header('Access-Control-Allow-Methods', 'GET,POST,PUT,DELETE,OPTIONS');
    reply.header('Access-Control-Allow-Headers', 'Content-Type,Authorization');
    reply.header('Access-Control-Max-Age', '600');
    return reply.code(204).send();
  });

  app.get('/healthz', async (request) => {
    await requireSession(request);
    return {
      status: 'ok',
      service: 'agent-environment',
      vm_id: config.vm_id,
      registration_required: !(config.wallets?.authorized || []).length,
    };
  });

  app.get('/api/v1/trending', async (request) => {
    const url = new URL(request.raw.url || '/', 'http://localhost');
    const allowed = new Set(['source', 'since']);
    for (const key of url.searchParams.keys()) {
      if (!allowed.has(key) || url.searchParams.getAll(key).length !== 1) {
        throw new ApiError(400, 'only one source and one since value are allowed');
      }
    }
    const source = url.searchParams.get('source') || 'github';
    const since = url.searchParams.get('since') || 'daily';
    if (!['github', 'trendshift'].includes(source) || !['daily', 'weekly', 'monthly'].includes(since)) {
      throw new ApiError(400, 'source or since is unsupported');
    }
    return trending(source, since);
  });

  app.get('/api/v1/registration/challenge', async (request) => {
    if ((config.wallets?.authorized || []).length) {
      throw new ApiError(409, 'registration is already complete');
    }
    const address = normalizeAddress(queryValue(request, 'address'));
    cleanChallenges();
    const issuedAt = isoNow();
    const expiresAt = new Date(Date.now() + 10 * 60 * 1000).toISOString().replace(/\.\d{3}Z$/, 'Z');
    const challengeId = randomUUID();
    const nonce = randomBytes(24).toString('base64url');
    const message = createRegistrationMessage(address, issuedAt, expiresAt, nonce);
    registrationChallenges.set(challengeId, {
      address,
      message,
      expiresAt: Date.parse(expiresAt),
    });
    cleanChallenges();
    return {
      challenge_id: challengeId,
      address,
      role: 'admin',
      message,
      issued_at: issuedAt,
      expires_at: expiresAt,
    };
  });

  app.post<{ Body: Record<string, unknown> }>('/api/v1/registration', {
    bodyLimit: MAX_JSON_BYTES,
    schema: { body: signatureBodySchema },
  }, async (request) => {
    const body = request.body;
    const address = normalizeAddress(body.address);
    const challengeId = body.challenge_id;
    const message = body.message;
    const signature = body.signature;
    if (![challengeId, message, signature].every((value) => typeof value === 'string')) {
      throw new ApiError(400, 'address, challenge_id, message, and signature are required');
    }
    cleanChallenges();
    const challenge = registrationChallenges.get(challengeId as string);
    if (!challenge || challenge.address !== address || challenge.message !== message) {
      throw new ApiError(400, 'registration challenge is invalid');
    }
    const helper = config.registration_trigger || '/usr/local/sbin/agent-environment-enroll';
    let result: CommandResult;
    try {
      result = await runProcess(
        '/usr/bin/sudo',
        ['-n', helper],
        JSON.stringify({ address, message, signature }),
        65000,
        1024 * 1024,
      );
    } catch {
      throw new ApiError(400, 'wallet registration failed');
    }
    parseJsonOutput<Record<string, unknown>>(result.stdout, 'wallet registration helper');
    registrationChallenges.delete(challengeId as string);
    config = loadConfig(configPath);
    return { registered: true, address, role: 'admin', login_required: true };
  });

  app.get('/api/v1/auth/challenge', async (request) => {
    const address = normalizeAddress(queryValue(request, 'address'));
    const url = new URL(request.raw.url || '/', 'http://localhost');
    const origins = url.searchParams.getAll('origin');
    if (origins.length > 1) throw new ApiError(400, 'origin must be specified once');
    return authorization('challenge', {
      address,
      ...(origins.length ? { origin: origins[0] } : {}),
    });
  });

  app.post<{ Body: Record<string, unknown> }>('/api/v1/auth/verify', {
    schema: { body: signatureBodySchema },
  }, async (request) => {
    const address = normalizeAddress(request.body.address);
    const key = request.ip || 'unknown';
    if (!authFailureAllowed(key)) throw new ApiError(429, 'too many authentication failures');
    try {
      return await authorization('login', {
        challenge_id: request.body.challenge_id,
        address,
        message: request.body.message,
        signature: request.body.signature,
      });
    } catch (error) {
      noteAuthFailure(key);
      throw error;
    }
  });

  app.post('/api/v1/auth/logout', async (request) => {
    return authorization('logout', { token: readBearer(request) });
  });
  app.get('/api/v1/auth/session', async (request) => requireSession(request));

  app.get('/api/v1/registration/status', async (request) => {
    await requireSession(request);
    const authorized = config.wallets?.authorized || [];
    return {
      required: !authorized.length,
      vm_id: config.vm_id,
      role: authorized.length ? null : 'admin',
    };
  });

  app.get('/api/v1/health', async (request) => {
    await requireSession(request);
    const status = await getStatus();
    return status.health;
  });
  app.get('/api/v1/components', async (request) => {
    await requireSession(request);
    const status = await getStatus();
    const health = isRecord(status.health) ? status.health : {};
    return { vm_id: config.vm_id, components: health.components || [] };
  });
  app.get('/api/v1/runs', async (request) => {
    await requireSession(request);
    const status = await getStatus();
    const runs = Array.isArray(status.runs) ? status.runs.map(publicRun).filter(Boolean) : [];
    return { runs };
  });
  app.get('/api/v1/schedule', async (request) => {
    await requireSession(request);
    const status = await getStatus();
    const result = isRecord(status.config) ? { ...status.config } : {};
    const policy = isRecord(status.policy) ? status.policy : {};
    result.policy_valid = Boolean(policy.valid);
    result.policy_reason = policy.reason || 'daily updates are not enabled';
    return result;
  });
  app.get('/api/v1/policy', async (request) => {
    await requireSession(request);
    const status = await getStatus();
    return status.policy;
  });

  app.get('/api/v1/artifacts', async (request) => {
    await requireSession(request);
    return runAdeJson(['artifacts', 'list']);
  });
  app.post<{ Body: Record<string, unknown> }>('/api/v1/artifacts', {
    bodyLimit: MAX_ARTIFACT_BYTES,
    schema: { body: uploadBodySchema },
  }, async (request, reply) => {
    await requireSession(request, 'operator');
    const payload = JSON.stringify(request.body);
    if (Buffer.byteLength(payload, 'utf8') > MAX_ARTIFACT_BYTES) {
      throw new ApiError(413, 'request body is too large');
    }
    const receipt = await runAdeJson<Record<string, unknown>>(['artifacts', 'upload'], payload, 120000);
    return reply.code(201).send(receipt);
  });
  app.delete<{ Params: { name: string } }>('/api/v1/artifacts/:name', async (request) => {
    await requireSession(request, 'operator');
    return runAdeJson(['artifacts', 'delete', '--name', request.params.name]);
  });

  app.post<{ Body: Record<string, unknown> }>('/api/v1/update-runs', {
    schema: { body: updateBodySchema },
  }, async (request, reply) => {
    const body = request.body;
    const run = await authorization<Record<string, unknown>>('authorize_run', {
      token: readBearer(request),
      action: body.action || 'update',
      component_ids: body.component_ids,
      target_versions: body.target_versions || {},
    });
    const runId = run.id;
    if (typeof runId !== 'string') throw new ApiError(502, 'wallet authorization helper returned an invalid run');
    const trigger = config.manual_trigger || '/usr/local/sbin/agent-environment-trigger';
    const child = spawn('/usr/bin/sudo', ['-n', trigger, runId], {
      detached: true,
      stdio: 'ignore',
      windowsHide: true,
    });
    try {
      await new Promise<void>((resolvePromise, rejectPromise) => {
        child.once('spawn', () => resolvePromise());
        child.once('error', (error) => rejectPromise(error));
      });
      child.unref();
    } catch {
      await runAdeJson(['fail-run', '--run-id', runId, '--error', 'privileged updater trigger could not start']);
      throw new ApiError(400, 'update run was queued but could not start');
    }
    return reply.code(202).send(run);
  });

  app.put<{ Body: Record<string, unknown> }>('/api/v1/policy', {
    schema: { body: policyBodySchema },
  }, async (request) => {
    await requireSession(request, 'admin');
    const body = request.body;
    const fields = ['enabled', 'components', 'target_versions', 'allow_restart', 'release_channel', 'expires_at'];
    const payload: Record<string, unknown> = { token: readBearer(request) };
    for (const field of fields) {
      if (field in body) payload[field] = body[field];
    }
    return authorization('save_policy', payload);
  });

  await app.listen({ host, port });
  app.log.info({ host, port, tls: Boolean(tls.cert) }, 'ADES Fastify server listening');
}

startServer().catch((error: unknown) => {
  const message = error instanceof Error ? error.message : 'unknown startup error';
  process.stderr.write('agent-environment Fastify startup failed: ' + message + '\n');
  process.exitCode = 1;
});
