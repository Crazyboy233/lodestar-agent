/** 下拉里的「沿用当前配置」：不传这个字段，等价于后端拿 None（不动 cfg） */
export const KEEP_MODEL = '__keep';

interface Props {
  label: string;
  /** KEEP_MODEL / '' / 具体模型名 */
  value: string;
  options: string[];
  onChange: (value: string) => void;
  /** 含「沿用」项时，把当前生效的模型写进去，别只写「沿用当前配置」 */
  keepLabel?: string;
  disabled?: boolean;
  title?: string;
}

/**
 * worker / 验收的下拉。之前四份几乎一样的 select，语义（空串=CLI 决定、
 * KEEP=沿用配置）也散在各自文件里，集中到这一处。
 */
export function ModelPicker({
  label, value, options, onChange, keepLabel, disabled, title,
}: Props) {
  return (
    <label className="sel" title={title}>
      <span>{label}</span>
      <select
        value={value}
        onChange={(e) => onChange(e.target.value)}
        disabled={disabled}
      >
        {keepLabel !== undefined && (
          <option value={KEEP_MODEL} title="沿用当前配置，不改动 cfg">{keepLabel}</option>
        )}
        {options.map((m) => (
          <option key={m || '__default'} value={m}>{m || '默认（CLI 决定）'}</option>
        ))}
      </select>
    </label>
  );
}
