import { useState } from 'react';
import type { NoteItem } from '../types';

interface Props {
  notes: NoteItem[];
}

/**
 * 只负责「投递记录」：投递入口已经统一到事件流下方的对话框，
 * 这里不再重复输入框，避免两处都能发引导、看着像两套功能。
 */
export function GuidePanel({ notes }: Props) {
  const [open, setOpen] = useState<Set<string>>(() => new Set());
  const pending = notes.filter((n) => !n.consumed).length;

  const toggle = (id: string) => {
    setOpen((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  return (
    <div className="card guide">
      <div className="card-head">
        <h2>引导投递</h2>
        <span className="tasks-sum">
          {notes.length ? (pending ? `${pending} 待注入 / ${notes.length}` : `${notes.length} 条`) : '—'}
        </span>
      </div>
      <ul id="notes">
        {notes.length === 0 && <li className="empty">还没有投递</li>}
        {notes.map((n) => (
          <li
            key={n.id}
            className={`${n.consumed ? 'consumed' : ''}${open.has(n.id) ? ' open' : ''}`.trim()}
            onClick={() => toggle(n.id)}
          >
            <span className="tgt">{n.target === 'worker' ? 'worker' : 'main'}</span>
            <span className="txt">{n.text}</span>
            <span className="st">{n.consumed ? '已注入' : '待注入'}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}
