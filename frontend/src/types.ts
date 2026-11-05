export type SessionState = 'idle' | 'running' | 'done' | 'failed' | 'aborted' | 'error';

/** 一个节点这次运行实际用的来源与模型 */
export interface NodeInfo {
  source: string;
  model: string;
  /** 展示用：「来源 · 模型」 */
  label: string;
}

export type NodeId = 'main' | 'worker' | 'verifier';

export interface Session {
  goal: string;
  thread: string;
  state: SessionState;
  code: number | null;
  started: number;
  ended: number;
  error: string;
  /** 三个节点各自用的来源 + 模型（没开跑时为空对象） */
  nodes?: Partial<Record<NodeId, NodeInfo>>;
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

/**
 * 历史记录的信息流：后端把当时那份运行日志切成了事件。
 * ts 恒为 0（文本日志没有时间轴），页面据此不画时钟列。
 */
export interface HistoryLog {
  ok: boolean;
  events: LogEvent[];
  error?: string;
  note?: string;
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

/** 一个可选的模型来源（HTTP 接口 / 某个本机 CLI） */
export interface SourceInfo {
  name: string;
  kind: 'api' | 'cli';
  /** chat = 能当纯文本模型；agent = 会读写文件、跑命令 */
  capabilities: string[];
  /** 下拉候选；空数组表示「型号自己填」 */
  models: string[];
  /** 接口地址或命令名，给人看的 */
  display: string;
}

export interface RunMeta {
  nodes: Record<NodeId, NodeInfo>;
  sources: SourceInfo[];
  workdir: string;
  max_rounds: number;
  drain_timeout: number;
}

/** GET /state 的快照：Status 的超集 */
export interface StateSnapshot extends Status {
  meta: RunMeta;
  session: Session;
  /** 当前生效的产出目录（每次运行可以不同） */
  workdir: string;
  running: boolean;
  stop_requested: boolean;
  stop_reason: string;
  /** 事件总线的最新 seq；页面用它决定 SSE 从哪开始订阅（0 = 把 backlog 全量重放） */
  seq: number;
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

/** 设置弹窗的数据源：节点、来源、控制台三段，形状与 config.yaml 对齐 */
export interface ConfigView {
  nodes: Record<NodeId, Record<string, string | number>>;
  providers: {
    name: string;
    kind: 'api' | 'cli';
    fields: Record<string, string | number | boolean>;
  }[];
  ui: {
    drain_timeout_seconds: number;
    /** 按来源分组的候选模型，每行一个 */
    models: Record<string, string>;
  };
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

const EMPTY_NODE: NodeInfo = { source: '', model: '', label: '' };

export const EMPTY_META: RunMeta = {
  nodes: { main: EMPTY_NODE, worker: EMPTY_NODE, verifier: EMPTY_NODE },
  sources: [],
  workdir: '',
  max_rounds: 3,
  drain_timeout: 120,
};
