import { useState } from 'react';
import type { TaskItem } from '../types';
import { TASK_TEXT } from '../ui';

interface Props {
  tasks: TaskItem[];
  /** 侧栏窄卡片：两行截断 + 点开看全文与验收；历史页给足宽度，直接铺开 */
  compact?: boolean;
  empty?: string;
}

function metaText(t: TaskItem, compact: boolean): string {
  const parts: string[] = [];
  if (t.attempts) parts.push(`第 ${t.attempts} 轮`);
  else parts.push('未执行');
  if (t.duration) parts.push(`${t.duration}s`);
  if (t.reason && (t.status === 'fail' || !compact)) {
    parts.push(compact ? t.reason.slice(0, 40) : t.reason);
  }
  return parts.join(' · ');
}

/**
 * 任务清单。侧栏和历史页共用同一份渲染，避免两套 DOM 结构抢同一套 `.tasks` 样式。
 */
export function TaskList({ tasks, compact = false, empty = '还没有任务' }: Props) {
  const [open, setOpen] = useState<Set<number>>(() => new Set());

  const toggle = (i: number, el: HTMLElement) => {
    const willOpen = !open.has(i);
    setOpen((prev) => {
      const next = new Set(prev);
      if (next.has(i)) next.delete(i);
      else next.add(i);
      return next;
    });
    // 展开后卡片变高，可能顶到列表可视区外面；把它滚进视野，别让用户以为没反应
    if (willOpen) {
      requestAnimationFrame(() => el.scrollIntoView({ block: 'nearest' }));
    }
  };

  return (
    <ul className={`tasks${compact ? ' tasks--compact' : ''}`}>
      {tasks.length === 0 && <li className="empty">{empty}</li>}
      {tasks.map((t, i) => {
        const status = <span className="tst">{TASK_TEXT[t.status]}</span>;

        if (!compact) {
          return (
            <li key={`${i}-${t.task}`} data-st={t.status}>
              <span className="tnum">{i + 1}</span>
              <span className="tbody">
                <span className="ttask">{t.task || '(空任务)'}</span>
                <span className="tmeta">{metaText(t, false)}</span>
              </span>
              {status}
            </li>
          );
        }

        const isOpen = open.has(i);
        return (
          <li key={`${i}-${t.task}`} data-st={t.status} className={isOpen ? 'open' : ''}>
            <button
              type="button"
              className="trow"
              onClick={(e) => toggle(i, e.currentTarget)}
              aria-expanded={isOpen}
            >
              <span className="tnum">{i + 1}</span>
              <div className="tbody">
                <div className="ttask">{t.task}</div>
                <div className="tmeta">
                  <span className="tmetatxt">{metaText(t, true)}</span>
                  <span className="tcaret" aria-hidden="true">{isOpen ? '▴' : '▾'}</span>
                </div>
                {isOpen && (
                  <div className="tdetail">
                    {t.verify
                      ? <><b>验收</b>{t.verify}</>
                      : <span className="tnone">没有单独写验收标准，由验收 agent 自行设计</span>}
                  </div>
                )}
              </div>
              {status}
            </button>
          </li>
        );
      })}
    </ul>
  );
}
