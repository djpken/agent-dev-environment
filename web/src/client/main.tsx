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
interface AuthSession { session: string; username: string; role: Role; expires_at: string; }
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
interface AgentSkillStatus {
  id: string; status: string; installed: boolean; file_count: number; changed_files: number;
}
interface AgentMCPStatus {
  id: string; label: string; transport: string; enabled: boolean; installed: boolean;
  installed_version?: string; latest_version?: string; update_status: string;
  connection_status: string;
}
interface AgentAssetReport {
  checked_at: string; repository: string; source_ref: string; installation: string;
  skills: AgentSkillStatus[]; mcp_servers: AgentMCPStatus[];
  summary: {
    skills: number; skill_updates_available: number; skills_available: number;
    mcp_servers: number; mcp_updates_available: number; mcp_enabled: number; mcp_unhealthy: number;
  };
}
interface RunRecord {
  id: string; trigger: string; action: string; status: string; created_at: string;
  finished_at?: string; error?: string;
  component_ids?: string[];
  components?: Array<{ id: string; status: string; detail?: string }>;
}
interface LocalDeployment {
  sha: string; subject?: string; status: string; stage?: string;
  queued_at: string; started_at?: string; finished_at?: string; error?: string;
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

const SESSION_KEY = 'ade.environment.session.v2';
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
    queued: '排隊中', running: '執行中', completed: '完成', succeeded: '部署成功', updating: '更新中',
    partial_failure: '部分失敗', failed: '失敗', blocked: '已阻擋', skipped: '略過',
    current: '已同步', update_available: '有更新', available: '尚未安裝',
    removed_upstream: '上游已移除', disabled: '未啟用', ahead: '版本較新',
    source_missing: '來源已移除', untracked: '未追蹤來源', not_checked: '未檢查', not_installed: '未安裝',
  };
  return labels[status] || status;
}

function statusColor(status: string): NonNullable<BadgeProps['color']> {
  if (['healthy', 'completed', 'succeeded', 'current'].includes(status)) return 'success';
  if (['degraded', 'unknown', 'queued', 'running', 'update_available', 'available', 'removed_upstream', 'disabled', 'ahead', 'source_missing', 'untracked', 'not_checked', 'not_installed'].includes(status)) return 'warning';
  if (['unhealthy', 'failed', 'blocked', 'partial_failure'].includes(status)) return 'danger';
  if (status === 'updating') return 'informative';
  return 'subtle';
}

function StatusBadge({ status }: { status: string }) {
  return <Badge appearance="tint" color={statusColor(status)} shape="square" size="small">{statusLabel(status)}</Badge>;
}

function deploymentStageLabel(stage?: string): string {
  const labels: Record<string, string> = {
    queued: '等待執行', checking: '檢查 commit', archiving: '準備來源',
    frontend_dependencies: '安裝前端相依套件', frontend_build: '建置前端',
    go_build: '編譯 Go runtime', switching: '切換服務', health_check: '檢查服務',
    complete: '完成', rollback: '回復舊版本', superseded: '已有更新的 commit',
  };
  return stage ? labels[stage] || stage : '尚未開始';
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
  const [session, setSession] = useState<AuthSession | null>(null);
  const sessionRef = useRef<AuthSession | null>(null);
  sessionRef.current = session;
  const [connecting, setConnecting] = useState(false);
  const [setupRegistered, setSetupRegistered] = useState<boolean | null>(null);
  const [authMode, setAuthMode] = useState<'choose' | 'login' | 'register' | 'import'>('choose');
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [authMessage, setAuthMessage] = useState('');
  const [webdavUrl, setWebdavUrl] = useState('');
  const [webdavUsername, setWebdavUsername] = useState('');
  const [webdavPassword, setWebdavPassword] = useState('');
  const [backupPassphrase, setBackupPassphrase] = useState('');
  const [backupBusy, setBackupBusy] = useState(false);
  const [backupMessage, setBackupMessage] = useState('');
  const [globalError, setGlobalError] = useState('');
  const [health, setHealth] = useState<Health | null>(null);
  const [agentAssets, setAgentAssets] = useState<AgentAssetReport | null>(null);
  const [agentAssetsLoading, setAgentAssetsLoading] = useState(false);
  const [agentAssetsError, setAgentAssetsError] = useState('');
  const [schedule, setSchedule] = useState<Schedule | null>(null);
  const [runs, setRuns] = useState<RunRecord[]>([]);
  const [deployments, setDeployments] = useState<LocalDeployment[]>([]);
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
    sessionRef.current = null;
    setSession(null);
    setConnecting(false);
    setHealth(null);
    setAgentAssets(null);
    setAgentAssetsLoading(false);
    setAgentAssetsError('');
    setSchedule(null);
    setRuns([]);
    setDeployments([]);
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

  const acceptSession = useCallback((next: AuthSession) => {
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
      const [healthResult, scheduleResult, runsResult, deploymentResult, artifactResult] = await Promise.all([
        api<Health>('/api/v1/health', {}, token),
        api<Schedule>('/api/v1/schedule', {}, token),
        api<{ runs: RunRecord[] }>('/api/v1/runs', {}, token),
        api<{ deployments: LocalDeployment[] }>('/api/v1/deployments', {}, token),
        api<ArtifactList>('/api/v1/artifacts', {}, token).catch((error: unknown) => {
          if (error instanceof ApiError && error.status === 401) throw error;
          return { enabled: true, artifacts: [], error: error instanceof Error ? error.message : '作品清單無法載入' };
        }),
      ]);
      if (sessionRef.current?.session !== token) return;
      setHealth(healthResult);
      setSchedule(scheduleResult);
      setRuns(runsResult.runs ?? []);
      setDeployments(deploymentResult.deployments ?? []);
      setArtifacts(artifactResult);
      setShowPrivate(true);
    } catch (error) {
      if (error instanceof ApiError && error.status === 401) clearSession('登入已失效，請重新登入。');
      else setGlobalError(error instanceof Error ? error.message : String(error));
    } finally {
      setRefreshing(false);
    }
  }, [api, clearSession]);

  async function checkAgentAssets() {
    const current = sessionRef.current;
    if (!current) return;
    setAgentAssetsLoading(true);
    setAgentAssetsError('');
    try {
      const result = await api<AgentAssetReport>('/api/v1/agent-assets/check', {
        method: 'POST', body: '{}',
      }, current.session);
      if (sessionRef.current?.session === current.session) setAgentAssets(result);
    } catch (error) {
      if (error instanceof ApiError && error.status === 401) clearSession('登入已失效，請重新登入。');
      else setAgentAssetsError(error instanceof Error ? error.message : String(error));
    } finally {
      setAgentAssetsLoading(false);
    }
  }

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

  async function refreshSetupStatus() {
    setAuthMessage('正在確認 ADES 帳號狀態…');
    try {
      const status = await api<{ registered: boolean }>('/api/v1/auth/setup-status');
      setSetupRegistered(status.registered);
      setAuthMode(status.registered ? 'login' : 'choose');
      setAuthMessage('');
    } catch (error) {
      setSetupRegistered(null);
      setAuthMessage(error instanceof Error ? error.message : '無法確認 ADES 帳號狀態。');
    }
  }

  useEffect(() => {
    folderInput.current?.setAttribute('webkitdirectory', '');
    void loadTrending('github', 'daily');
    void refreshSetupStatus();
    let saved: AuthSession | undefined;
    try { saved = JSON.parse(sessionStorage.getItem(SESSION_KEY) || 'null') as AuthSession | undefined; } catch { /* Ignore malformed local state. */ }
    if (saved?.session && Date.parse(saved.expires_at) > Date.now()) {
      void api<Omit<AuthSession, 'session'>>('/api/v1/auth/session', {}, saved.session)
        .then(async (restored) => {
          const restoredSession = { ...restored, session: saved!.session };
          acceptSession(restoredSession);
          await loadDashboard(restoredSession.session);
        })
        .catch((error: unknown) => {
          if (error instanceof ApiError && error.status === 401) clearSession();
          else setGlobalError(error instanceof Error ? error.message : String(error));
        });
    }
  // Mount once; session state is validated by the server on reload.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    if (!session) return;
    const remaining = Date.parse(session.expires_at) - Date.now();
    if (remaining <= 0) {
      clearSession('登入已逾期，請重新登入。');
      return;
    }
    const timer = window.setTimeout(() => clearSession('登入已逾期，請重新登入。'), remaining);
    return () => window.clearTimeout(timer);
  }, [session, clearSession]);

  useEffect(() => {
    const activeRun = runs.some((run) => run.status === 'queued' || run.status === 'running');
    const activeDeployment = deployments.some((deployment) => deployment.status === 'queued' || deployment.status === 'running');
    if (!session || refreshing || (!activeRun && !activeDeployment)) return;
    const timer = window.setTimeout(() => void loadDashboard(session.session), 4000);
    return () => window.clearTimeout(timer);
  }, [session, runs, deployments, refreshing, loadDashboard]);

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
  const hasActiveDeployment = deployments.some((deployment) => deployment.status === 'queued' || deployment.status === 'running');
  const controlsBusy = controlSubmitting || hasActiveRun;

  useEffect(() => {
    if (!htmlEntrypoints.length) setEntrypoint('');
    else if (!htmlEntrypoints.includes(entrypoint)) setEntrypoint(htmlEntrypoints[0]);
  }, [htmlEntrypoints, entrypoint]);

  async function login(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (connecting || session) return;
    if (window.location.protocol !== 'https:') {
      setAuthMessage('密碼登入需要 HTTPS，請使用安全連線網址。');
      return;
    }
    setConnecting(true);
    setAuthMessage('正在登入 ADES…');
    setGlobalError('');
    try {
      const verified = await api<AuthSession>('/api/v1/auth/login', {
      method: 'POST',
        body: JSON.stringify({ username, password }),
      });
      acceptSession(verified);
      setPassword('');
      await loadDashboard(verified.session);
    } catch (error) {
      setAuthMessage(error instanceof ApiError && error.status === 401
        ? '使用者名稱或密碼錯誤。'
        : error instanceof Error ? error.message : String(error));
    } finally {
      setConnecting(false);
    }
  }

  async function register(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (connecting || session) return;
    if (window.location.protocol !== 'https:') {
      setAuthMessage('註冊需要 HTTPS，請使用安全連線網址。');
      return;
    }
    setConnecting(true);
    setAuthMessage('正在建立第一個管理員帳號…');
    try {
      const created = await api<AuthSession>('/api/v1/auth/register', {
        method: 'POST', body: JSON.stringify({ username, password }),
      });
      setSetupRegistered(true);
      acceptSession(created);
      setPassword('');
      await loadDashboard(created.session);
    } catch (error) {
      setAuthMessage(error instanceof Error ? error.message : String(error));
      if (error instanceof ApiError && error.status === 409) {
        setSetupRegistered(true);
        setAuthMode('login');
      }
    } finally {
      setConnecting(false);
    }
  }

  async function importFromWebDAV(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (connecting || session) return;
    if (window.location.protocol !== 'https:') {
      setAuthMessage('WebDAV 匯入需要 HTTPS，請使用安全連線網址。');
      return;
    }
    setConnecting(true);
    setAuthMessage('正在下載並驗證加密備份…');
    try {
      const result = await api<{ imported: boolean; restart_required?: boolean; restart_scheduled?: boolean }>(
        '/api/v1/auth/webdav-import',
        {
          method: 'POST',
          body: JSON.stringify({
            webdav_url: webdavUrl,
            webdav_username: webdavUsername,
            webdav_password: webdavPassword,
            backup_passphrase: backupPassphrase,
          }),
        },
      );
      setSetupRegistered(true);
      setAuthMode('login');
      setUsername('');
      setPassword('');
      setWebdavPassword('');
      setBackupPassphrase('');
      const message = result.restart_required
        ? result.restart_scheduled
          ? '環境設定已匯入，ADES 正在重新啟動。請稍後用備份中的帳號登入。'
          : '環境設定已匯入，但無法自動重新啟動 ADES。請先重新啟動服務，再用備份中的帳號登入。'
        : '匯入完成，請用備份中的帳號與密碼登入。';
      setAuthMessage(message);
    } catch (error) {
      setAuthMessage(error instanceof Error ? error.message : String(error));
      if (error instanceof ApiError && error.status === 409) {
        setSetupRegistered(true);
        setAuthMode('login');
      }
    } finally {
      setConnecting(false);
    }
  }

  async function backupToWebDAV(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!session || session.role !== 'admin' || backupBusy) return;
    if (window.location.protocol !== 'https:') {
      setBackupMessage('備份需要 HTTPS，請使用安全連線網址。');
      return;
    }
    if (!window.confirm('加密備份會覆蓋 WebDAV URL 上的同名檔案，內容包含 ADES 設定、帳號雜湊與更新政策。確定繼續？')) return;
    setBackupBusy(true);
    setBackupMessage('正在加密並上傳環境設定…');
    try {
      const result = await api<{ backed_up: boolean; size_bytes: number }>('/api/v1/environment/backup', {
        method: 'POST',
        body: JSON.stringify({
          webdav_url: webdavUrl,
          webdav_username: webdavUsername,
          webdav_password: webdavPassword,
          backup_passphrase: backupPassphrase,
        }),
      });
      setBackupMessage(`備份完成，已上傳 ${(result.size_bytes / 1024).toFixed(1)} KiB 加密檔案。`);
      setWebdavPassword('');
      setBackupPassphrase('');
    } catch (error) {
      setBackupMessage(error instanceof Error ? error.message : String(error));
    } finally {
      setBackupBusy(false);
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
            <a href="#deployments-panel"><span>03</span>本機部署</a>
            {artifacts.enabled && <a href="#artifact-panel"><span>04</span>網頁發布</a>}
            <a href="#schedule-panel"><span>05</span>更新排程</a><a href="#runs-panel"><span>06</span>執行紀錄</a>
            {session.role === 'admin' && <a href="#backup-panel"><span>07</span>設定備份</a>}
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
              <span className="identity">{session.role} · {session.username}</span>
              <Button appearance="subtle" type="button" onClick={() => void refresh()} disabled={refreshing}>{refreshing ? '更新中…' : '重新整理'}</Button>
              <Button appearance="subtle" type="button" onClick={() => void logout()}>登出</Button>
            </> : <span className="identity">尚未登入</span>}
          </div>
        </header>

        <main id="main-content">
          {!session && <section className="auth-stage" aria-labelledby="auth-title">
            <article className="auth-card">
              <span className="auth-mark" aria-hidden="true">AE</span>
              <p className="auth-tag">{setupRegistered ? 'ACCOUNT LOGIN' : 'FIRST-TIME SETUP'}</p>
              <h2 id="auth-title">{setupRegistered ? '登入 ADES' : authMode === 'register' ? '註冊管理員帳號' : authMode === 'import' ? '從 WebDAV 匯入' : '設定 ADES 帳號'}</h2>
              <p>{setupRegistered
                ? '使用這台環境的帳號與密碼登入，查看狀態並管理受控作業。'
                : '目前沒有 ADES 使用者，請註冊第一個管理員帳號，或從 WebDAV 還原加密備份。'}</p>
              {setupRegistered === null && <div className="auth-form">
                <Button appearance="secondary" type="button" onClick={() => void refreshSetupStatus()}>重新檢查</Button>
              </div>}
              {setupRegistered === false && authMode === 'choose' && <div className="setup-choice">
                <Button appearance="primary" type="button" onClick={() => { setAuthMode('register'); setAuthMessage(''); }}>註冊</Button>
                <Button appearance="secondary" type="button" onClick={() => { setAuthMode('import'); setAuthMessage(''); }}>WebDAV 匯入</Button>
              </div>}
              {setupRegistered === false && authMode === 'register' && <form className="auth-form" onSubmit={(event) => void register(event)}>
                <Field label="使用者名稱" required>
                  <Input name="username" type="text" autoComplete="username" autoCapitalize="none" spellCheck={false}
                    value={username} onChange={(_event, data) => setUsername(data.value)} required />
                </Field>
                <Field label="密碼，至少 12 個字元" required>
                  <Input name="password" type="password" autoComplete="new-password" minLength={12}
                    value={password} onChange={(_event, data) => setPassword(data.value)} required />
                </Field>
                <div className="auth-actions">
                  <Button appearance="primary" type="submit" disabled={connecting || !username || password.length < 12}>{connecting ? '建立中…' : '建立管理員帳號'}</Button>
                  <Button appearance="subtle" type="button" disabled={connecting} onClick={() => { setAuthMode('choose'); setAuthMessage(''); }}>返回</Button>
                </div>
              </form>}
              {setupRegistered === false && authMode === 'import' && <form className="auth-form" onSubmit={(event) => void importFromWebDAV(event)}>
                <Field label="WebDAV 備份檔案 URL" required>
                  <Input name="webdav_url" type="url" autoComplete="url" placeholder="https://dav.example.com/ades-backup.json"
                    value={webdavUrl} onChange={(_event, data) => setWebdavUrl(data.value)} required />
                </Field>
                <Field label="WebDAV 使用者名稱">
                  <Input name="webdav_username" type="text" autoComplete="off" autoCapitalize="none"
                    value={webdavUsername} onChange={(_event, data) => setWebdavUsername(data.value)} />
                </Field>
                <Field label="WebDAV 密碼">
                  <Input name="webdav_password" type="password" autoComplete="off"
                    value={webdavPassword} onChange={(_event, data) => setWebdavPassword(data.value)} />
                </Field>
                <Field label="備份加密密碼，至少 12 個字元" required>
                  <Input name="backup_passphrase" type="password" autoComplete="off" minLength={12}
                    value={backupPassphrase} onChange={(_event, data) => setBackupPassphrase(data.value)} required />
                </Field>
                <div className="auth-actions">
                  <Button appearance="primary" type="submit" disabled={connecting || !webdavUrl || backupPassphrase.length < 12}>{connecting ? '匯入中…' : '下載並匯入'}</Button>
                  <Button appearance="subtle" type="button" disabled={connecting} onClick={() => { setAuthMode('choose'); setAuthMessage(''); }}>返回</Button>
                </div>
              </form>}
              {setupRegistered === true && <form className="auth-form" onSubmit={(event) => void login(event)}>
                <Field label="使用者名稱" required>
                  <Input name="username" type="text" autoComplete="username" autoCapitalize="none" spellCheck={false}
                    value={username} onChange={(_event, data) => setUsername(data.value)} required />
                </Field>
                <Field label="密碼" required>
                  <Input name="password" type="password" autoComplete="current-password"
                    value={password} onChange={(_event, data) => setPassword(data.value)} required />
                </Field>
                <Button appearance="primary" type="submit" disabled={connecting || !username || !password}>
                  {connecting ? '登入中…' : '登入'}
                </Button>
              </form>}
              <p className="auth-message" role="status" aria-live="polite">{authMessage}</p>
              <small>帳號與密碼欄位支援 1Password 自動填入。登入、註冊與 WebDAV 憑證都需要 HTTPS。登入 session 最長有效 12 小時。</small>
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

            <section className="panel-shell" id="deployments-panel">
              <Card appearance="filled-alternative" className="panel">
              <div className="panel-heading"><div><p className="eyebrow">LOCAL COMMIT DEPLOY</p><h2>本機部署</h2>
                <p>本機 commit 會觸發一次部署；失敗時保留上一個正常版本，不自動重試。</p></div>
                {deployments[0] && <StatusBadge status={deployments[0].status} />}
              </div>
              {deployments.length ? <div className="run-list">{deployments.map((deployment) => <article className="run-row" key={deployment.sha}>
                <div className="run-title"><code>{deployment.sha.slice(0, 12)}</code><StatusBadge status={deployment.status} /></div>
                <p>{deployment.subject || '本機 commit'} · {formatDate(deployment.queued_at)}</p>
                <p>階段：{deploymentStageLabel(deployment.stage)}{deployment.started_at ? ` · 開始於 ${formatDate(deployment.started_at)}` : ''}</p>
                {deployment.finished_at && <p>結束於 {formatDate(deployment.finished_at)}</p>}
                {deployment.error && <p className="run-error">{deployment.error}</p>}
              </article>)}</div> : <p className="empty-state">尚無本機部署紀錄。下一次 commit 後會自動出現在這裡。</p>}
              {hasActiveDeployment && <MessageBar intent="info"><MessageBarBody role="status" aria-live="polite">部署進行中，狀態每 4 秒更新。</MessageBarBody></MessageBar>}
              </Card>
            </section>

            <section className="panel-shell" id="agent-assets">
              <Card appearance="filled-alternative" className="panel">
                <div className="panel-heading">
                  <div><p className="eyebrow">SKILLS & MCP</p><h2>Skills 與 MCP 更新狀態</h2>
                    <p>手動比對已安裝內容與來源 repo 的 default branch。MCP 連線由 host 管理，此檢查不探測 MCP session。</p>
                  </div>
                  <Button appearance="secondary" type="button" onClick={() => void checkAgentAssets()} disabled={agentAssetsLoading}>
                    {agentAssetsLoading ? '檢查中…' : '手動檢查更新'}
                  </Button>
                </div>
                {agentAssetsError && <p className="error-text" role="alert">{agentAssetsError}</p>}
                {agentAssets && <>
                  <p className="component-detail" role="status">
                    {agentAssets.repository} · {agentAssets.source_ref} · 檢查於 {formatDate(agentAssets.checked_at)}
                  </p>
                  <p className="component-detail">
                    Skills {agentAssets.summary.skill_updates_available} 項有更新，{agentAssets.summary.skills_available} 項尚未安裝；
                    MCP {agentAssets.summary.mcp_updates_available} 項有更新，{agentAssets.summary.mcp_enabled} 項已啟用。
                  </p>
                  <div className="component-grid">
                    {agentAssets.skills.map((skill) => <article className="component-item" key={`skill:${skill.id}`}>
                      <Card appearance="filled" className="component-card">
                        <div className="card-heading"><StatusBadge status={skill.status} /><span className="component-kind">Skill</span></div>
                        <h3>{skill.id}</h3>
                        <p className="component-version">{skill.installed ? '已安裝' : '可從來源取得'} · {skill.file_count} 個檔案</p>
                        {!!skill.changed_files && <p className="component-detail">{skill.changed_files} 個檔案與來源不同</p>}
                      </Card>
                    </article>)}
                    {agentAssets.mcp_servers.map((provider) => <article className="component-item" key={`mcp:${provider.id}`}>
                      <Card appearance="filled" className="component-card">
                        <div className="card-heading"><StatusBadge status={provider.update_status} /><span className="component-kind">MCP · {provider.transport}</span></div>
                        <h3>{provider.label}</h3>
                        <p className="component-version">
                          {provider.enabled ? `已啟用 · ${provider.installed_version || '版本未知'}` : '未啟用'}
                          {provider.latest_version ? ` · 來源版本 ${provider.latest_version}` : ''}
                        </p>
                        <p className="component-detail">MCP session：{statusLabel(provider.connection_status)}</p>
                      </Card>
                    </article>)}
                  </div>
                  {!agentAssets.skills.length && !agentAssets.mcp_servers.length && <p className="empty-state">來源目前沒有可盤點的 Skills 或 MCP provider。</p>}
                </>}
                {!agentAssets && !agentAssetsLoading && !agentAssetsError && <p className="empty-state">按「手動檢查更新」盤點 Skills 與 MCP。檢查只讀取本機 ADE generation 與 GitHub，不會套用更新。</p>}
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

            {session.role === 'admin' && <section className="panel-shell" id="backup-panel">
              <Card appearance="filled-alternative" className="panel">
                <div className="panel-heading"><div><p className="eyebrow">WEBDAV BACKUP</p><h2>環境設定備份</h2>
                  <p>匯出 ADES manifest、帳號雜湊與更新政策，使用備份密碼加密後上傳。</p></div></div>
                <p className="backup-note">備份綁定目前 VM，只能還原到相同 VM。登入 session、TLS 私鑰與 WebDAV 連線憑證不會寫入備份。匯入會驗證設定並清除舊 session。</p>
                <form className="webdav-form" onSubmit={(event) => void backupToWebDAV(event)}>
                  <Field label="WebDAV 備份檔案 URL" required size="small">
                    <Input name="webdav_url" type="url" autoComplete="url" placeholder="https://dav.example.com/ades-backup.json"
                      value={webdavUrl} onChange={(_event, data) => setWebdavUrl(data.value)} required />
                  </Field>
                  <Field label="WebDAV 使用者名稱" size="small">
                    <Input name="webdav_username" type="text" autoComplete="off" autoCapitalize="none"
                      value={webdavUsername} onChange={(_event, data) => setWebdavUsername(data.value)} />
                  </Field>
                  <Field label="WebDAV 密碼" size="small">
                    <Input name="webdav_password" type="password" autoComplete="off"
                      value={webdavPassword} onChange={(_event, data) => setWebdavPassword(data.value)} />
                  </Field>
                  <Field label="備份加密密碼，至少 12 個字元" required size="small">
                    <Input name="backup_passphrase" type="password" autoComplete="off" minLength={12}
                      value={backupPassphrase} onChange={(_event, data) => setBackupPassphrase(data.value)} required />
                  </Field>
                  <Button appearance="primary" type="submit" disabled={backupBusy || !webdavUrl || backupPassphrase.length < 12}>
                    {backupBusy ? '備份中…' : '加密並上傳'}
                  </Button>
                </form>
                {backupMessage && <p className="backup-message" role="status" aria-live="polite">{backupMessage}</p>}
              </Card>
            </section>}

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

if (import.meta.env.PROD && 'serviceWorker' in navigator) {
  void navigator.serviceWorker.register('/sw.js').catch((error: unknown) => {
    console.error('PWA service worker registration failed:', error);
  });
}

createRoot(document.getElementById('root')!).render(
  <FluentProvider theme={adeTheme} className="fluent-root">
    <React.StrictMode><App /></React.StrictMode>
  </FluentProvider>,
);
