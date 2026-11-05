import { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import type { LogEvent, LogKind, LogRole } from '../types';
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

/** 内容类别：和 kind（哪个节点）正交，一个说「谁」，一个说「这是什么」 */
const ROLE_TEXT: Record<LogRole, string> = {
  // 章节标题也属于旁白：它是脚本打的流程说明，不是模型说的话。
  // head 保留只为兼容旧事件（那时候标题单独一类，没有标签）。
  head: '旁白',
  prompt: '派发',
  output: '输出',
  log: '旁白',
  guide: '引导',
};

const roleOf = (e: LogEvent): LogRole => {
  if (e.hr) return 'head';
  if (e.role) return e.role;
  // 老事件（后端没带 role）按 kind 兜底，别全都变成旁白
  if (e.kind === 'note') return 'guide';
  if (e.kind === 'worker' || e.kind === 'verifier' || e.kind === 'synth') return 'output';
  return 'log';
};

/**
 * 单条正文：默认最多 10 行（CSS 直接钳住），实测确实溢出时才加渐变和「展开全文」。
 *
 * 之前是先用字数/行数猜「够不够长」，猜不中的那条就被 CSS 截断又点不开，
 * 于是内容看得见截口却看不到全文。现在钳制恒定生效、是否给入口由实测决定，
 * 不会再出现「截了但展不开」。
 */
function Body({
  text, open, onToggle,
}: {
  text: string;
  open: boolean;
  onToggle: () => void;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const [over, setOver] = useState(false);

  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const check = () => {
      // 展开状态下没有钳制，量出来永远「不溢出」，所以跳过
      if (open) return;
      setOver(el.scrollHeight - el.clientHeight > 2);
    };
    check();
    // 窗口宽度变化会重新折行，行数跟着变，得重算
    const ro = new ResizeObserver(check);
    ro.observe(el);
    return () => ro.disconnect();
  }, [text, open]);

  const flags = [open ? 'open' : '', !open && over ? 'over' : ''].filter(Boolean).join(' ');

  return (
    <div className="cell">
      <div ref={ref} className={`body${flags ? ` ${flags}` : ''}`}>{text}</div>
      {(over || open) && (
        <button type="button" className="ev-more" onClick={onToggle}>
          {open ? '收起' : '展开全文'}
        </button>
      )}
    </div>
  );
}

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
          const open = expanded.has(e.seq);
          const role = roleOf(e);
          const flags = [
            e.hr ? 'hr' : '',
            e.kind === 'verifier'
              ? (/→\s*PASS/.test(e.text) ? 'pass' : /→\s*FAIL/.test(e.text) ? 'fail' : '')
              : '',
            open ? 'expanded' : '',
          ].filter(Boolean).join(' ');

          return (
            <div
              className={`ev ${flags}`.trim()}
              data-kind={e.kind}
              data-role={role}
              key={e.seq}
            >
              <span className="ts">{clock(e.ts)}</span>
              <span className="tag">{TAG[e.kind] ?? e.kind}</span>
              <span className="role" data-role={role}>{ROLE_TEXT[role]}</span>
              <Body text={e.text} open={open} onToggle={() => toggle(e.seq)} />
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
