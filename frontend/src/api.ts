import type {
  ConfigView, HistoryDetail, HistoryGroups, Session, StateSnapshot,
} from './types';

async function json<T>(res: Response): Promise<T> {
  const text = await res.text();
  try {
    return JSON.parse(text) as T;
  } catch {
    throw new Error(`响应不是 JSON（HTTP ${res.status}）`);
  }
}

export async function fetchState(): Promise<StateSnapshot> {
  const res = await fetch('/state');
  if (!res.ok) throw new Error(`GET /state ${res.status}`);
  return json<StateSnapshot>(res);
}

export interface ConfigResponse {
  ok: boolean;
  config?: ConfigView;
  error?: string;
  errors?: string[];
  note?: string;
}

export async function fetchConfig(): Promise<ConfigResponse> {
  const res = await fetch('/config');
  return json<ConfigResponse>(res);
}

/** 左侧导航：按产出目录分组的历史会话 */
export async function fetchHistory(): Promise<HistoryGroups> {
  const res = await fetch('/history');
  if (!res.ok) return { ok: false, total: 0, projects: [], error: `GET /history ${res.status}` };
  return json<HistoryGroups>(res);
}

export interface HistoryDetailResponse {
  ok: boolean;
  item?: HistoryDetail;
  error?: string;
}

export async function fetchHistoryDetail(thread: string): Promise<HistoryDetailResponse> {
  const res = await fetch(`/history/${encodeURIComponent(thread)}`);
  return json<HistoryDetailResponse>(res);
}

export interface CommandResponse {
  ok: boolean;
  session?: Session;
  error?: string;
  errors?: string[];
  /** pickdir 用：选中的绝对路径；canceled 为 true 表示用户点了取消 */
  path?: string;
  canceled?: boolean;
}

export interface SaveConfigResponse {
  ok: boolean;
  config?: ConfigView;
  error?: string;
  errors?: string[];
  note?: string;
}

export async function command(payload: Record<string, unknown>): Promise<CommandResponse> {
  const res = await fetch('/command', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  return json<CommandResponse>(res);
}

/** 让后端在本机弹出系统文件夹选择器（macOS / Windows / Linux） */
export async function pickDirectory(): Promise<CommandResponse> {
  const res = await fetch('/command', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ type: 'pickdir' }),
  });
  return json<CommandResponse>(res);
}

export async function saveConfig(payload: unknown): Promise<SaveConfigResponse> {
  const res = await fetch('/command', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ type: 'config', payload }),
  });
  return json<SaveConfigResponse>(res);
}

/** 页面上的异常打到服务端终端：排查「点了没反应」时不用开 DevTools */
export function reportClient(text: string): void {
  void fetch('/command', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ type: 'clientlog', message: String(text).slice(0, 400) }),
  }).catch(() => undefined);
}
