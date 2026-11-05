import type { HistoryDetail, LogEvent } from '../types';
import { summarizeTasks } from '../ui';
import { Stream } from './Stream';
import { TaskPanel } from './TaskPanel';

interface Props {
  detail: HistoryDetail | null;
  events: LogEvent[] | null;
  loading: boolean;
  error: string;
  /** 后端说明为什么没有事件（老记录没有日志文件之类） */
  note: string;
}

/**
 * 历史记录当时的「操作页面」：左边是信息流，右边是当时的任务清单。
 *
 * 布局与运行页一致（同一个 Stream / aside），区别只有两处：事件来自日志文件、
 * 没有输入卡和退出面板 —— 回看就是回看，不在这里操作。
 */
export function ReviewView({
  detail, events, loading, error, note,
}: Props) {
  const tasks = detail?.tasks ?? [];
  const empty = loading
    ? '正在读取信息流…'
    : (error || note || '这次运行没有留下信息流');

  return (
    <>
      {events && events.length > 0 ? (
        <Stream events={events} title="信息流" readOnly />
      ) : (
        <section className="stream-wrap">
          <div className="hist-empty">{empty}</div>
        </section>
      )}
      <aside>
        <TaskPanel tasks={tasks} summary={summarizeTasks(tasks)} />
      </aside>
    </>
  );
}
