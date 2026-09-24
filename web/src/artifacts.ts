import { randomUUID } from 'node:crypto';
import { request as httpRequest } from 'node:http';
import { dirname, isAbsolute, join, resolve, sep } from 'node:path';
import { chmod, lstat, mkdir, readdir, rename, rm, stat, writeFile } from 'node:fs/promises';
import { withFileLock } from './file-lock.js';

export const MAX_UPLOAD_BYTES = 10 * 1024 * 1024;
export const MAX_UPLOAD_FILES = 200;
export const MAX_ARTIFACT_REQUEST_BYTES = 15 * 1024 * 1024;

const artifactNamePattern = /^[a-z][a-z0-9-]{0,63}$/;
const htmlSuffixes = new Set(['.html', '.htm']);
const defaultArtifactBaseUrl = 'http://172.16.240.41:80';

export interface ArtifactConfiguration {
  enabled: boolean;
  artifact_root?: string;
  base_url?: string;
}

export class ArtifactError extends Error {
  constructor(
    readonly statusCode: number,
    message: string,
  ) {
    super(message);
  }
}

function parseUrl(value: string, message: string): URL {
  try {
    return new URL(value);
  } catch {
    throw new Error(message);
  }
}

function validateBaseUrl(value: unknown): URL {
  if (typeof value !== 'string') throw new Error('publish URL must be an HTTP port 80 origin');
  const parsed = parseUrl(value, 'publish URL has an invalid port');
  if (
    parsed.protocol !== 'http:' || !parsed.hostname || parsed.username || parsed.password ||
    parsed.search || parsed.hash || !['', '/'].includes(parsed.pathname) ||
    !['', '80'].includes(parsed.port)
  ) {
    throw new Error('publish URL must be an HTTP port 80 origin');
  }
  return parsed;
}

function originKey(value: string): string {
  const parsed = parseUrl(value, 'management origin is invalid');
  const port = parsed.port || (parsed.protocol === 'https:' ? '443' : '80');
  return `${parsed.protocol}//${parsed.hostname.toLowerCase()}:${port}`;
}

export function normalizeArtifactConfiguration(value: unknown, managementOrigins: string[]): ArtifactConfiguration {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    throw new Error('artifacts must be an object');
  }
  const raw = value as Record<string, unknown>;
  if (raw.enabled !== undefined && typeof raw.enabled !== 'boolean') {
    throw new Error('artifacts.enabled must be a boolean');
  }
  if (raw.enabled !== true) return { enabled: false };
  const artifactRoot = raw.artifact_root === undefined ? '/var/lib/ade/web-artifacts' : raw.artifact_root;
  if (typeof artifactRoot !== 'string' || !isAbsolute(artifactRoot)) {
    throw new Error('artifacts.artifact_root must be absolute');
  }
  const baseUrlValue = raw.base_url === undefined ? defaultArtifactBaseUrl : raw.base_url;
  if (typeof baseUrlValue !== 'string') throw new Error('publish URL must be an HTTP port 80 origin');
  const baseUrl = baseUrlValue;
  const parsed = validateBaseUrl(baseUrl);
  if (managementOrigins.includes('*') || managementOrigins.some((origin) => originKey(origin) === originKey(parsed.toString()))) {
    throw new Error('artifacts must use a separate origin from the management service');
  }
  return { enabled: true, artifact_root: artifactRoot, base_url: baseUrl };
}

function validateName(name: unknown): asserts name is string {
  if (typeof name !== 'string' || !artifactNamePattern.test(name)) {
    throw new ArtifactError(400, 'invalid artifact name');
  }
}

function validateEntrypoint(value: unknown): string {
  if (typeof value !== 'string' || !value || value.startsWith('/') || value.includes('\\')) {
    throw new ArtifactError(400, 'invalid HTML entrypoint');
  }
  const parts = value.split('/');
  if (parts.some((part) => !part || part === '.' || part === '..' || part.startsWith('.'))) {
    throw new ArtifactError(400, 'invalid HTML entrypoint');
  }
  const suffix = parts.at(-1)?.match(/\.[^.]*$/)?.[0]?.toLowerCase() || '';
  if (!htmlSuffixes.has(suffix)) throw new ArtifactError(400, 'HTML entrypoint must end with .html or .htm');
  return parts.join('/');
}

function encodePathSegment(value: string): string {
  return encodeURIComponent(value).replace(/[!'()*]/g, (character) => `%${character.charCodeAt(0).toString(16).toUpperCase()}`);
}

function accessUrl(baseUrl: string, name: string, entrypoint: string): string {
  const parsed = validateBaseUrl(baseUrl);
  const host = parsed.hostname;
  return `http://${host}:80/artifacts/${encodePathSegment(name)}/${entrypoint.split('/').map(encodePathSegment).join('/')}`;
}

async function exists(path: string): Promise<boolean> {
  try {
    await lstat(path);
    return true;
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === 'ENOENT') return false;
    throw error;
  }
}

async function publisherReady(baseUrl: string): Promise<void> {
  const parsed = validateBaseUrl(baseUrl);
  await new Promise<void>((resolvePromise, rejectPromise) => {
    const request = httpRequest({
      hostname: '127.0.0.1',
      port: 80,
      path: '/healthz',
      method: 'GET',
      headers: { Host: parsed.hostname },
      timeout: 2000,
    }, (response) => {
      response.resume();
      if ((response.statusCode || 500) >= 200 && (response.statusCode || 500) < 400) {
        resolvePromise();
      } else {
        rejectPromise(new Error('publisher returned an unsuccessful health response'));
      }
    });
    request.once('timeout', () => request.destroy(new Error('publisher health request timed out')));
    request.once('error', rejectPromise);
    request.end();
  }).catch(() => {
    throw new ArtifactError(503, 'publish blocked: HTTP publisher unavailable at 127.0.0.1:80');
  });
}

async function prepareArtifactRoot(root: string): Promise<string> {
  await mkdir(root, { recursive: true, mode: 0o755 });
  await chmod(root, 0o755);
  const publicRoot = join(root, 'artifacts');
  if (await exists(publicRoot)) {
    const metadata = await lstat(publicRoot);
    if (metadata.isSymbolicLink()) throw new ArtifactError(400, 'artifact root may not contain symbolic links');
    if (!metadata.isDirectory()) throw new ArtifactError(400, 'artifact root artifacts path must be a directory');
  } else {
    await mkdir(publicRoot, { mode: 0o755 });
  }
  await chmod(publicRoot, 0o755);
  return publicRoot;
}

async function ensureArtifactRoot(root: string): Promise<void> {
  await mkdir(root, { recursive: true, mode: 0o755 });
  await chmod(root, 0o755);
}

async function walkArtifact(
  artifactPath: string,
  relative = '',
): Promise<{ pages: Array<{ entrypoint: string; access_url: string }>; fileCount: number; sizeBytes: number }> {
  const pages: Array<{ entrypoint: string; access_url: string }> = [];
  let fileCount = 0;
  let sizeBytes = 0;
  const entries = await readdir(artifactPath, { withFileTypes: true });
  entries.sort((left, right) => left.name < right.name ? -1 : left.name > right.name ? 1 : 0);
  for (const entry of entries) {
    if (entry.name.startsWith('.')) continue;
    const path = join(artifactPath, entry.name);
    const metadata = await lstat(path);
    if (metadata.isSymbolicLink()) continue;
    if (metadata.isDirectory()) {
      const nested = await walkArtifact(path, relative ? `${relative}/${entry.name}` : entry.name);
      pages.push(...nested.pages);
      fileCount += nested.fileCount;
      sizeBytes += nested.sizeBytes;
    } else if (metadata.isFile()) {
      fileCount += 1;
      sizeBytes += metadata.size;
      const entrypoint = relative ? `${relative}/${entry.name}` : entry.name;
      const suffix = entrypoint.match(/\.[^.]*$/)?.[0]?.toLowerCase() || '';
      if (htmlSuffixes.has(suffix)) {
        pages.push({ entrypoint, access_url: '' });
      }
    }
  }
  return { pages, fileCount, sizeBytes };
}

export async function listArtifacts(configuration: ArtifactConfiguration): Promise<Record<string, unknown>> {
  if (!configuration.enabled) return { enabled: false, artifacts: [] };
  const root = resolve(configuration.artifact_root as string);
  let publisherReadyResult = true;
  try {
    await publisherReady(configuration.base_url as string);
  } catch {
    publisherReadyResult = false;
  }
  try {
    if (!(await exists(root))) {
      return {
        enabled: true,
        publisher_ready: publisherReadyResult,
        max_upload_bytes: MAX_UPLOAD_BYTES,
        max_upload_files: MAX_UPLOAD_FILES,
        artifacts: [],
      };
    }
    await chmod(root, 0o755);
    const items = await withFileLock(join(root, '.ade-publish.lock'), 'shared', async () => {
      const publicRoot = join(root, 'artifacts');
      if (!(await exists(publicRoot))) return [];
      const publicRootMetadata = await lstat(publicRoot);
      if (publicRootMetadata.isSymbolicLink()) {
        throw new ArtifactError(503, 'artifact root may not contain symbolic links');
      }
      if (!publicRootMetadata.isDirectory()) {
        throw new ArtifactError(503, 'artifact root artifacts path must be a directory');
      }
      const directories = await readdir(publicRoot, { withFileTypes: true });
      directories.sort((left, right) => left.name < right.name ? -1 : left.name > right.name ? 1 : 0);
      const result: Array<Record<string, unknown>> = [];
      for (const entry of directories) {
        if (!artifactNamePattern.test(entry.name)) continue;
        const artifactPath = join(publicRoot, entry.name);
        const metadata = await lstat(artifactPath);
        if (metadata.isSymbolicLink() || !metadata.isDirectory()) continue;
        const inventory = await walkArtifact(artifactPath);
        inventory.pages.sort((left, right) => {
          if (left.entrypoint === 'index.html') return right.entrypoint === 'index.html' ? 0 : -1;
          if (right.entrypoint === 'index.html') return 1;
          return left.entrypoint < right.entrypoint ? -1 : left.entrypoint > right.entrypoint ? 1 : 0;
        });
        result.push({
          name: entry.name,
          pages: inventory.pages.map((page) => ({
            ...page,
            access_url: accessUrl(configuration.base_url as string, entry.name, page.entrypoint),
          })),
          file_count: inventory.fileCount,
          size_bytes: inventory.sizeBytes,
        });
      }
      return result;
    });
    return {
      enabled: true,
      publisher_ready: publisherReadyResult,
      max_upload_bytes: MAX_UPLOAD_BYTES,
      max_upload_files: MAX_UPLOAD_FILES,
      artifacts: items,
    };
  } catch (error) {
    if (error instanceof ArtifactError) throw new ArtifactError(503, error.message);
    throw new ArtifactError(503, 'web artifact storage is unavailable');
  }
}

function validateUploadPath(value: unknown): asserts value is string {
  if (
    typeof value !== 'string' || value.length < 1 || value.length > 1024 ||
    value.includes('\\') || value.includes('\0')
  ) {
    throw new ArtifactError(400, 'invalid upload path');
  }
  const parts = value.split('/');
  if (parts.some((part) => !part || part.startsWith('.'))) throw new ArtifactError(400, 'invalid upload path');
}

function decodeBase64(value: unknown): Buffer {
  if (
    typeof value !== 'string' || value.length > 4 * Math.ceil(MAX_UPLOAD_BYTES / 3) ||
    !/^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$/.test(value)
  ) {
    throw new ArtifactError(400, 'upload exceeds 10 MiB');
  }
  const result = Buffer.from(value, 'base64');
  if (result.toString('base64') !== value) throw new ArtifactError(400, 'invalid base64 file content');
  return result;
}

async function removePath(path: string): Promise<void> {
  const metadata = await lstat(path);
  if (metadata.isSymbolicLink() || !metadata.isDirectory()) {
    await rm(path);
  } else {
    await rm(path, { recursive: true });
  }
}

async function replaceDirectory(destination: string, stage: string): Promise<void> {
  let backup: string | undefined;
  if (await exists(destination)) {
    backup = join(resolve(destination, '..'), `.${destination.split(sep).at(-1)}.old-${randomUUID()}`);
    await rename(destination, backup);
  }
  try {
    await rename(stage, destination);
  } catch (error) {
    if (backup && !(await exists(destination))) await rename(backup, destination);
    throw error;
  }
  if (backup) await removePath(backup);
}

export async function uploadArtifact(
  configuration: ArtifactConfiguration,
  payload: Record<string, unknown>,
): Promise<Record<string, unknown>> {
  if (!configuration.enabled) throw new ArtifactError(400, 'web publishing is disabled');
  const name = payload.name;
  validateName(name);
  const entrypoint = validateEntrypoint(payload.entrypoint ?? 'index.html');
  const overwrite = payload.overwrite ?? false;
  if (typeof overwrite !== 'boolean') throw new ArtifactError(400, 'overwrite must be a boolean');
  const files = payload.files;
  if (!Array.isArray(files) || !files.length || files.length > MAX_UPLOAD_FILES) {
    throw new ArtifactError(400, `upload must contain 1 to ${MAX_UPLOAD_FILES} files`);
  }

  const decoded: Array<{ path: string; data: Buffer }> = [];
  const seen = new Set<string>();
  let total = 0;
  for (const item of files) {
    if (!item || typeof item !== 'object' || Array.isArray(item)) throw new ArtifactError(400, 'invalid upload file');
    const file = item as Record<string, unknown>;
    validateUploadPath(file.path);
    if (seen.has(file.path)) throw new ArtifactError(400, 'duplicate upload path');
    seen.add(file.path);
    const data = decodeBase64(file.content_base64);
    total += data.byteLength;
    if (total > MAX_UPLOAD_BYTES) throw new ArtifactError(400, 'upload exceeds 10 MiB');
    decoded.push({ path: file.path, data });
  }
  if (!seen.has(entrypoint)) throw new ArtifactError(400, 'HTML entrypoint does not exist');

  await publisherReady(configuration.base_url as string);
  const root = resolve(configuration.artifact_root as string);
  try {
    await ensureArtifactRoot(root);
    return await withFileLock(join(root, '.ade-publish.lock'), 'exclusive', async () => {
      const publicRoot = await prepareArtifactRoot(root);
      const destination = join(publicRoot, name);
      if (!overwrite && await exists(destination)) {
        throw new ArtifactError(400, 'artifact already exists; confirm replacement');
      }
      const stage = join(publicRoot, `.${name}.stage-${randomUUID()}`);
      await mkdir(stage, { mode: 0o755 });
      await chmod(stage, 0o755);
      let stageExists = true;
      try {
        for (const file of decoded) {
          const target = join(stage, ...file.path.split('/'));
          const parent = join(target, '..');
          try {
            await mkdir(parent, { recursive: true, mode: 0o755 });
            let directory = parent;
            while (directory === stage || directory.startsWith(`${stage}${sep}`)) {
              await chmod(directory, 0o755);
              if (directory === stage) break;
              directory = dirname(directory);
            }
            await writeFile(target, file.data, { flag: 'wx', mode: 0o644 });
            await chmod(target, 0o644);
          } catch {
            throw new ArtifactError(400, 'conflicting upload paths');
          }
        }
        const entrypointPath = join(stage, ...entrypoint.split('/'));
        if (!(await stat(entrypointPath)).isFile()) throw new ArtifactError(400, 'HTML entrypoint does not exist');
        await replaceDirectory(destination, stage);
        stageExists = false;
      } finally {
        if (stageExists) await rm(stage, { recursive: true, force: true });
      }
      return { name, entrypoint, access_url: accessUrl(configuration.base_url as string, name, entrypoint) };
    });
  } catch (error) {
    if (error instanceof ArtifactError) throw error;
    throw new ArtifactError(503, 'web artifact storage is unavailable');
  }
}

export async function deleteArtifact(
  configuration: ArtifactConfiguration,
  name: string,
): Promise<Record<string, unknown>> {
  if (!configuration.enabled) throw new ArtifactError(400, 'web publishing is disabled');
  validateName(name);
  const root = resolve(configuration.artifact_root as string);
  try {
    await ensureArtifactRoot(root);
    return await withFileLock(join(root, '.ade-publish.lock'), 'exclusive', async () => {
      const publicRoot = await prepareArtifactRoot(root);
      const destination = join(publicRoot, name);
      if (!(await exists(destination))) throw new ArtifactError(404, `artifact not found: ${name}`);
      await removePath(destination);
      return { name, deleted: true };
    });
  } catch (error) {
    if (error instanceof ArtifactError) throw error;
    throw new ArtifactError(503, 'web artifact storage is unavailable');
  }
}
