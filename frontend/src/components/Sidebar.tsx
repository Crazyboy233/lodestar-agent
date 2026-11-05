import { useEffect, useState } from 'react';
import type { HistoryProject } from '../types';
import { STATE_TEXT, timeAgo } from '../ui';

interface Props {
  projects: HistoryProject[];
  total: number;
  loading: boolean;
  /** 读取历史失败时的原因；没有记录和读不出记录要分开显示 */
  error?: string;
  selected: string | null;
  currentThread: string | null;
  /** 有任务在跑：一次只能跑一个，新任务按钮先禁用 */
  busy: boolean;
  connected: boolean;
  onNew: () => void;
  onOpenSettings: () => void;
  onSelect: (thread: string) => void;
}

function IconPlus() {
  return (
    <svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true">
      <path d="M8 3v10M3 8h10" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" />
    </svg>
  );
}

function IconGear() {
  return (
    <svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true">
      <circle cx="8" cy="8" r="2.4" stroke="currentColor" strokeWidth="1.4" fill="none" />
      <path
        d="M8 1.6v1.8M8 12.6v1.8M1.6 8h1.8M12.6 8h1.8M3.5 3.5l1.3 1.3M11.2 11.2l1.3 1.3M12.5 3.5l-1.3 1.3M4.8 11.2l-1.3 1.3"
        stroke="currentColor" strokeWidth="1.4" strokeLinecap="round"
      />
    </svg>
  );
}

const COLLAPSE_KEY = 'lodestar.collapsedProjects';

export function Sidebar({
  projects, total, loading, error, selected, currentThread, busy, connected,
  onNew, onOpenSettings, onSelect,
}: Props) {
  // 折叠状态记在本地：目录多了之后，用户不希望每次刷新都重新收一遍
  const [collapsed, setCollapsed] = useState<Set<string>>(() => {
    try {
      const raw = localStorage.getItem(COLLAPSE_KEY);
      return new Set<string>(raw ? JSON.parse(raw) as string[] : []);
    } catch {
      return new Set<string>();
    }
  });

  useEffect(() => {
    try {
      localStorage.setItem(COLLAPSE_KEY, JSON.stringify([...collapsed]));
    } catch {
      /* 隐私模式下写不进去就算了 */
    }
  }, [collapsed]);

  const toggle = (workdir: string) => {
    setCollapsed((prev) => {
      const next = new Set(prev);
      if (next.has(workdir)) next.delete(workdir);
      else next.add(workdir);
      return next;
    });
  };

  return (
    <nav className="sidebar" aria-label="导航">
      <div className="side-brand">
        <span className="side-mark" />
        Lodestar
      </div>

      <div className="side-actions">
        <button
          className="side-new"
          onClick={onNew}
          disabled={busy}
          title={busy ? '任务运行中，等它结束再开新任务' : '新建任务'}
        >
          <IconPlus />
          新任务
        </button>
        <button className="side-link" onClick={onOpenSettings}>
          <IconGear />
          设置
        </button>
      </div>

      <div className="side-scroll">
        <div className="side-label">
          任务历史
          {total > 0 && <span className="side-total">{total}</span>}
        </div>

        {loading && projects.length === 0 && (
          <div className="side-empty">读取中…</div>
        )}
        {!loading && projects.length === 0 && (
          <div className="side-empty">{error || '还没有运行记录'}</div>
        )}

        {projects.map((project) => {
          const open = !collapsed.has(project.workdir);
          return (
            <div className="proj" key={project.workdir}>
              <button
                className="proj-head"
                onClick={() => toggle(project.workdir)}
                title={project.workdir}
              >
                <span className={`caret${open ? ' open' : ''}`}>▸</span>
                <span className="proj-name">{project.name}</span>
                <span className="proj-count">{project.count}</span>
              </button>

              {open && (
                <ul className="proj-list">
                  {project.items.map((item) => {
                    const running = item.thread === currentThread;
                    const state = running ? 'running' : item.state;
                    const stateText = running ? '运行中' : (STATE_TEXT[state] ?? state);
                    const goal = item.goal || item.thread;
                    return (
                      <li key={item.thread}>
                        <button
                          className={`hi${selected === item.thread ? ' on' : ''}${running ? ' current' : ''}`}
                          onClick={() => onSelect(item.thread)}
                          title={`${goal}\n${timeAgo(item.started)} · ${stateText}\n${item.thread}`}
                        >
                          <span className="hi-dot" data-state={state} />
                          <span className="hi-goal">{goal}</span>
                          <span className="hi-meta" data-state={state}>
                            {state === 'done' ? timeAgo(item.started) : stateText}
                          </span>
                        </button>
                      </li>
                    );
                  })}
                </ul>
              )}
            </div>
          );
        })}
      </div>

      <div className="side-foot">
        <span className={`side-conn${connected ? ' live' : ''}`}>
          <span className="side-conn-dot" />
          {connected ? '已连接' : '连接中断'}
        </span>
        <span className="side-ver">本地</span>
      </div>
    </nav>
  );
}
