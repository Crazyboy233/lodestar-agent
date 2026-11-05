import { useState } from 'react';
import { ComposerTextarea } from './ComposerTextarea';
import { GuideTargets, type NoteTarget } from './GuideTargets';
import type { CardProps } from './types';

/** 运行中：输入框只发引导，投给主 agent 或 worker */
export function GuideCard({ variant, onGuide }: CardProps<'guide'>) {
  const { locked, pending } = variant;
  const [target, setTarget] = useState<NoteTarget>('main');
  const [note, setNote] = useState('');
  const [sending, setSending] = useState(false);
  const [sent, setSent] = useState('');
  const [hint, setHint] = useState('');

  const queued = [
    pending.main ? `主 agent ${pending.main}` : '',
    pending.worker ? `worker ${pending.worker}` : '',
  ].filter(Boolean).join(' · ');

  const send = async () => {
    if (!note.trim() || sending || locked) return;
    setSending(true);
    setHint('');
    setSent('');
    const err = await onGuide(target, note.trim());
    setSending(false);
    if (err) {
      setHint(err);
      return;
    }
    setNote('');
    setSent(`已投递给${target === 'main' ? '主 agent' : 'worker'}，会在下一个节点边界生效`);
  };

  return (
    <div className="composer-card guide-card">
      <ComposerTextarea
        value={note}
        onChange={(v) => {
          setNote(v);
          setSent('');
        }}
        onSubmit={() => void send()}
        disabled={locked}
        placeholder={locked
          ? '正在收尾，已停止接受新的引导'
          : target === 'main'
            ? '给主 agent 的引导：下一个节点边界生效，不打断当前这一轮'
            : '给 worker 的引导：它下一次被调用时生效，不打断当前这一轮'}
      />
      <div className="launch-opts">
        <GuideTargets value={target} onChange={setTarget} disabled={locked} pending={pending} />
        <span className="composer-hint">
          {locked
            ? '正在等待当前这一轮结束，然后退出'
            : (queued ? `已排队 ${queued}` : '⌘ / Ctrl + ↵ 发送')}
        </span>
        <button
          className="primary"
          disabled={locked || !note.trim() || sending}
          onClick={() => void send()}
        >
          {sending ? '发送中…' : '投递引导'}
        </button>
      </div>
      {hint && <div className="launch-hint">{hint}</div>}
      {sent && <div className="launch-ok">{sent}</div>}
    </div>
  );
}

