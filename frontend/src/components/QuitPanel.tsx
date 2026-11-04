import { useEffect, useMemo, useState } from 'react';
import type { LogEvent, Session, Status } from '../types';
import { isFinished, NODE_LABEL } from '../ui';
import { useNow } from '../hooks/useNow';

interface Props {
  status: Status;
  session: Session;
  events: LogEvent[];
  drainStart: number | null;
  onStop: () => Promise<string | null>;
}

export function QuitPanel({ status, session, events, drainStart, onStop }: Props) {
  const now = useNow();
  const [armed, setArmed] = useState(false);
  const [hint, setHint] = useState('');
  const [sending, setSending] = useState(false);
  const waited = drainStart ? (now - drainStart) / 1000 : status.waited;

  useEffect(() => {
    if (!armed) return;
    const id = window.setTimeout(() => setArmed(false), 4000);
    return () => window.clearTimeout(id);
  }, [armed]);

  const steps = useMemo(() => events.filter((e) => e.kind === 'stop'), [events]);
  const draining = status.state === 'draining';
  const finished = isFinished(session.state);
  const showTimeline = draining || finished || steps.length > 0;

  const click = async () => {
    if (!armed) {
      setArmed(true);
      return;
    }
    setArmed(false);
    setSending(true);
    const err = await onStop();
    setSending(false);
    if (err) setHint(err);
  };

  return (
    <div className="card quit">
      <h2>边界退出</h2>
      <p className="hint">
        不再开始新的模型调用，等当前这一次回复跑完再收工（超过上限才强制终止）。
      </p>

      <button
        className={`primary${armed ? ' armed' : ''}`}
        onClick={() => void click()}
        disabled={draining || finished || sending}
      >
        {sending ? '正在退出…' : armed ? '再点一次确认退出' : '边界退出'}
      </button>
      {hint && <div className="launch-hint">{hint}</div>}

      <ol id="timeline" className={showTimeline ? 'on' : ''}>
        {steps.map((s, i) => (
          <li key={s.seq} className={draining && i === steps.length - 1 ? 'active' : 'done'}>
            {s.text}
          </li>
        ))}
        {draining && status.busy && (
          <li className="active">
            {`等待 ${NODE_LABEL[status.busy]}（${status.model || status.busyDetail || '模型调用中'}）跑完`
              + `（已等 ${Math.floor(waited)}s / 上限 ${status.drainTimeout}s）`}
          </li>
        )}
        {finished && steps.length === 0 && (
          <li className="done">{session.state === 'aborted' ? '已按边界退出' : '本轮已结束'}</li>
        )}
      </ol>
    </div>
  );
}
