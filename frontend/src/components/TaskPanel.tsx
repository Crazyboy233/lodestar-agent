import type { TaskItem, TaskSummary } from '../types';
import { TaskList } from './TaskList';

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
      <TaskList tasks={tasks} compact />
    </div>
  );
}
