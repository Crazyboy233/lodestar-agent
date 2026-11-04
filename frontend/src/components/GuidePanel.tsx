import { useState } from 'react';
import type { NoteItem, Status } from '../types';
import { NODE_LABEL } from '../ui';

interface Props {
  notes: NoteItem[];
  status: Status;
  disabled: boolean;
  onSend: (target: 'main' | 'worker', text: string) => Promise<string | null>;
}

export function GuidePanel({ notes, status, disabled, onSend }: Props) {
  const [target, setTarget] = useState<'main' | 'worker'>('main');
  const [text, setText] = useState('');
  const [hint, setHint] = useState('');
  const [sending, setSending] = useState(false);

  const send = async () => {
    const body = text.trim();
    if (!body) return;
    setSending(true);
    setHint('');
    const err = await onSend(target, body);
    setSending(false);
    if (err) setHint(err);
    else setText('');
  };

  const busyHint = status.busy === 'worker'
    ? 'worker 正在执行，这条引导会在它下一次被调用时生效'
    : status.busy === 'verifier'
      ? '验收正在进行，这条引导会在下一轮规划 / 执行时生效'
      : '例如：改 script.js 就行，别动 index.html';

  return (
    <div className="card guide">
      <h2>引导</h2>
      <p className="hint">
        投递后排队，不打断正在执行的那一轮，在下一个节点边界生效。
        {status.busy && ` 当前：${NODE_LABEL[status.busy]}。`}
      </p>

      <div className="tabs">
        {(['main', 'worker'] as const).map((t) => (
          <button
            key={t}
            className={target === t ? 'on' : ''}
            onClick={() => setTarget(t)}
          >
            {t === 'main' ? '主 agent' : 'worker'}
          </button>
        ))}
      </div>

      <textarea
        value={text}
        onChange={(e) => setText(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) {
            e.preventDefault();
            void send();
          }
        }}
        placeholder={busyHint}
        disabled={disabled}
      />

      <div className="send-row">
        <span className="kb">⌘ / Ctrl + ↵</span>
        <button className="primary" onClick={() => void send()} disabled={disabled || sending}>
          {sending ? '发送中…' : '发送'}
        </button>
      </div>
      {hint && <div className="launch-hint">{hint}</div>}

      <div className="sub">投递记录</div>
      <ul id="notes">
        {notes.length === 0 && <li className="empty">还没有投递</li>}
        {notes.map((n) => (
          <li key={n.id} className={n.consumed ? 'consumed' : ''}>
            <span className="tgt">{n.target === 'worker' ? 'worker' : 'main'}</span>
            <span className="txt" title={n.text}>{n.text}</span>
            <span className="st">{n.consumed ? '已注入' : '待注入'}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}
