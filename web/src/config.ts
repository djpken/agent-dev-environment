import { readFileSync } from 'node:fs';
import { isAbsolute, resolve } from 'node:path';
import { normalizeArtifactConfiguration, type ArtifactConfiguration } from './artifacts.js';
import type { StatusComponentConfig } from './status.js';

export const ENVIRONMENT_CONFIG = Symbol('ENVIRONMENT_CONFIG');

export interface ComponentConfig extends StatusComponentConfig {
  id: string;
  health: Record<string, unknown>;
  public?: boolean;
  enabled?: boolean;
  update_command?: string[] | null;
  restart_command?: string[] | null;
  [key: string]: unknown;
}

export interface EnvironmentConfig {
  path: string;
  vm_id: string;
  source_root: string;
  state_root: string;
  listen: { host: string; port: number };
  public_origin: string;
  allow_http?: boolean;
  tls?: { cert?: string; key?: string };
  allowed_origins?: string[];
  authorization_trigger?: string;
  manual_trigger?: string;
  components: ComponentConfig[];
  artifacts?: ArtifactConfiguration;
  snapshot?: { enabled?: boolean };
}

export function parseArgs(argv: string[]): { configPath: string; host?: string; port?: number } {
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

export function loadConfig(configPath: string): EnvironmentConfig {
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
  raw.artifacts = normalizeArtifactConfiguration(raw.artifacts || {}, [raw.public_origin, ...raw.allowed_origins]);
  return raw;
}
