import { useCallback, useState } from 'react';
import type {
  LogEvent, NoteItem, ServerEvent, Session, StateSnapshot, Status, TaskItem, TaskSummary,
} from '../types';
import { EMPTY_SESSION, EMPTY_STATUS } from '../types';
import { command } from '../api';
import { useStream } from './useStream';

const EMPTY_SUMMARY: TaskSummary = { total: 0, done: 0, passed: 0, failed: 0 };
/** 事件流最多留这么多条，再多就把最早的丢掉 */
const MAX_EVENTS = 2500;

/**
 * 一次运行的可视状态 + SSE 归约。
 *
 * 只负责「把这轮运行的状态放好」：开跑成功就把 session 换掉，什么时候切视图、
 * 要不要刷新历史，由调用方看 session.state 的迁移自己决定（见 App 里的 effect）。
 */
export function useRunSession() {
  const [session, setSession] = useState<Session>(EMPTY_SESSION);
  const [status, setStatus] = useState<Status>(EMPTY_STATUS);
  const [events, setEvents] = useState<LogEvent[]>([]);
  const [tasks, setTasks] = useState<TaskItem[]>([]);
  const [summary, setSummary] = useState<TaskSummary>(EMPTY_SUMMARY);
  const [notes, setNotes] = useState<NoteItem[]>([]);
  const [elapsed, setElapsed] = useState({ base: 0, at: Date.now() });
  const [drainStart, setDrainStart] = useState<number | null>(null);

  const onEvent = useCallback((ev: ServerEvent) => {
    switch (ev.type) {
      case 'log':
        setEvents((prev) => {
          const next = [...prev, {
            seq: ev.seq, ts: ev.ts, kind: ev.kind, text: ev.text,
            hr: ev.meta?.hr, role: ev.meta?.role,
          }];
          return next.length > MAX_EVENTS ? next.slice(next.length - MAX_EVENTS) : next;
        });
        break;
      case 'status':
        setStatus(ev as unknown as Status);
        setElapsed({ base: ev.elapsed, at: Date.now() });
        break;
      case 'session':
        setSession((prev) => ({
          ...prev,
          state: ev.state,
          goal: ev.goal ?? prev.goal,
          thread: ev.thread ?? prev.thread,
          code: ev.code ?? prev.code,
          error: ev.error ?? prev.error,
          workdir: ev.workdir ?? prev.workdir,
        }));
        break;
      case 'tasks':
        setTasks(ev.tasks);
        setSummary(ev.summary);
        break;
      case 'note':
        setNotes((prev) => [...prev.filter((n) => n.id !== ev.note.id), ev.note]);
        break;
      case 'note_consumed':
        setNotes((prev) => prev.map((n) => (
          ev.ids.includes(n.id) ? { ...n, consumed: true } : n
        )));
        break;
      case 'reset':
        // 旧事件已经由 useStream 按 seq 过滤掉，这里只清界面状态
        setEvents([]);
        setTasks([]);
        setNotes([]);
        setSummary(EMPTY_SUMMARY);
        setDrainStart(null);
        break;
      case 'stop':
        setDrainStart(Date.now());
        break;
      default:
        break;
    }
  }, []);

  /** SSE 从哪开始订阅；null = 还没定（等 /state 的快照） */
  const [streamSince, setStreamSince] = useState<number | null>(null);

  const { connected } = useStream(onEvent, streamSince);

  /**
   * 初次加载 / 刷新页面：用 GET /state 的快照铺底。
   *
   * 只恢复「还在跑」的那一轮。已经结束的会话属于左侧历史，刷新后应该落在新任务页，
   * 而不是把上一次的结果当成当前界面——所以快照说结束了，就按空会话处理，
   * 并把 SSE 起点推到快照的 seq 上，免得 backlog 里的 `session: done` 把界面又点亮。
   * 传 null = 快照没取到，退回「不恢复、从头订阅」的旧行为。
   */
  const applySnapshot = useCallback((snap: StateSnapshot | null) => {
    if (snap && snap.session.state === 'running') {
      setSession(snap.session);
      setStatus(snap as unknown as Status);
      setElapsed({ base: snap.elapsed ?? 0, at: Date.now() });
      if (snap.stop_requested) {
        setDrainStart(Date.now() - (snap.waited ?? 0) * 1000);
      }
      setStreamSince(0);
      return;
    }
    setSession(EMPTY_SESSION);
    setStatus(EMPTY_STATUS);
    setElapsed({ base: 0, at: Date.now() });
    setDrainStart(null);
    setStreamSince(snap?.seq ?? 0);
  }, []);

  /** 启动成功后，直接用命令返回的 session（不用等 SSE 那一帧） */
  const adopt = useCallback((next: Session) => setSession(next), []);

  const requestStop = useCallback(async (): Promise<string | null> => {
    const res = await command({ type: 'stop' });
    if (!res.ok) return res.error ?? '请求失败';
    setDrainStart(Date.now());
    return null;
  }, []);

  return {
    session, status, events, tasks, summary, notes, elapsed, drainStart, connected,
    applySnapshot, adopt, requestStop,
  };
}
