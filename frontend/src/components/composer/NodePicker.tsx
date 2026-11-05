import type { NodeId, SourceInfo } from '../../types';
import { NODE_TEXT } from '../../ui';
import { ComboInput, Dropdown, type Option } from '../Dropdown';

/** 下拉里的「沿用当前配置」：不传这个字段，等价于后端拿 None（不覆盖 cfg） */
export const KEEP = '__keep';

export interface NodeChoice {
  source: string;
  model: string;
}

interface Props {
  node: NodeId;
  value: NodeChoice;
  /** 这个节点能选的来源（已按能力过滤） */
  sources: SourceInfo[];
  /** 当前来源的模型候选；空数组也能用（datainput 允许自由输入） */
  models: string[];
  onChange: (next: NodeChoice) => void;
  /** 给了就多一个「沿用」项，并把它写在文案里（续跑卡用） */
  keepLabel?: string;
  disabled?: boolean;
  title?: string;
}

/**
 * 一个节点 = 一个来源 + 一个模型。
 *
 * 之前是「一个模型下拉」，来源被写死在各处；现在来源也是一等公民：
 * 主 agent 可以是 API、codebuddy，或以后接进来的任何 CLI。
 *
 * 两个控件都是自绘的（见 components/Dropdown）：原生 select / datalist
 * 的浮层交给浏览器画，尺寸和配色不跟着这套界面走。
 * 模型保持可自由输入：HTTP 接口的型号没法枚举，CLI 的候选也不该
 * 限制用户填别的（比如供应商新出的型号）。
 */
export function NodePicker({
  node, value, sources, models, onChange, keepLabel, disabled, title,
}: Props) {
  const keeping = value.source === KEEP;

  const sourceOpts: Option[] = [];
  if (keepLabel !== undefined) {
    sourceOpts.push({ value: KEEP, label: `沿用 · ${keepLabel}`, hint: '不改动配置' });
  }
  for (const s of sources) {
    sourceOpts.push({ value: s.name, label: s.name, hint: s.display });
  }

  const modelOpts: Option[] = models.map((m) => ({
    value: m,
    label: m || '默认模型',
    hint: m ? '' : '该来源的默认',
  }));

  return (
    <div className="sel" title={title}>
      <span>{NODE_TEXT[node]}</span>
      <Dropdown
        className="src-pick"
        value={value.source}
        options={sourceOpts}
        onChange={(source) => onChange({ ...value, source })}
        disabled={disabled}
        ariaLabel={`${NODE_TEXT[node]}来源`}
        minWidth={240}
        placeholder="选择来源"
      />
      <ComboInput
        className="model-input"
        value={value.model}
        options={modelOpts}
        onChange={(model) => onChange({ ...value, model })}
        placeholder={keeping ? '—' : '默认模型'}
        disabled={disabled || keeping}
        title={keeping ? '沿用当前配置里的模型' : '留空 = 该来源的默认模型'}
        ariaLabel={`${NODE_TEXT[node]}模型`}
        minWidth={200}
      />
    </div>
  );
}
