import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { createRoot } from 'react-dom/client';
import {
  Badge,
  Button,
  Card,
  Dropdown,
  Field,
  FluentProvider,
  Input,
  MessageBar,
  MessageBarBody,
  Option,
  createDarkTheme,
  type BadgeProps,
  type BrandVariants,
} from '@fluentui/react-components';
import './styles.css';

type Role = 'viewer' | 'operator' | 'admin';
type WalletProvider = {
  isMetaMask?: boolean;
  isRabby?: boolean;
  providers?: WalletProvider[];
  request(args: { method: string; params?: unknown[] }): Promise<unknown>;
  on?(event: string, listener: (...args: unknown[]) => void): void;
  removeListener?(event: string, listener: (...args: unknown[]) => void): void;
};

declare global {
  interface Window { ethereum?: WalletProvider; }
}

interface WalletSession { session: string; address: string; role: Role; expires_at: string; }
interface ComponentStatus {
  id: string; label: string; kind: string; status: string;
  version?: string | null; detail?: string;
  update_supported?: boolean; restart_supported?: boolean;
}
interface Health { status: string; vm_id: string; checked_at: string; components: ComponentStatus[]; }
interface Schedule {
  schedule: { time: string; timezone: string; persistent: boolean };
  policy_valid: boolean; policy_reason: string;
}
interface RunRecord {
  id: string; trigger: string; action: string; status: string; created_at: string;
  finished_at?: string; error?: string;
  component_ids?: string[];
  components?: Array<{ id: string; status: string; detail?: string }>;
}
interface ArtifactPage { entrypoint: string; access_url: string; }
interface Artifact { name: string; file_count: number; size_bytes: number; pages: ArtifactPage[]; }
interface ArtifactList {
  enabled: boolean; publisher_ready?: boolean; artifacts: Artifact[]; error?: string;
}
interface TrendingItem {
  fullName: string; url: string; language?: string | null;
  starsToday: number; starsTotal: number; description?: string;
}
interface TrendingData {
  label: string; since: string; sourceUrl: string; fetchedAt: string; items: TrendingItem[];
}

class ApiError extends Error {
  constructor(message: string, readonly status: number) { super(message); }
}

const SESSION_KEY = 'ade.environment.session.v1';
const MAX_UPLOAD_FILES = 200;
const MAX_UPLOAD_BYTES = 10 * 1024 * 1024;

const adeBrand: BrandVariants = {
  10: '#071e11', 20: '#0a301a', 30: '#104624', 40: '#175b30',
  50: '#206f3d', 60: '#2a824b', 70: '#37945b', 80: '#49a56c',
  90: '#5db57f', 100: '#74c592', 110: '#8bd3a4', 120: '#a3e0b6',
  130: '#bbebc8', 140: '#d0f4d9', 150: '#e1f8e7', 160: '#effbf2',
};
const adeTheme = createDarkTheme(adeBrand);

async function readResponse<T>(response: Response): Promise<T> {
  let body: Record<string, unknown> = {};
  try { body = await response.json() as Record<string, unknown>; } catch { /* Preserve an HTTP status for non-JSON responses. */ }
  if (!response.ok) {
    throw new ApiError(typeof body.error === 'string' ? body.error : `HTTP ${response.status}`, response.status);
  }
  return body as T;
}

function getWalletProvider(): WalletProvider | undefined {
  const ethereum = window.ethereum;
  if (!ethereum) return undefined;
  return ethereum.providers?.find((provider) => provider.isMetaMask && !provider.isRabby) ??
    (ethereum.isMetaMask && !ethereum.isRabby ? ethereum : undefined);
}

function encodeMessage(message: string): string {
  const bytes = new TextEncoder().encode(message);
  let encoded = '';
  for (const byte of bytes) encoded += byte.toString(16).padStart(2, '0');
  return `0x${encoded}`;
}

function formatDate(value?: string): string {
  if (!value) return '尚無時間資料';
  const date = new Date(value);
  return Number.isNaN(date.valueOf()) ? value : date.toLocaleString('zh-TW', { hour12: false });
}

function actionLabel(action: string): string {
  if (action === 'update') return '更新';
  if (action === 'restart') return '重啟';
  return action;
}

function statusLabel(status: string): string {
  const labels: Record<string, string> = {
    healthy: '正常', degraded: '降級', unhealthy: '異常', unknown: '未知',
    queued: '排隊中', running: '執行中', completed: '完成', updating: '更新中',
    partial_failure: '部分失敗', failed: '失敗', blocked: '已阻擋', skipped: '略過',
  };
  return labels[status] || status;
}

function statusColor(status: string): NonNullable<BadgeProps['color']> {
  if (['healthy', 'completed'].includes(status)) return 'success';
  if (['degraded', 'unknown', 'queued', 'running'].includes(status)) return 'warning';
  if (['unhealthy', 'failed', 'blocked', 'partial_failure'].includes(status)) return 'danger';
  if (status === 'updating') return 'informative';
  return 'subtle';
}

function StatusBadge({ status }: { status: string }) {
  return <Badge appearance="tint" color={statusColor(status)} shape="square" size="small">{statusLabel(status)}</Badge>;
}

function triggerLabel(trigger: string): string {
  if (trigger === 'manual') return '手動';
  if (trigger === 'scheduled') return '排程';
  return trigger;
}

function safeExternalUrl(value: string): boolean {
  try { return ['https:', 'http:'].includes(new URL(value).protocol); } catch { return false; }
}

function App() {
  const [session, setSession] = useState<WalletSession | null>(null);
  const sessionRef = useRef<WalletSession | null>(null);
  sessionRef.current = session;
  const [registrationRequired, setRegistrationRequired] = useState<boolean | null>(null);
  const [connecting, setConnecting] = useState(false);
  const [authMessage, setAuthMessage] = useState('');
  const [globalError, setGlobalError] = useState('');
  const [health, setHealth] = useState<Health | null>(null);
  const [schedule, setSchedule] = useState<Schedule | null>(null);
  const [runs, setRuns] = useState<RunRecord[]>([]);
  const [controlSubmitting, setControlSubmitting] = useState(false);
  const [controlMessage, setControlMessage] = useState('');
  const [artifacts, setArtifacts] = useState<ArtifactList>({ enabled: false, artifacts: [] });
  const [trending, setTrending] = useState<TrendingData | null>(null);
  const [trendSource, setTrendSource] = useState<'github' | 'trendshift'>('github');
  const [trendSince, setTrendSince] = useState<'daily' | 'weekly' | 'monthly'>('daily');
  const [trendLoading, setTrendLoading] = useState(false);
  const [trendError, setTrendError] = useState('');
  const [refreshing, setRefreshing] = useState(false);
  const [selectedFiles, setSelectedFiles] = useState<File[]>([]);
  const [artifactName, setArtifactName] = useState('');
  const [entrypoint, setEntrypoint] = useState('');
  const [publishing, setPublishing] = useState(false);
  const [artifactMessage, setArtifactMessage] = useState('');
  const [showPrivate, setShowPrivate] = useState(false);
  const fileInput = useRef<HTMLInputElement>(null);
  const folderInput = useRef<HTMLInputElement>(null);
  const providerRef = useRef<WalletProvider | undefined>(undefined);
  const accountRef = useRef<string | null>(null);
  const operationRef = useRef(0);
  const trendRequestRef = useRef(0);
  const controlRunIdRef = useRef<string | null>(null);

  const api = useCallback(async <T,>(path: string, init: RequestInit = {}, token?: string): Promise<T> => {
    const authToken = token ?? sessionRef.current?.session;
    const headers = new Headers(init.headers);
    if (init.body && !headers.has('Content-Type')) headers.set('Content-Type', 'application/json');
    if (authToken) headers.set('Authorization', `Bearer ${authToken}`);
    const response = await fetch(path, { ...init, headers, credentials: 'same-origin' });
    return readResponse<T>(response);
  }, []);

  const clearSession = useCallback((message = '') => {
    operationRef.current += 1;
    sessionRef.current = null;
    accountRef.current = null;
    setSession(null);
    setConnecting(false);
    setHealth(null);
    setSchedule(null);
    setRuns([]);
    setControlMessage('');
    controlRunIdRef.current = null;
    setArtifacts({ enabled: false, artifacts: [] });
    setSelectedFiles([]);
    setArtifactName('');
    setEntrypoint('');
    setShowPrivate(false);
    setGlobalError('');
    setAuthMessage(message);
    try { sessionStorage.removeItem(SESSION_KEY); } catch { /* Memory-only session remains usable. */ }
    if (fileInput.current) fileInput.current.value = '';
    if (folderInput.current) folderInput.current.value = '';
  }, []);

  const acceptSession = useCallback((next: WalletSession) => {
    sessionRef.current = next;
    setSession(next);
    setShowPrivate(true);
    setAuthMessage('');
    setGlobalError('');
    try { sessionStorage.setItem(SESSION_KEY, JSON.stringify(next)); } catch { /* Memory-only session remains usable. */ }
  }, []);

  const loadDashboard = useCallback(async (token: string) => {
    setRefreshing(true);
    setGlobalError('');
    try {
      const [healthResult, scheduleResult, runsResult, artifactResult] = await Promise.all([
        api<Health>('/api/v1/health', {}, token),
        api<Schedule>('/api/v1/schedule', {}, token),
        api<{ runs: RunRecord[] }>('/api/v1/runs', {}, token),
        api<ArtifactList>('/api/v1/artifacts', {}, token).catch((error: unknown) => {
          if (error instanceof ApiError && error.status === 401) throw error;
          return { enabled: true, artifacts: [], error: error instanceof Error ? error.message : '作品清單無法載入' };
        }),
      ]);
      if (sessionRef.current?.session !== token) return;
      setHealth(healthResult);
      setSchedule(scheduleResult);
      setRuns(runsResult.runs ?? []);
      setArtifacts(artifactResult);
      setShowPrivate(true);
    } catch (error) {
      if (error instanceof ApiError && error.status === 401) clearSession('登入已失效，請重新連接 wallet。');
      else setGlobalError(error instanceof Error ? error.message : String(error));
    } finally {
      setRefreshing(false);
    }
  }, [api, clearSession]);

  const loadTrending = useCallback(async (source = trendSource, since = trendSince) => {
    const requestId = ++trendRequestRef.current;
    setTrendLoading(true);
    setTrendError('');
    try {
      const data = await api<TrendingData>(`/api/v1/trending?source=${encodeURIComponent(source)}&since=${encodeURIComponent(since)}`);
      if (requestId === trendRequestRef.current) setTrending(data);
    } catch (error) {
      if (requestId === trendRequestRef.current) setTrendError(error instanceof Error ? error.message : String(error));
    } finally {
      if (requestId === trendRequestRef.current) setTrendLoading(false);
    }
  }, [api, trendSince, trendSource]);

  const revoke = useCallback(async (token: string) => {
    const response = await fetch('/api/v1/auth/logout', {
      method: 'POST',
      headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
      body: '{}',
    });
    if (!response.ok && response.status !== 401) throw new Error('登出失敗，請再試一次。');
  }, []);

  async function handleAccountsChanged(value: unknown) {
    const accounts = Array.isArray(value) ? value.filter((item): item is string => typeof item === 'string') : [];
    const current = sessionRef.current;
    const activeAddress = current?.address ?? accountRef.current;
    const nextAddress = accounts[0]?.toLowerCase() ?? null;
    if (!activeAddress || activeAddress === nextAddress) return;
    accountRef.current = nextAddress;
    clearSession('MetaMask 帳號已變更，請重新連接。');
    if (current) void revoke(current.session).catch(() => undefined);
  }

  function handleWalletDisconnect() {
    const current = sessionRef.current;
    clearSession('MetaMask 已中斷連線。');
    if (current) void revoke(current.session).catch(() => undefined);
  }

  const attachProvider = useCallback((provider: WalletProvider) => {
    if (providerRef.current === provider) return;
    providerRef.current?.removeListener?.('accountsChanged', handleAccountsChanged);
    providerRef.current?.removeListener?.('disconnect', handleWalletDisconnect);
    providerRef.current = provider;
    provider.on?.('accountsChanged', handleAccountsChanged);
    provider.on?.('disconnect', handleWalletDisconnect);
  // Handlers use refs, so the provider listener can remain attached for this page lifetime.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    folderInput.current?.setAttribute('webkitdirectory', '');
    void loadTrending('github', 'daily');

    const announce = (event: Event) => {
      const detail = (event as CustomEvent<{ info?: { rdns?: string }; provider?: WalletProvider }>).detail;
      if (detail?.info?.rdns === 'io.metamask' && detail.provider) attachProvider(detail.provider);
    };
    window.addEventListener('eip6963:announceProvider', announce);
    window.dispatchEvent(new Event('eip6963:requestProvider'));
    const detected = getWalletProvider();
    if (detected) attachProvider(detected);

    let saved: WalletSession | undefined;
    try { saved = JSON.parse(sessionStorage.getItem(SESSION_KEY) || 'null') as WalletSession | undefined; } catch { /* Ignore malformed local state. */ }
    if (saved?.session && Date.parse(saved.expires_at) > Date.now()) {
      const op = operationRef.current;
      void api<Omit<WalletSession, 'session'>>('/api/v1/auth/session', {}, saved.session)
        .then(async (restored) => {
          if (operationRef.current !== op) return;
          const provider = providerRef.current ?? getWalletProvider();
          if (provider) {
            attachProvider(provider);
            const accounts = await provider.request({ method: 'eth_accounts' }) as string[];
            if (accounts[0]?.toLowerCase() !== restored.address) {
              clearSession('MetaMask 帳號與目前 session 不一致，請重新登入。');
              void revoke(saved!.session).catch(() => undefined);
              return;
            }
            accountRef.current = restored.address;
          }
          const restoredSession = { ...restored, session: saved!.session };
          acceptSession(restoredSession);
          await loadDashboard(restoredSession.session);
        })
        .catch((error: unknown) => {
          if (error instanceof ApiError && error.status === 401) clearSession();
          else setGlobalError(error instanceof Error ? error.message : String(error));
        });
    }
    return () => {
      window.removeEventListener('eip6963:announceProvider', announce);
      providerRef.current?.removeListener?.('accountsChanged', handleAccountsChanged);
      providerRef.current?.removeListener?.('disconnect', handleWalletDisconnect);
    };
  // Mount once; session and wallet listeners read current values through refs.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    if (!session) return;
    const remaining = Date.parse(session.expires_at) - Date.now();
    if (remaining <= 0) {
      clearSession('登入已逾期，請重新連接 wallet。');
      return;
    }
    const timer = window.setTimeout(() => clearSession('登入已逾期，請重新連接 wallet。'), remaining);
    return () => window.clearTimeout(timer);
  }, [session, clearSession]);

  useEffect(() => {
    if (!session || refreshing || !runs.some((run) => run.status === 'queued' || run.status === 'running')) return;
    const timer = window.setTimeout(() => void loadDashboard(session.session), 4000);
    return () => window.clearTimeout(timer);
  }, [session, runs, refreshing, loadDashboard]);

  useEffect(() => {
    const runId = controlRunIdRef.current;
    const run = runId ? runs.find((item) => item.id === runId) : undefined;
    if (!run || run.status === 'queued' || run.status === 'running') return;
    setControlMessage(`${actionLabel(run.action)}作業${statusLabel(run.status)}。`);
    controlRunIdRef.current = null;
  }, [runs]);

  const uploadBytes = useMemo(() => selectedFiles.reduce((sum, file) => sum + file.size, 0), [selectedFiles]);
  const htmlEntrypoints = useMemo(() => selectedFiles
    .map((file) => file.webkitRelativePath || file.name)
    .map((path) => path.includes('/') ? path.slice(path.indexOf('/') + 1) : path)
    .filter((path) => /\.html?$/i.test(path))
    .sort((a, b) => Number(a !== 'index.html') - Number(b !== 'index.html') || a.localeCompare(b)), [selectedFiles]);
  const canOperate = session?.role === 'operator' || session?.role === 'admin';
  const updatable = health?.components.filter((component) => component.update_supported) ?? [];
  const restartable = health?.components.filter((component) => component.restart_supported) ?? [];
  const hasActiveRun = runs.some((run) => run.status === 'queued' || run.status === 'running');
  const controlsBusy = controlSubmitting || hasActiveRun;

  useEffect(() => {
    if (!htmlEntrypoints.length) setEntrypoint('');
    else if (!htmlEntrypoints.includes(entrypoint)) setEntrypoint(htmlEntrypoints[0]);
  }, [htmlEntrypoints, entrypoint]);

  async function signWithWallet(provider: WalletProvider, address: string, message: string): Promise<string> {
    const accounts = await provider.request({ method: 'eth_accounts' }) as string[];
    if (accounts[0]?.toLowerCase() !== address) throw new Error('MetaMask 帳號已變更，請重新連接。');
    const signature = await provider.request({ method: 'personal_sign', params: [encodeMessage(message), address] });
    if (typeof signature !== 'string') throw new Error('MetaMask 未回傳簽章。');
    return signature;
  }

  async function login(address: string, provider: WalletProvider, operation: number) {
    const challenge = await api<{ challenge_id: string; message: string }>(
      `/api/v1/auth/challenge?address=${encodeURIComponent(address)}&origin=${encodeURIComponent(location.origin)}`,
    );
    const signature = await signWithWallet(provider, address, challenge.message);
    const verified = await api<WalletSession>('/api/v1/auth/verify', {
      method: 'POST',
      body: JSON.stringify({ challenge_id: challenge.challenge_id, address, message: challenge.message, signature }),
    });
    if (operationRef.current !== operation || accountRef.current !== address) {
      await revoke(verified.session);
      throw new Error('帳號已變更，請重新連接。');
    }
    acceptSession(verified);
    await loadDashboard(verified.session);
  }

  async function connectWallet() {
    if (connecting || session) return;
    const operation = ++operationRef.current;
    setConnecting(true);
    setAuthMessage('請在 MetaMask 確認連線。');
    setGlobalError('');
    try {
      const provider = providerRef.current ?? getWalletProvider();
      if (!provider) throw new Error('找不到 MetaMask。請安裝或啟用 MetaMask 後重新載入頁面。');
      attachProvider(provider);
      const accounts = await provider.request({ method: 'eth_requestAccounts' }) as string[];
      if (operationRef.current !== operation) return;
      const address = accounts[0]?.toLowerCase();
      if (!address) throw new Error('MetaMask 沒有回傳帳號。');
      accountRef.current = address;
      let registration: { challenge_id: string; message: string } | undefined;
      try {
        registration = await api<{ challenge_id: string; message: string }>(
          '/api/v1/registration/challenge?address=' + encodeURIComponent(address),
        );
      } catch (error) {
        if (!(error instanceof ApiError) || error.status !== 409) throw error;
        setRegistrationRequired(false);
      }
      if (registration) {
        setRegistrationRequired(true);
        setAuthMessage('請在 MetaMask 簽署首次註冊訊息。');
        const signature = await signWithWallet(provider, address, registration.message);
        if (operationRef.current !== operation) return;
        await api('/api/v1/registration', {
          method: 'POST',
          body: JSON.stringify({ challenge_id: registration.challenge_id, address, message: registration.message, signature }),
        });
        setRegistrationRequired(false);
      }
      if (operationRef.current === operation) {
        setAuthMessage('請在 MetaMask 簽署登入訊息。');
        await login(address, provider, operation);
      }
    } catch (error) {
      if (operationRef.current === operation) setAuthMessage(error instanceof Error ? error.message : String(error));
    } finally {
      if (operationRef.current === operation) setConnecting(false);
    }
  }

  async function refresh() {
    if (session) await loadDashboard(session.session);
  }

  async function logout() {
    if (!session) return;
    const current = session;
    try {
      await revoke(current.session);
      if (sessionRef.current?.session === current.session) clearSession();
    } catch (error) {
      setGlobalError(error instanceof Error ? error.message : String(error));
    }
  }

  async function startControlAction(action: 'update' | 'restart', ids: string[]) {
    const activeSession = sessionRef.current;
    if (!activeSession || !canOperate || controlsBusy || !ids.length) return;
    if (action === 'restart') {
      const labels = ids.map((id) => health?.components.find((component) => component.id === id)?.label || id);
      const selection = labels.length === 1
        ? `「${labels[0]}」`
        : `${labels.length} 個元件（${labels.slice(0, 3).join('、')}${labels.length > 3 ? '…' : ''}）`;
      if (!window.confirm(`確定要重啟${selection}？元件宣告的相依服務也會依相依順序重啟，服務可能短暫中斷。`)) return;
    }
    const token = activeSession.session;
    const label = actionLabel(action);
    setControlSubmitting(true);
    setControlMessage(`${label}請求送出中…`);
    setGlobalError('');
    try {
      const run = await api<{ id?: string }>('/api/v1/update-runs', {
        method: 'POST', body: JSON.stringify({ action, component_ids: ids, target_versions: {} }),
      }, token);
      if (sessionRef.current?.session !== token) return;
      controlRunIdRef.current = run.id || null;
      setControlMessage(`${label}已排入執行佇列${run.id ? ` · ${run.id}` : ''}，狀態會自動更新。`);
      await loadDashboard(token);
    } catch (error) {
      if (sessionRef.current?.session === token) {
        setControlMessage('');
        setGlobalError(error instanceof Error ? error.message : String(error));
      }
    } finally {
      setControlSubmitting(false);
    }
  }

  async function savePolicy(enabled: boolean) {
    if (!session) return;
    setGlobalError('');
    const components = enabled ? updatable.map((component) => component.id) : undefined;
    try {
      await api('/api/v1/policy', {
        method: 'PUT',
        body: JSON.stringify({ enabled, ...(components ? { components } : {}), allow_restart: true, release_channel: 'stable' }),
      });
      await refresh();
    } catch (error) { setGlobalError(error instanceof Error ? error.message : String(error)); }
  }

  function pickFiles(files: FileList | null, folder: boolean) {
    if (!files) return;
    const next = Array.from(files);
    const total = next.reduce((sum, file) => sum + file.size, 0);
    if (next.length > MAX_UPLOAD_FILES || total > MAX_UPLOAD_BYTES) {
      setSelectedFiles([]);
      setArtifactMessage(`檔案超過限制：最多 ${MAX_UPLOAD_FILES} 個檔案、合計 10 MiB。`);
    } else {
      setSelectedFiles(next);
      setArtifactMessage('');
    }
    if (folder && fileInput.current) fileInput.current.value = '';
    if (!folder && folderInput.current) folderInput.current.value = '';
  }

  async function publishArtifact(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!session || !canOperate || !selectedFiles.length || !entrypoint || publishing) return;
    const name = artifactName.trim();
    const overwriting = artifacts.artifacts.some((artifact) => artifact.name === name);
    if (overwriting && !window.confirm(`取代「${name}」的全部檔案？舊內容會被覆蓋。`)) return;
    const token = session.session;
    setPublishing(true);
    setArtifactMessage('正在發布…');
    try {
      const files = await Promise.all(selectedFiles.map(async (file) => {
        const rawPath = file.webkitRelativePath || file.name;
        const path = rawPath.includes('/') ? rawPath.slice(rawPath.indexOf('/') + 1) : rawPath;
        const bytes = new Uint8Array(await file.arrayBuffer());
        let binary = '';
        for (let offset = 0; offset < bytes.length; offset += 8192) {
          binary += String.fromCharCode(...bytes.subarray(offset, offset + 8192));
        }
        return { path, content_base64: btoa(binary) };
      }));
      if (sessionRef.current?.session !== token) return;
      const receipt = await api<{ name: string }>('/api/v1/artifacts', {
        method: 'POST', body: JSON.stringify({ name, entrypoint, files, overwrite: overwriting }),
      }, token);
      await loadDashboard(token);
      if (sessionRef.current?.session === token) setArtifactMessage(`已發布 ${receipt.name}。`);
    } catch (error) { setArtifactMessage(error instanceof Error ? error.message : String(error)); }
    finally { setPublishing(false); }
  }

  async function deleteArtifact(name: string) {
    if (!session || !canOperate || !window.confirm(`刪除「${name}」？作品連結將失效，且無法復原。`)) return;
    try {
      await api('/api/v1/artifacts/' + encodeURIComponent(name), { method: 'DELETE' });
      await refresh();
    } catch (error) { setGlobalError(error instanceof Error ? error.message : String(error)); }
  }

  return (
    <>
    <a className="skip-link" href="#main-content">略過導覽</a>
    <div className="app-shell">
      <aside className="sidebar">
        <a className="brand" href="#top" aria-label="ADES 首頁">
          <span className="brand-mark">AE</span><span><strong>ADES</strong><small>Environment service</small></span>
        </a>
        <p className="sidebar-label">Workspace</p>
        <nav className="side-nav" aria-label="主要導覽">
          <a href="#trending"><span>01</span>專案排行</a>
          {session && <>
            <p className="sidebar-label">管理</p><a href="#environment"><span>02</span>環境總覽</a>
            {artifacts.enabled && <a href="#artifact-panel"><span>03</span>網頁發布</a>}
            <a href="#schedule-panel"><span>04</span>更新排程</a><a href="#runs-panel"><span>05</span>執行紀錄</a>
          </>}
        </nav>
        <div className="sidebar-footer"><i />{session ? '已連線至此環境' : '公開閱讀模式'}</div>
      </aside>

      <div className="workspace" id="top">
        <header className="topbar">
          <div><p className="eyebrow">ADES / {session ? 'MANAGEMENT CONSOLE' : 'PUBLIC FEED'}</p>
            <h1>{session ? '環境管理' : 'GitHub 專案排行'}</h1></div>
          <div className="topbar-actions">
            {session ? <>
              <span className="identity">{session.role} · {session.address}</span>
              <Button appearance="subtle" type="button" onClick={() => void refresh()} disabled={refreshing}>{refreshing ? '更新中…' : '重新整理'}</Button>
              <Button appearance="subtle" type="button" onClick={() => void logout()}>登出</Button>
            </> : <span className="identity">尚未登入</span>}
          </div>
        </header>

        <main id="main-content">
          {!session && <section className="auth-stage" aria-labelledby="auth-title">
            <article className="auth-card">
              <span className="auth-mark" aria-hidden="true">AE</span>
              <p className="auth-tag">{connecting ? 'WALLET AUTHORIZATION' : registrationRequired === null ? 'WALLET ACCESS' : registrationRequired ? 'FIRST REGISTRATION' : 'WALLET LOGIN'}</p>
              <h2 id="auth-title">{registrationRequired === null ? '連接 wallet' : registrationRequired ? '註冊這台環境' : '登入 ADES'}</h2>
              <p>{registrationRequired === null
                ? '連接 MetaMask 後，ADES 會確認這台環境是否需要首次註冊。'
                : registrationRequired
                  ? '尚未完成註冊。第一個註冊的 wallet 會成為這台環境的管理者。'
                  : '使用已授權的 MetaMask wallet 登入，查看環境狀態與管理功能。'}</p>
              <Button appearance="primary" type="button" onClick={() => void connectWallet()} disabled={connecting}>{connecting ? '請在 MetaMask 確認…' : registrationRequired === null ? '連接 MetaMask' : registrationRequired ? '使用 MetaMask 註冊' : '使用 MetaMask 登入'}</Button>
              <p className="auth-message" role="status" aria-live="polite">{authMessage}</p>
              <small>登入 session 最長有效 12 小時，登出後立即撤銷。</small>
            </article>
          </section>}

          {session && globalError && <MessageBar intent="error"><MessageBarBody>{globalError}</MessageBarBody></MessageBar>}

          <section className="panel-shell trending-panel-shell" id="trending" aria-labelledby="trending-title">
            <Card appearance="filled-alternative" className="panel trending-panel">
            <div className="panel-heading">
              <div><h2 id="trending-title">GitHub 專案即時排行</h2><p>公開查看排行，資料不會寫入 ADE 管理紀錄。</p></div>
              <div className="field-row">
                <Field label="資料來源" size="small">
                  <Dropdown value={trendSource} selectedOptions={[trendSource]} onOptionSelect={(_event, data) => {
                    const value = data.optionValue;
                    if (value !== 'github' && value !== 'trendshift') return;
                    setTrendSource(value); void loadTrending(value, trendSince);
                  }}>
                    <Option value="github">GitHub Trending</Option><Option value="trendshift">Trendshift</Option>
                  </Dropdown>
                </Field>
                <Field label="期間" size="small">
                  <Dropdown value={trendSince} selectedOptions={[trendSince]} onOptionSelect={(_event, data) => {
                    const value = data.optionValue;
                    if (value !== 'daily' && value !== 'weekly' && value !== 'monthly') return;
                    setTrendSince(value); void loadTrending(trendSource, value);
                  }}>
                    <Option value="daily">每日</Option><Option value="weekly">每週</Option><Option value="monthly">每月</Option>
                  </Dropdown>
                </Field>
                <Button appearance="subtle" type="button" onClick={() => void loadTrending()} disabled={trendLoading}>{trendLoading ? '載入中…' : '重新整理排行'}</Button>
              </div>
            </div>
            {trending && <p className="feed-meta">
              {safeExternalUrl(trending.sourceUrl) && <a href={trending.sourceUrl} target="_blank" rel="noreferrer">{trending.label || '資料來源'}</a>}
              <span>擷取於 {formatDate(trending.fetchedAt)}</span>
            </p>}
            {trendError && <p className="error-text" role="alert">{trendError}</p>}
            {trendLoading && !trending && <p className="empty-state">正在取得排行…</p>}
            <ol className="rank-list">{(trending?.items ?? []).map((item, index) => <li className="rank-item" key={`${item.fullName}-${index}`}>
              <span className="rank-number">{String(index + 1).padStart(2, '0')}</span><div className="rank-content">
                <div className="rank-title">{safeExternalUrl(item.url) ? <a href={item.url} target="_blank" rel="noreferrer">{item.fullName}</a> : item.fullName}
                  {item.language && <span className="language-tag">{item.language}</span>}</div>
                <div className="rank-meta"><span>★ {Number(item.starsTotal || 0).toLocaleString('en-US')}</span>{item.starsToday > 0 && <span>↗ {Number(item.starsToday).toLocaleString('en-US')} 本期新增</span>}</div>
                {item.description && <p className="rank-description">{item.description}</p>}
              </div>
            </li>)}</ol>
            {trending && !trending.items.length && <p className="empty-state">目前沒有可顯示的項目。</p>}
            </Card>
          </section>

          {session && showPrivate && <div className="private-area">
            <section className="panel-shell" id="environment">
              <Card appearance="filled-alternative" className="panel">
              <div className="panel-heading"><div><p className="eyebrow">VM HEALTH</p><h2>環境總覽</h2>
                <p>{health ? `VM ${health.vm_id} · 檢查於 ${formatDate(health.checked_at)}` : '載入環境狀態…'}</p></div>
                {health && <StatusBadge status={health.status} />}
              </div>
              {(controlMessage || hasActiveRun) && <MessageBar intent={hasActiveRun ? 'info' : 'success'}><MessageBarBody role="status" aria-live="polite">{controlMessage || '維護作業執行中，完成後會自動更新狀態。'}</MessageBarBody></MessageBar>}
              <div className="component-grid">{(health?.components ?? []).map((component) => <article className="component-item" key={component.id}><Card appearance="filled" className="component-card">
                <div className="card-heading"><StatusBadge status={component.status} /><span className="component-kind">{component.kind}</span></div>
                <h3>{component.label}</h3><p className="component-version">版本 {component.version || 'unknown'}</p>
                {component.detail && <p className="component-detail">{component.detail}</p>}
                {(component.update_supported || component.restart_supported) && <div className="component-actions">
                  {component.update_supported && <Button appearance="secondary" size="small" type="button" disabled={!canOperate || controlsBusy} onClick={() => void startControlAction('update', [component.id])}>更新此元件</Button>}
                  {component.restart_supported && <Button appearance="outline" size="small" type="button" className="restart-action" disabled={!canOperate || controlsBusy} onClick={() => void startControlAction('restart', [component.id])}>重啟此元件</Button>}
                </div>}
              </Card></article>)}</div>
              {!health?.components.length && <p className="empty-state">目前沒有可顯示的元件。</p>}
              {canOperate && (!!updatable.length || !!restartable.length) && <div className="panel-actions">
                {!!updatable.length && <Button appearance="primary" type="button" disabled={controlsBusy} onClick={() => void startControlAction('update', updatable.map((component) => component.id))}>更新所有可管理元件</Button>}
                {!!restartable.length && <Button appearance="outline" type="button" className="restart-action" disabled={controlsBusy} onClick={() => void startControlAction('restart', restartable.map((component) => component.id))}>重啟所有可管理元件</Button>}
              </div>}
              </Card>
            </section>

            {artifacts.enabled && <section className="panel-shell" id="artifact-panel">
              <Card appearance="filled-alternative" className="panel">
              <div className="panel-heading"><div><p className="eyebrow">WEB ARTIFACTS</p><h2>網頁發布</h2><p>發布內容使用獨立的 artifact origin，不會在管理頁面內執行。</p></div>
                <Badge appearance="tint" color={artifacts.publisher_ready ? 'success' : 'warning'} shape="square" size="small">{artifacts.publisher_ready ? 'publisher ready' : 'publish blocked'}</Badge>
              </div>
              <p className="artifact-note" role="status">{artifactMessage || artifacts.error || (artifacts.publisher_ready ? `${artifacts.artifacts.length} 個已發布作品` : '網頁發布服務尚未就緒。')}</p>
              {canOperate && <form className="artifact-form" onSubmit={(event) => void publishArtifact(event)}>
                <Field className="artifact-name-field" label="作品名稱" required size="small">
                  <Input value={artifactName} onChange={(_event, data) => setArtifactName(data.value)} placeholder="my-report" pattern="[a-z0-9][a-z0-9._-]{0,63}" required />
                </Field>
                <div className="file-pickers">
                  <label className="file-picker">選擇檔案<input ref={fileInput} type="file" multiple onChange={(event) => pickFiles(event.target.files, false)} /></label>
                  <label className="file-picker">選擇資料夾<input ref={folderInput} type="file" multiple onChange={(event) => pickFiles(event.target.files, true)} /></label>
                  <small>{selectedFiles.length} 個檔案 · {(uploadBytes / 1024).toFixed(1)} KiB，最多 200 個檔案與 10 MiB</small>
                </div>
                <Field className="artifact-entry-field" label="首頁檔案" required size="small">
                  <Dropdown value={entrypoint} selectedOptions={entrypoint ? [entrypoint] : []} onOptionSelect={(_event, data) => {
                    if (data.optionValue) setEntrypoint(data.optionValue);
                  }} disabled={!htmlEntrypoints.length}>
                    {htmlEntrypoints.map((path) => <Option key={path} value={path}>{path}</Option>)}
                  </Dropdown>
                </Field>
                <Button appearance="primary" type="submit" disabled={!artifacts.publisher_ready || publishing || !selectedFiles.length || !entrypoint}>{publishing ? '發布中…' : '發布作品'}</Button>
              </form>}
              <div className="artifact-grid">{artifacts.artifacts.map((artifact) => <article className="artifact-item" key={artifact.name}><Card appearance="filled" className="artifact-card">
                <div><h3>{artifact.name}</h3><p>{artifact.file_count} 個檔案 · {(artifact.size_bytes / 1024).toFixed(1)} KiB</p></div>
                <div className="artifact-links">{artifact.pages.map((page) => safeExternalUrl(page.access_url) && <a key={page.entrypoint} href={page.access_url} target="_blank" rel="noopener noreferrer">開啟 {page.entrypoint}</a>)}</div>
                {canOperate && <Button appearance="outline" type="button" className="danger-action" onClick={() => void deleteArtifact(artifact.name)}>刪除</Button>}
              </Card></article>)}</div>
              {!artifacts.artifacts.length && <p className="empty-state">尚未發布作品。</p>}
              </Card>
            </section>}

            <section className="panel-shell" id="schedule-panel">
              <Card appearance="filled-alternative" className="panel">
              <div className="panel-heading"><div><p className="eyebrow">UPDATE POLICY</p><h2>更新排程</h2>
                <p>{schedule ? `每日 ${schedule.schedule.time} ${schedule.schedule.timezone} 執行 · missed run 會補跑` : '載入排程…'}</p></div>
                <Badge appearance="tint" color={schedule?.policy_valid ? 'success' : 'subtle'} shape="square" size="small">{schedule?.policy_valid ? '已啟用' : '未啟用'}</Badge>
              </div>
              <p className="policy-reason">{schedule?.policy_valid ? '每日更新會持續執行，直到管理者停用。' : schedule?.policy_reason || '尚無排程設定。'}</p>
              {session.role === 'admin' && <div className="panel-actions">
                <Button appearance="primary" type="button" disabled={!updatable.length} onClick={() => void savePolicy(true)}>啟用每日更新</Button>
                <Button appearance="subtle" type="button" disabled={!schedule?.policy_valid} onClick={() => void savePolicy(false)}>停用排程</Button>
              </div>}
              </Card>
            </section>

            <section className="panel-shell" id="runs-panel">
              <Card appearance="filled-alternative" className="panel">
              <div className="panel-heading"><div><p className="eyebrow">HISTORY</p><h2>執行紀錄</h2><p>最近更新與維護操作。</p></div></div>
              {runs.length ? <div className="run-list">{runs.map((run) => <article className="run-row" key={run.id}>
                <div className="run-title"><code>{run.id}</code><StatusBadge status={run.status} /></div>
                <p>{triggerLabel(run.trigger)} · {actionLabel(run.action)} · {formatDate(run.created_at)}</p>
                {!!run.component_ids?.length && <p>元件：{run.component_ids.map((id) => health?.components.find((component) => component.id === id)?.label || id).join('、')}</p>}
                {!!run.components?.length && <ul className="run-components">{run.components.map((component) => <li key={component.id}>
                  <span>{health?.components.find((item) => item.id === component.id)?.label || component.id}</span>
                  <StatusBadge status={component.status} />
                  {component.detail && component.status !== 'completed' && <p className="run-error">{component.detail}</p>}
                </li>)}</ul>}
                {run.error && <p className="run-error">{run.error}</p>}
              </article>)}</div> : <p className="empty-state">尚無執行紀錄。</p>}
              </Card>
            </section>
          </div>}
        </main>
      </div>
    </div>
    </>
  );
}

createRoot(document.getElementById('root')!).render(
  <FluentProvider theme={adeTheme} className="fluent-root">
    <React.StrictMode><App /></React.StrictMode>
  </FluentProvider>,
);
