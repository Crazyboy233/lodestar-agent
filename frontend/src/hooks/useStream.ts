import { useCallback, useEffect, useRef, useState } from 'react';
import type { ServerEvent } from '../types';

/**
 * SSE 连接。事件带单调 seq，断线重连会拿到 backlog，这里按 seq 去重。
 * reset 事件代表「新一轮开始」，调用方要把 seq 基准推过去，否则上一轮排队的事件会被重放。
 */
export function useStream(onEvent: (ev: ServerEvent) => void) {
  const [connected, setConnected] = useState(false);
  const lastSeq = useRef(0);
  const handler = useRef(onEvent);
  handler.current = onEvent;

  useEffect(() => {
    const es = new EventSource('/events?since=0');
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
  }, []);

  const bumpSeq = useCallback((seq: number) => {
    lastSeq.current = Math.max(lastSeq.current, seq);
  }, []);

  return { connected, bumpSeq };
}
