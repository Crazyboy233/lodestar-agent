import type { HistoryDetail, RunState, Session, SessionState, Status } from '../types';
import { fmtDuration, isFinished, STATE_CLASS, STATE_TEXT } from '../ui';
import { useNow } from '../hooks/useNow';

interface Props {
  session: Session;
  status: Status;
  elapsed: { base: number; at: number };
  drainStart: number | null;
  /** 正在回看的会话；有值时顶栏跟着它走，而不是一直显示「新任务」 */
  history?: HistoryDetail | null;
  running?: boolean;
}

export function Header({
  session, status, elapsed, drainStart, history, running = false,
}: Props) {
  const now = useNow();
  const viewing = Boolean(history) && !running;
  const detail = viewing ? history : null;

  const pillState: RunState | SessionState = detail
    ? detail.state
    : (session.state === 'idle' ? 'idle' : status.state);
  const draining = !detail && status.state === 'draining';
  const frozen = detail ? true : isFinished(session.state);
  const secs = elapsed.base + (frozen ? 0 : (now - elapsed.at) / 1000);
  const waited = drainStart ? (now - drainStart) / 1000 : status.waited;

  const goal = detail ? (detail.goal || '(没有记录目标)') : session.goal;
  const sub = detail
    ? `thread ${detail.thread}${detail.workdir ? ` · ${detail.workdir}` : ''}`
    : (session.thread ? `thread ${session.thread}` : '尚未开始')
      + (session.workdir ? ` · ${session.workdir}` : '');

  return (
    <header>
      <div className="head-main">
        <div className="head-goal" title={goal || undefined}>
          {goal || (session.state === 'idle' ? '新任务' : '未命名任务')}
        </div>
        <div className="head-sub">{sub}</div>
      </div>
      <span className="spacer" />
      <span
        className={`pill ${STATE_CLASS[pillState] ?? ''}`}
        title={detail ? (detail.error || undefined) : status.detail}
      >
        <span className="dot" />
        <span>
          {draining ? `等待收尾 ${Math.floor(waited)}s` : STATE_TEXT[pillState]}
        </span>
      </span>
      <span className="timer">
        {detail
          ? (detail.duration
            ? (detail.duration >= 60 ? fmtDuration(detail.duration) : `${detail.duration.toFixed(1)}s`)
            : '—')
          : fmtDuration(secs)}
      </span>
    </header>
  );
}
