import { decodeHTML } from 'entities';

const sources = {
  github: {
    label: 'GitHub Trending（全部語言）',
    host: 'github.com',
  },
  trendshift: {
    label: 'Trendshift（熱門榜）',
    host: 'trendshift.io',
  },
} as const;

type Source = keyof typeof sources;
type Period = 'daily' | 'weekly' | 'monthly';
type TrendItem = {
  fullName: string;
  name: string;
  author: string;
  url: string;
  language: string | null;
  starsToday: number;
  starsTotal: number;
  description: string;
  category: string;
  source: string;
};
export type TrendingResult = {
  source: Source;
  label: string;
  since: Period;
  sourceUrl: string;
  fetchedAt: string;
  items: TrendItem[];
};

export class TrendingError extends Error {}

const cacheSeconds = 45_000;
const maxResponseBytes = 5 * 1024 * 1024;
const userAgent = 'ade-agent-environment/0.1 (read-only GitHub Trending view)';
const repoName = /^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/;
const trendingArticle = /<article class="Box-row"[\s\S]*?<\/article>/gi;
const trendingHref = /<h2\b[\s\S]*?<a\b[^>]*href="\/([^"]+)"/i;
const trendingDescription = /<p\b[^>]*class="col-9[^"]*"[^>]*>([\s\S]*?)<\/p>/i;
const trendingLanguage = /<span\b[^>]*itemprop="programmingLanguage"[^>]*>([\s\S]*?)<\/span>/i;
const trendingStars = /href="\/[^"]+\/stargazers"[^>]*>[\s\S]*?([\d,]+)\s*<\/a>/i;
const trendingGain = /([\d,]+)\s+stars?\s+(?:today|this week|this month)/i;
const rscPush = /self\.__next_f\.push\(\[1,"((?:[^"\\]|\\.)*)"\]\)/g;
const tagPattern = /<[^>]+>/g;
const cache = new Map<string, { expiresAt: number; value: TrendingResult }>();
const inFlight = new Map<string, Promise<TrendingResult>>();

function htmlUnescape(value: string): string {
  return decodeHTML(value);
}

function unquote(value: string): string {
  return value.replace(/(?:%[\da-f]{2})+/gi, (encoded) => {
    const bytes = encoded.match(/%[\da-f]{2}/gi) || [];
    return Buffer.from(bytes.map((byte) => Number.parseInt(byte.slice(1), 16))).toString('utf8');
  });
}

function cleanText(value: string): string {
  return htmlUnescape(value.replace(tagPattern, '')).replace(/\s+/g, ' ').trim();
}

function integer(value: unknown): number {
  if (typeof value === 'boolean' || value === null || value === undefined) return 0;
  if (typeof value === 'number') return Number.isFinite(value) ? Math.trunc(value) : 0;
  const compact = String(value).trim().toLowerCase().replaceAll(',', '');
  const multiplier = compact.endsWith('k') ? 1_000 : compact.endsWith('m') ? 1_000_000 : 1;
  const numeric = Number.parseFloat(multiplier === 1 ? compact : compact.slice(0, -1));
  if (!Number.isFinite(numeric)) return 0;
  const scaled = numeric * multiplier;
  const lower = Math.floor(scaled);
  const fraction = scaled - lower;
  if (fraction !== 0.5) return Math.round(scaled);
  return lower % 2 === 0 ? lower : lower + 1;
}

function candidate(
  fullNameValue: unknown,
  values: {
    language?: unknown;
    starsToday: number;
    starsTotal: number;
    description: string;
    category: string;
    source: string;
  },
): TrendItem | undefined {
  const fullName = htmlUnescape(String(fullNameValue ?? '')).trim().replace(/^\/+|\/+$/g, '');
  if (!repoName.test(fullName)) return undefined;
  const [author, name] = fullName.split('/', 2);
  return {
    fullName,
    name,
    author,
    url: `https://github.com/${fullName}`,
    language: typeof values.language === 'string' && values.language ? htmlUnescape(values.language).trim() : null,
    starsToday: values.starsToday,
    starsTotal: values.starsTotal,
    description: htmlUnescape(values.description).trim(),
    category: values.category,
    source: values.source,
  };
}

function parseGithub(document: string): TrendItem[] {
  const items: TrendItem[] = [];
  for (const articleMatch of document.matchAll(trendingArticle)) {
    const article = articleMatch[0];
    const href = trendingHref.exec(article)?.[1];
    if (!href) continue;
    const fullName = unquote(href.replace(/\/$/, ''));
    const description = trendingDescription.exec(article)?.[1];
    const language = trendingLanguage.exec(article)?.[1];
    const total = trendingStars.exec(article)?.[1];
    const gain = trendingGain.exec(article)?.[1];
    const item = candidate(fullName, {
      language: language === undefined ? undefined : cleanText(language),
      starsToday: gain ? integer(gain) : 0,
      starsTotal: total ? integer(total) : 0,
      description: description ? cleanText(description) : '',
      category: 'All Languages',
      source: 'trending',
    });
    if (item) items.push(item);
  }
  return items;
}

function jsonValueEnd(text: string, start: number): number | undefined {
  const opening = text[start];
  if (opening !== '[' && opening !== '{') return undefined;
  const stack: string[] = [];
  let inString = false;
  let escaped = false;
  for (let index = start; index < text.length; index += 1) {
    const character = text[index];
    if (inString) {
      if (escaped) escaped = false;
      else if (character === '\\') escaped = true;
      else if (character === '"') inString = false;
      continue;
    }
    if (character === '"') {
      inString = true;
    } else if (character === '[' || character === '{') {
      stack.push(character);
    } else if (character === ']' || character === '}') {
      const last = stack.pop();
      if ((character === ']' && last !== '[') || (character === '}' && last !== '{')) return undefined;
      if (!stack.length) return index + 1;
    }
  }
  return undefined;
}

function parseTrendshift(document: string): TrendItem[] | undefined {
  for (const match of document.matchAll(rscPush)) {
    let decoded: string;
    try {
      decoded = JSON.parse(`"${match[1]}"`) as string;
    } catch {
      continue;
    }
    const keyIndex = decoded.indexOf('"initialData":');
    if (keyIndex < 0) continue;
    const arrayStart = decoded.indexOf('[', keyIndex);
    const end = arrayStart < 0 ? undefined : jsonValueEnd(decoded, arrayStart);
    if (arrayStart < 0 || end === undefined) continue;
    let rows: unknown;
    try {
      rows = JSON.parse(decoded.slice(arrayStart, end)) as unknown;
    } catch {
      continue;
    }
    if (!Array.isArray(rows)) continue;
    const ranked: Array<{ rank: number; index: number; item: TrendItem }> = [];
    rows.forEach((row: unknown, index: number) => {
      if (!row || typeof row !== 'object' || Array.isArray(row)) return;
      const value = row as Record<string, unknown>;
      const item = candidate(value.full_name, {
        language: value.repository_language || value.language,
        starsToday: integer(value.repository_stars_gained),
        starsTotal: integer(value.repository_stars),
        description: String(value.repository_description || ''),
        category: 'Trendshift',
        source: 'trendshift',
      });
      if (!item) return;
      ranked.push({ rank: integer(value.rank) || index + 1, index, item });
    });
    ranked.sort((left, right) => left.rank - right.rank || left.index - right.index);
    return ranked.map(({ item }) => item);
  }
  return undefined;
}

function sourceUrl(source: Source, since: Period): string {
  if (source === 'github') return `https://github.com/trending?since=${since}`;
  return since === 'daily' ? 'https://trendshift.io/' : `https://trendshift.io/${since}`;
}

async function fetchResponse(url: string, host: string): Promise<Response> {
  let current = url;
  for (let redirectCount = 0; redirectCount <= 10; redirectCount += 1) {
    const response = await fetch(current, {
      redirect: 'manual',
      signal: AbortSignal.timeout(20_000),
      headers: {
        'User-Agent': userAgent,
        Accept: 'text/html,application/xhtml+xml',
        'Accept-Language': 'en-US,en;q=0.9',
      },
    });
    if (response.status < 300 || response.status >= 400) return response;
    const location = response.headers.get('location');
    if (!location) return response;
    const target = new URL(location, current);
    await response.body?.cancel();
    if (target.protocol !== 'https:' || target.hostname !== host) {
      throw new TrendingError('upstream attempted to redirect outside its source host');
    }
    current = target.toString();
  }
  throw new TrendingError('upstream exceeded the redirect limit');
}

async function readHtml(response: Response): Promise<string> {
  const reader = response.body?.getReader();
  if (!reader) return '';
  const chunks: Uint8Array[] = [];
  let total = 0;
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    total += value.byteLength;
    if (total > maxResponseBytes) {
      await reader.cancel();
      throw new TrendingError('upstream response exceeded the 5 MiB limit');
    }
    chunks.push(value);
  }
  const bytes = new Uint8Array(total);
  let offset = 0;
  for (const chunk of chunks) {
    bytes.set(chunk, offset);
    offset += chunk.byteLength;
  }
  const charset = response.headers.get('content-type')?.match(/charset\s*=\s*["']?([^;"'\s]+)/i)?.[1] || 'utf-8';
  try {
    return new TextDecoder(charset, { fatal: false }).decode(bytes);
  } catch {
    return new TextDecoder('utf-8', { fatal: false }).decode(bytes);
  }
}

async function fetchHtml(url: string, host: string): Promise<string> {
  let lastError = 'network request failed';
  for (let attempt = 0; attempt < 2; attempt += 1) {
    let response: Response;
    try {
      response = await fetchResponse(url, host);
    } catch (error) {
      if (error instanceof TrendingError && error.message.startsWith('upstream attempted')) throw error;
      lastError = error instanceof Error ? error.message : String(error);
      if (attempt === 0) await new Promise((resolvePromise) => setTimeout(resolvePromise, 500));
      continue;
    }
    if (!response.ok) {
      lastError = `HTTP ${response.status} ${response.statusText}`.trim();
      await response.body?.cancel();
      if (response.status !== 429 && response.status < 500) break;
      if (attempt === 0) await new Promise((resolvePromise) => setTimeout(resolvePromise, 500));
      continue;
    }
    try {
      return await readHtml(response);
    } catch (error) {
      if (error instanceof TrendingError) throw error;
      lastError = error instanceof Error ? error.message : String(error);
      if (attempt === 0) await new Promise((resolvePromise) => setTimeout(resolvePromise, 500));
    }
  }
  throw new TrendingError(`Could not fetch ${host}: ${lastError}`);
}

async function fetchTrending(source: Source, since: Period): Promise<TrendingResult> {
  const url = sourceUrl(source, since);
  const document = await fetchHtml(url, sources[source].host);
  const items = source === 'github' ? parseGithub(document) : parseTrendshift(document);
  if (source === 'github' && !items?.length) {
    throw new TrendingError('GitHub Trending page structure changed or returned no repositories');
  }
  if (source === 'trendshift' && items === undefined) {
    throw new TrendingError('Trendshift page structure changed: initialData was not found');
  }
  return {
    source,
    label: sources[source].label,
    since,
    sourceUrl: url,
    fetchedAt: new Date().toISOString().replace(/\.\d{3}Z$/, 'Z'),
    items: items || [],
  };
}

export function getTrending(sourceValue: string, sinceValue: string): Promise<TrendingResult> {
  if (!Object.hasOwn(sources, sourceValue)) return Promise.reject(new TrendingError('source must be github or trendshift'));
  if (!['daily', 'weekly', 'monthly'].includes(sinceValue)) {
    return Promise.reject(new TrendingError('since must be daily, weekly, or monthly'));
  }
  const source = sourceValue as Source;
  const since = sinceValue as Period;
  const key = `${source}:${since}`;
  const cached = cache.get(key);
  if (cached && cached.expiresAt > Date.now()) return Promise.resolve(structuredClone(cached.value));
  const pending = inFlight.get(key);
  if (pending) return pending.then((value) => structuredClone(value));
  const request = fetchTrending(source, since)
    .then((value) => {
      cache.set(key, { value, expiresAt: Date.now() + cacheSeconds });
      return structuredClone(value);
    })
    .finally(() => inFlight.delete(key));
  inFlight.set(key, request);
  return request;
}
