export type NoteTarget = 'main' | 'worker';

interface Props {
  value: NoteTarget;
  onChange: (target: NoteTarget) => void;
  disabled?: boolean;
  /** 该收件人还排着几条没注入（角标） */
  pending?: Record<string, number>;
}

/** 引导投给谁：运行中的输入卡和续跑卡共用 */
export function GuideTargets({ value, onChange, disabled, pending }: Props) {
  return (
    <div className="guide-targets" role="group" aria-label="引导投给哪个节点">
      {(['main', 'worker'] as const).map((t) => (
        <button
          key={t}
          type="button"
          className={value === t ? 'on' : ''}
          disabled={disabled}
          onClick={() => onChange(t)}
        >
          {t === 'main' ? '主 agent' : 'worker'}
          {pending?.[t] ? <span className="n">{pending[t]}</span> : null}
        </button>
      ))}
    </div>
  );
}
