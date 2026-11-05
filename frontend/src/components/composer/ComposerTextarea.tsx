interface Props {
  value: string;
  onChange: (value: string) => void;
  /** ⌘ / Ctrl + Enter 触发同一个提交动作 */
  onSubmit: () => void;
  placeholder: string;
  disabled?: boolean;
}

/**
 * 输入区里那个 textarea。三种 composer（新任务 / 引导 / 续跑）共用一份，
 * 快捷键、禁用态、样式只有一处定义。
 */
export function ComposerTextarea({
  value, onChange, onSubmit, placeholder, disabled = false,
}: Props) {
  return (
    <textarea
      value={value}
      onChange={(e) => onChange(e.target.value)}
      onKeyDown={(e) => {
        if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) {
          e.preventDefault();
          onSubmit();
        }
      }}
      placeholder={placeholder}
      disabled={disabled}
    />
  );
}
