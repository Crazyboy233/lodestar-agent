import { useState } from 'react';
import type { TaskItem, TaskSummary } from '../types';
import { TASK_TEXT } from '../ui';

interface Props {
  tasks: TaskItem[];
  summary: TaskSummary;
}

export function TaskPanel({ tasks, summary }: Props) {
  // 默认只露两行，点一下就地展开看全文与验收标准。
  // 不在这里再套一层滚动条：列表本身已经能滚，卡片内再滚会形成嵌套滚动。
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

  let sum = `${summary.passed}/${summary.total} 通过`;
  if (summary.failed) sum += ` · ${summary.failed} 失败`;

  return (
    <div className="card tasks-card">
      <div className="card-head">
        <h2>任务</h2>
        <span className="tasks-sum">{tasks.length ? sum : '—'}</span>
      </div>
      <ul id="tasks" className="tasks">
        {tasks.length === 0 && <li className="empty">还没有任务</li>}
        {tasks.map((t, i) => {
          const meta: string[] = [];
          if (t.attempts) meta.push(`第 ${t.attempts} 轮`);
          if (t.duration) meta.push(`${t.duration}s`);
          if (t.status === 'fail' && t.reason) meta.push(t.reason.slice(0, 40));

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
                    <span className="tmetatxt">{meta.join(' · ') || '—'}</span>
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
                <span className="tst">{TASK_TEXT[t.status]}</span>
              </button>
            </li>
          );
        })}
      </ul>
    </div>
  );
}
