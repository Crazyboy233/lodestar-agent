import type {
  ConfigView, HistoryDetail, HistoryGroups, HistoryLog, Session, StateSnapshot,
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

/**
 * 一次运行的信息流（回看历史记录的操作页面用）。后端把 logs/run_*.log 切成了事件；
 * 老记录没有日志文件时返回空 events + note，不算错误。
 */
export async function fetchHistoryLog(thread: string): Promise<HistoryLog> {
  try {
    const res = await fetch(`/history/${encodeURIComponent(thread)}/log`);
    return await json<HistoryLog>(res);
  } catch (err) {
    return {
      ok: false, events: [],
      error: err instanceof Error ? err.message : String(err),
    };
  }
}

/**
 * 删除一条会话历史。后端会连同它的 checkpoint 一起删掉，之后不能再续跑。
 * 409 = 这条正在跑，404 = 已经被删过了；两种都带 JSON 错误文案，原样透出去。
 */
export async function deleteHistory(thread: string): Promise<{ ok: boolean; error?: string }> {
  try {
    const res = await fetch(`/history/${encodeURIComponent(thread)}`, { method: 'DELETE' });
    return await json<{ ok: boolean; error?: string }>(res);
  } catch (err) {
    return { ok: false, error: err instanceof Error ? err.message : String(err) };
  }
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

/** 一次 run 的前端入参：字段缺省 = 用后端的默认语义（别拿空串代替「不传」） */
export interface RunStart {
  goal: string;
  thread?: string;
  resume?: boolean;
  /** 缺省=沿用配置；''=显式用 CLI 默认模型 */
  workerModel?: string;
  verifierModel?: string;
  /** 缺省 / 空串 = 用配置里的默认产出目录 */
  workdir?: string;
  /** 开跑前写好的引导，跟着 run 一起投递 */
  seedNotes?: { target: 'main' | 'worker'; text: string }[];
}

export async function command(payload: Record<string, unknown>): Promise<CommandResponse> {
  const res = await fetch('/command', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  return json<CommandResponse>(res);
}

/** 开始一次运行（新任务 / 续跑共用）：snake_case 的协议细节只在这一处出现 */
export function runCommand(start: RunStart): Promise<CommandResponse> {
  const payload: Record<string, unknown> = { type: 'run', goal: start.goal };
  if (start.thread !== undefined) payload.thread = start.thread;
  if (start.resume !== undefined) payload.resume = start.resume;
  if (start.workerModel !== undefined) payload.worker_model = start.workerModel;
  if (start.verifierModel !== undefined) payload.verifier_model = start.verifierModel;
  if (start.workdir) payload.workdir = start.workdir;
  if (start.seedNotes?.length) payload.seed_notes = start.seedNotes;
  return command(payload);
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
