import type { SessionState, Status } from '../types';
import { NODE_LABEL } from '../ui';
import { useNow } from '../hooks/useNow';

interface Props {
  status: Status;
  sessionState: SessionState;
}

export function Workbar({ status, sessionState }: Props) {
  const now = useNow();
  const node = status.busy;
  const draining = status.draining && node !== null;

  let taskText: string;
  let timeText = '—';
  if (node) {
    const parts: string[] = [];
    if (status.attempt) parts.push(`第 ${status.attempt} 条`);
    if (status.maxAttempts > 1) parts.push(`第 ${status.attempt || 1}/${status.maxAttempts} 轮`);
    if (status.task) parts.push(status.task);
    taskText = parts.join(' · ') || '模型调用中';
    const secs = status.busySince
      ? Math.max(0, Math.floor(now / 1000 - status.busySince))
      : 0;
    timeText = `${secs}s${draining ? ' · 等它结束' : ''}`;
  } else {
    taskText = sessionState === 'running'
      ? '等待下一个节点'
      : sessionState === 'idle'
        ? '等待接入'
        : '本轮已结束';
  }

  return (
    <div
      className={`workbar${node ? ' on' : ''}${draining ? ' draining' : ''}`}
      data-node={node ?? ''}
    >
      <span className="wnode">{node ? NODE_LABEL[node] : '空闲'}</span>
      <span className="wmodel">{node ? (status.model || status.busyDetail) : ''}</span>
      <span className="wtask" title={node ? status.task : ''}>{taskText}</span>
      <span className="wtime">{timeText}</span>
    </div>
  );
}
