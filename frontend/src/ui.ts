import type { NodeName, RunMeta, RunState, SessionState, TaskStatus } from './types';

export const STATE_TEXT: Record<RunState | SessionState, string> = {
  idle: '待开始',
  starting: '启动中',
  running: '运行中',
  busy: '运行中',
  draining: '等待收尾',
  done: '已完成',
  aborted: '已退出',
  failed: '已失败',
  error: '出错了',
};

export const STATE_CLASS: Record<string, string> = {
  starting: '',
  running: 'running',
  busy: 'running',
  draining: 'draining',
  done: 'done',
  aborted: 'aborted',
  failed: 'failed',
  error: 'failed',
};

export const NODE_LABEL: Record<NodeName, string> = {
  plan: '主 agent',
  worker: 'worker 节点',
  verifier: '验收节点',
  synth: '汇总',
};

export const TASK_TEXT: Record<TaskStatus, string> = {
  pending: '待执行',
  running: '执行中',
  pass: '通过',
  fail: '未通过',
  skip: '未验收',
};

export function clock(tsSeconds: number): string {
  const d = new Date(tsSeconds * 1000);
  return d.toTimeString().slice(0, 8);
}

export function fmtDuration(secs: number): string {
  const s = Math.max(0, Math.floor(secs));
  const m = Math.floor(s / 60);
  return `${String(m).padStart(2, '0')}:${String(s % 60).padStart(2, '0')}`;
}

export const isFinished = (s: SessionState): boolean =>
  s === 'done' || s === 'failed' || s === 'aborted' || s === 'error';

/** 历史列表里的相对时间：今天只给时分，更早给日期 */
export function timeAgo(tsSeconds: number): string {
  if (!tsSeconds) return '';
  const then = new Date(tsSeconds * 1000);
  const now = new Date();
  const sameDay = then.toDateString() === now.toDateString();
  const hm = `${String(then.getHours()).padStart(2, '0')}:${String(then.getMinutes()).padStart(2, '0')}`;
  if (sameDay) return hm;
  const days = Math.round((now.getTime() - then.getTime()) / 86400000);
  if (days === 1) return `昨天 ${hm}`;
  if (days < 7) return `${days} 天前`;
  return `${then.getMonth() + 1}/${then.getDate()}`;
}

export function fmtFullTime(tsSeconds: number): string {
  if (!tsSeconds) return '—';
  const d = new Date(tsSeconds * 1000);
  const p = (n: number) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

/** 模型下拉的候选：空串 = 「CLI 决定」，永远排第一；配置里的列表 + 当前生效值都放进来 */
export function modelOptions(meta: RunMeta): string[] {
  const base = [''];
  for (const m of meta.models ?? []) {
    if (m && !base.includes(m)) base.push(m);
  }
  for (const v of [meta.workerModel, meta.verifierModel]) {
    if (v && !base.includes(v)) base.push(v);
  }
  return base;
}
