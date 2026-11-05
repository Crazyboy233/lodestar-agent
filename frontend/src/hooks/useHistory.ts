import { useCallback, useEffect, useState } from 'react';
import type { HistoryDetail, HistoryGroups, LogEvent } from '../types';
import {
  deleteHistory, fetchHistory, fetchHistoryDetail, fetchHistoryLog, reportClient,
} from '../api';

const EMPTY_HISTORY: HistoryGroups = { ok: true, total: 0, projects: [] };
const POLL_FIRST_MS = 1200;
const POLL_EVERY_MS = 6000;

const errText = (err: unknown) => (err instanceof Error ? err.message : String(err));

/**
 * 左侧导航的数据（按目录分组的历史）与「打开某条会话」的详情。
 *
 * polling=true 时定时刷新列表：新会话是后台线程写 history.json 的，
 * 开始那一刻可能还没落盘，不补这一次左侧要等任务跑完才冒出「运行中」。
 */
export function useHistory({ polling }: { polling: boolean }) {
  const [groups, setGroups] = useState<HistoryGroups>(EMPTY_HISTORY);
  const [loading, setLoading] = useState(true);
  const [detail, setDetail] = useState<HistoryDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailError, setDetailError] = useState('');
  /** 回看用的事件流：null = 还没取过这个会话的日志 */
  const [stream, setStream] = useState<LogEvent[] | null>(null);
  const [streamLoading, setStreamLoading] = useState(false);
  const [streamError, setStreamError] = useState('');
  const [streamNote, setStreamNote] = useState('');

  const clearStream = useCallback(() => {
    setStream(null);
    setStreamLoading(false);
    setStreamError('');
    setStreamNote('');
  }, []);

  const refresh = useCallback(async () => {
    try {
      setGroups(await fetchHistory());
    } catch (err) {
      reportClient('读取历史失败：' + errText(err));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { void refresh(); }, [refresh]);

  useEffect(() => {
    if (!polling) return;
    const first = window.setTimeout(() => void refresh(), POLL_FIRST_MS);
    const timer = window.setInterval(() => void refresh(), POLL_EVERY_MS);
    return () => {
      window.clearTimeout(first);
      window.clearInterval(timer);
    };
  }, [polling, refresh]);

  const load = useCallback(async (thread: string) => {
    setDetail(null);
    setDetailError('');
    setDetailLoading(true);
    clearStream();
    try {
      const res = await fetchHistoryDetail(thread);
      if (!res.ok || !res.item) throw new Error(res.error || '读取失败');
      setDetail(res.item);
    } catch (err) {
      setDetailError('读取会话失败：' + errText(err));
    } finally {
      setDetailLoading(false);
    }
  }, [clearStream]);

  /** 回看历史记录的操作页面：把当时那份运行日志取成事件流 */
  const loadStream = useCallback(async (thread: string) => {
    setStream(null);
    setStreamError('');
    setStreamNote('');
    setStreamLoading(true);
    try {
      const res = await fetchHistoryLog(thread);
      if (!res.ok) throw new Error(res.error || '读取失败');
      setStream(res.events);
      setStreamNote(res.note ?? '');
    } catch (err) {
      setStreamError('读取信息流失败：' + errText(err));
    } finally {
      setStreamLoading(false);
    }
  }, []);

  /** 关掉详情就没什么可回看的了，信息流跟着一起丢掉 */
  const clearDetail = useCallback(() => {
    setDetail(null);
    setDetailLoading(false);
    setDetailError('');
    clearStream();
  }, [clearStream]);

  /** 删一条历史；成功返回 null，失败返回给页面看的错误文案 */
  const remove = useCallback(async (thread: string): Promise<string | null> => {
    const res = await deleteHistory(thread);
    if (!res.ok) return res.error || '删除失败';
    await refresh();
    return null;
  }, [refresh]);

  return {
    groups, loading, detail, detailLoading, detailError,
    stream, streamLoading, streamError, streamNote,
    refresh, load, loadStream, clearStream, clearDetail, remove,
  };
}
