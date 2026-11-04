import type { TaskItem, TaskSummary } from '../types';
import { TASK_TEXT } from '../ui';

interface Props {
  tasks: TaskItem[];
  summary: TaskSummary;
}

export function TaskPanel({ tasks, summary }: Props) {
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

          return (
            <li key={`${i}-${t.task}`} data-st={t.status}>
              <span className="tnum">{i + 1}</span>
              <div className="tbody">
                <div className="ttask" title={t.task}>{t.task}</div>
                <div className="tmeta">{meta.join(' · ') || '—'}</div>
              </div>
              <span className="tst">{TASK_TEXT[t.status]}</span>
            </li>
          );
        })}
      </ul>
    </div>
  );
}
