import { useEffect, useMemo, useRef, useState } from 'react';
import type { LogEvent, LogKind } from '../types';
import { clock } from '../ui';

const TAG: Record<LogKind, string> = {
  system: 'sys',
  plan: 'plan',
  worker: 'work',
  verifier: 'verify',
  synth: 'final',
  stop: 'stop',
  note: 'note',
};

const isLong = (e: LogEvent) =>
  e.text.length > 360 || e.text.split('\n').length > 8;

interface Props {
  events: LogEvent[];
}

export function Stream({ events }: Props) {
  const boxRef = useRef<HTMLDivElement>(null);
  const [stuck, setStuck] = useState(true);
  const [clearedAt, setClearedAt] = useState(0);
  const [expanded, setExpanded] = useState<Set<number>>(() => new Set());

  const visible = useMemo(
    () => events.filter((e) => e.seq > clearedAt),
    [events, clearedAt],
  );

  useEffect(() => {
    const el = boxRef.current;
    if (el && stuck) el.scrollTop = el.scrollHeight;
  }, [visible.length, stuck]);

  const onScroll = () => {
    const el = boxRef.current;
    if (!el) return;
    setStuck(el.scrollHeight - el.scrollTop - el.clientHeight < 80);
  };

  const toggle = (seq: number) => {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(seq)) next.delete(seq);
      else next.add(seq);
      return next;
    });
  };

  return (
    <section className="stream-wrap">
      <div className="stream-head">
        <span>事件流</span>
        <span className="count">{visible.length} 条</span>
        <span className="spacer" />
        <button
          className="ghost"
          onClick={() => setClearedAt(events.length ? events[events.length - 1].seq : 0)}
        >
          清屏
        </button>
      </div>

      <div id="stream" ref={boxRef} onScroll={onScroll}>
        {visible.map((e) => {
          const long = isLong(e);
          const open = expanded.has(e.seq);
          const flags = [
            e.hr ? 'hr' : '',
            e.kind === 'verifier'
              ? (/→\s*PASS/.test(e.text) ? 'pass' : /→\s*FAIL/.test(e.text) ? 'fail' : '')
              : '',
            long ? 'clipped' : '',
            long && open ? 'expanded' : '',
          ].filter(Boolean).join(' ');

          return (
            <div className={`ev ${flags}`.trim()} data-kind={e.kind} key={e.seq}>
              <span className="ts">{clock(e.ts)}</span>
              <span className="tag">{TAG[e.kind] ?? e.kind}</span>
              <div className="body" onClick={long ? () => toggle(e.seq) : undefined}>
                {e.text}
              </div>
            </div>
          );
        })}
      </div>

      <button
        id="jump"
        className={stuck ? '' : 'on'}
        onClick={() => {
          setStuck(true);
          const el = boxRef.current;
          if (el) el.scrollTop = el.scrollHeight;
        }}
      >
        回到底部
      </button>
    </section>
  );
}
