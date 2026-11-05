import { useCallback, useEffect, useState } from 'react';
import type { HistoryDetail, HistoryGroups } from '../types';
import {
  deleteHistory, fetchHistory, fetchHistoryDetail, reportClient,
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
    try {
      const res = await fetchHistoryDetail(thread);
      if (!res.ok || !res.item) throw new Error(res.error || '读取失败');
      setDetail(res.item);
    } catch (err) {
      setDetailError('读取会话失败：' + errText(err));
    } finally {
      setDetailLoading(false);
    }
  }, []);

  const clearDetail = useCallback(() => {
    setDetail(null);
    setDetailLoading(false);
    setDetailError('');
  }, []);

  /** 删一条历史；成功返回 null，失败返回给页面看的错误文案 */
  const remove = useCallback(async (thread: string): Promise<string | null> => {
    const res = await deleteHistory(thread);
    if (!res.ok) return res.error || '删除失败';
    await refresh();
    return null;
  }, [refresh]);

  return {
    groups, loading, detail, detailLoading, detailError,
    refresh, load, clearDetail, remove,
  };
}
