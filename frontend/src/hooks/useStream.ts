import { useCallback, useEffect, useRef, useState } from 'react';
import type { ServerEvent } from '../types';

/**
 * SSE 连接。事件带单调 seq，断线重连会拿到 backlog，这里按 seq 去重。
 *
 * `since` 决定从哪儿开始订阅，传 null 表示「还没定，先别连」——页面首次加载时
 * 要先看 /state 说的是哪一轮：正在跑就 since=0 把这一轮的日志补回来，已经结束就
 * 直接用快照里的 seq 跳过 backlog，否则上一轮的 `session: done` 会把界面重新点亮。
 */
export function useStream(onEvent: (ev: ServerEvent) => void, since: number | null) {
  const [connected, setConnected] = useState(false);
  const lastSeq = useRef(0);
  const handler = useRef(onEvent);
  handler.current = onEvent;

  useEffect(() => {
    if (since === null) return undefined;
    lastSeq.current = since;
    const es = new EventSource(`/events?since=${since}`);
    es.onopen = () => setConnected(true);
    es.onerror = () => setConnected(false);
    es.onmessage = (e: MessageEvent<string>) => {
      let ev: ServerEvent;
      try {
        ev = JSON.parse(e.data) as ServerEvent;
      } catch {
        return;
      }
      const seq = 'seq' in ev ? ev.seq : undefined;
      if (typeof seq === 'number') {
        if (seq <= lastSeq.current) return;
        lastSeq.current = seq;
      }
      handler.current(ev);
    };
    return () => es.close();
  }, [since]);

  const bumpSeq = useCallback((seq: number) => {
    lastSeq.current = Math.max(lastSeq.current, seq);
  }, []);

  return { connected, bumpSeq };
}
