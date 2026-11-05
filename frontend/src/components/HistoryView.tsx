import type { HistoryDetail } from '../types';
import { STATE_TEXT, fmtFullTime } from '../ui';
import { TaskList } from './TaskList';

interface Props {
  detail: HistoryDetail | null;
  loading: boolean;
  error: string;
  running: boolean;
  onResume: (item: HistoryDetail) => void;
  onClose: () => void;
}

export function HistoryView({
  detail, loading, error, running, onResume, onClose,
}: Props) {
  if (loading) {
    return <section className="hist-view"><div className="hist-empty">正在读取会话…</div></section>;
  }

  if (error || !detail) {
    return (
      <section className="hist-view">
        <div className="hist-empty">
          {error || '没有选中会话'}
          <button className="ghost" onClick={onClose}>返回</button>
        </div>
      </section>
    );
  }

  const state = detail.state;
  // 以服务端判断为准：已正常跑完的会话没有可续的断点，按钮不该假装能点
  const canResume = !running && Boolean(detail.resumable);

  return (
    <section className="hist-view">
      <div className="hist-inner">
        <div className="hist-top">
          <span className={`hist-badge ${state}`}>{STATE_TEXT[state] ?? state}</span>
          <span className="hist-thread">{detail.thread}</span>
          <span className="spacer" />
          <button className="ghost" onClick={onClose}>关闭</button>
        </div>

        <h1 className="hist-goal">{detail.goal || '(没有记录目标)'}</h1>

        <div className="hist-meta">
          <span><b>产出目录</b><code>{detail.workdir || '—'}</code></span>
          <span><b>开始</b>{fmtFullTime(detail.started)}</span>
          <span><b>耗时</b>{detail.duration ? `${detail.duration}s` : '—'}</span>
          {detail.workerModel && <span><b>执行</b>{detail.workerModel}</span>}
          {detail.verifierModel && <span><b>验收</b>{detail.verifierModel}</span>}
        </div>

        {detail.error && <div className="hist-error">{detail.error}</div>}

        <div className="hist-actions">
          <button
            className="primary"
            disabled={!canResume}
            title={running
              ? '当前有任务在运行'
              : (canResume ? '回到操作界面，确认后才开始跑' : '没有可续跑的断点')}
            onClick={() => onResume(detail)}
          >
            继续这个会话
          </button>
          {canResume
            ? <span className="hist-action-tip">先回到操作界面，在下方确认后才开始跑</span>
            : !running && (
              <span className="hist-action-tip">没有可续跑的断点；可以复制目标重新开一个</span>
            )}
        </div>

        <h2 className="hist-h2">
          任务清单
          {detail.tasks && detail.tasks.length > 0 && (
            <span className="hist-count">
              {detail.tasks.filter((t) => t.status === 'pass').length}/{detail.tasks.length} 通过
            </span>
          )}
        </h2>

        {detail.tasks && detail.tasks.length > 0 ? (
          <TaskList tasks={detail.tasks} />
        ) : (
          <div className="hist-none">这个会话没有留下任务清单</div>
        )}

        {detail.answer && (
          <>
            <h2 className="hist-h2">最终答复</h2>
            <pre className="hist-answer">{detail.answer}</pre>
          </>
        )}
      </div>
    </section>
  );
}
