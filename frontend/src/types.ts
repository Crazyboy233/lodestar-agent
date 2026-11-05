export type SessionState = 'idle' | 'running' | 'done' | 'failed' | 'aborted' | 'error';

export interface Session {
  goal: string;
  thread: string;
  state: SessionState;
  code: number | null;
  started: number;
  ended: number;
  error: string;
  workerModel?: string;
  verifierModel?: string;
  workdir?: string;
}

/** 左侧导航里的一条历史会话 */
export interface HistoryItem {
  thread: string;
  goal: string;
  workdir: string;
  state: SessionState | 'idle';
  code: number | null;
  started: number;
  ended: number;
  duration?: number;
  answer?: string;
  error?: string;
  workerModel?: string;
  verifierModel?: string;
  passed?: number;
  failed?: number;
  bootstrap?: boolean;
  interrupted?: boolean;
}

export interface HistoryProject {
  workdir: string;
  name: string;
  count: number;
  updated: number;
  passed: number;
  items: HistoryItem[];
}

export interface HistoryGroups {
  ok: boolean;
  total: number;
  projects: HistoryProject[];
  error?: string;
}

export interface HistoryDetail extends HistoryItem {
  tasks?: TaskItem[];
  current?: { task?: string };
  pending?: { task?: string }[];
  hasMore?: boolean;
  /** 能不能接着跑：由后端读 checkpoint 判断（边界退出可续，正常跑完不可续） */
  resumable?: boolean;
}

export type RunState = 'starting' | 'running' | 'busy' | 'draining' | 'done' | 'aborted' | 'failed';
export type NodeName = 'plan' | 'worker' | 'verifier' | 'synth';

/** 当前这一次模型调用：页面工作栏和「等谁跑完」都靠它 */
export interface Status {
  state: RunState;
  stage: string;
  detail: string;
  elapsed: number;
  busy: NodeName | null;
  busyDetail: string;
  model: string;
  task: string;
  attempt: number;
  maxAttempts: number;
  busySince: number;
  draining: boolean;
  waited: number;
  drainTimeout: number;
  pending: Record<string, number>;
}

export type LogKind = 'system' | 'plan' | 'worker' | 'verifier' | 'synth' | 'stop' | 'note';

/**
 * 这一条属于哪一类内容：
 * prompt = 模型调用真正收到的 prompt 原文；task = 从结果里摘出的任务与验收；
 * output = 节点产出；log = 旁白；guide = 用户投递的引导；
 * head = 旧事件里的章节标题（现在统一归 log）
 */
export type LogRole = 'head' | 'prompt' | 'task' | 'output' | 'log' | 'guide';

export interface LogEvent {
  seq: number;
  ts: number;
  kind: LogKind;
  text: string;
  hr?: boolean;
  role?: LogRole;
}

export type TaskStatus = 'pending' | 'running' | 'pass' | 'fail' | 'skip';

export interface TaskItem {
  task: string;
  verify: string;
  status: TaskStatus;
  attempts: number;
  duration: number;
  reason: string;
}

export interface TaskSummary {
  total: number;
  done: number;
  passed: number;
  failed: number;
}

export interface NoteItem {
  id: string;
  target: 'main' | 'worker';
  text: string;
  consumed: boolean;
}

export interface RunMeta {
  model: string;
  base_url: string;
  workdir: string;
  worker: string;
  verifier: string;
  max_rounds: number;
  drain_timeout: number;
  models: string[];
  workerModel: string;
  verifierModel: string;
}

/** GET /state 的快照：Status 的超集 */
export interface StateSnapshot extends Status {
  host: string;
  port: number;
  meta: RunMeta;
  session: Session;
  /** 当前生效的产出目录（每次运行可以不同） */
  workdir: string;
  running: boolean;
  stop_requested: boolean;
  stop_reason: string;
}

export type ServerEvent =
  | ({ type: 'log' } & LogEvent & { meta?: { hr?: boolean; role?: LogRole } })
  | ({ type: 'status' } & Status & { seq: number; ts: number })
  | {
    type: 'session'; state: SessionState; goal?: string; thread?: string;
    code?: number; error?: string; workdir?: string;
  }
  | { type: 'tasks'; tasks: TaskItem[]; summary: TaskSummary }
  | { type: 'note'; note: NoteItem }
  | { type: 'note_consumed'; ids: string[] }
  | { type: 'reset'; seq: number }
  | { type: 'stop'; reason: string; busy: NodeName | null; busyDetail: string; drainTimeout: number };

export interface ConfigView {
  main_agent: {
    base_url: string;
    api_key: string;
    api_key_set: boolean;
    model: string;
    temperature: number;
    max_retries: number;
    plan_retries: number;
  };
  worker: Record<string, string | number>;
  verifier: Record<string, string | number>;
  ui: { drain_timeout_seconds: number; models: string };
}

export const EMPTY_STATUS: Status = {
  state: 'starting',
  stage: '启动',
  detail: '',
  elapsed: 0,
  busy: null,
  busyDetail: '',
  model: '',
  task: '',
  attempt: 0,
  maxAttempts: 0,
  busySince: 0,
  draining: false,
  waited: 0,
  drainTimeout: 120,
  pending: { main: 0, worker: 0 },
};

export const EMPTY_SESSION: Session = {
  goal: '',
  thread: '',
  state: 'idle',
  code: null,
  started: 0,
  ended: 0,
  error: '',
};

export const EMPTY_META: RunMeta = {
  model: '',
  base_url: '',
  workdir: '',
  worker: '',
  verifier: '',
  max_rounds: 3,
  drain_timeout: 120,
  models: [''],
  workerModel: '',
  verifierModel: '',
};
