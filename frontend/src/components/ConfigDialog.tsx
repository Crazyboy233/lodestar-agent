import { useCallback, useEffect, useState } from 'react';
import type { ConfigView } from '../types';
import { fetchConfig, pickDirectory, reportClient, saveConfig } from '../api';

interface Field {
  key: string;
  label: string;
  type: 'text' | 'password' | 'number' | 'lines' | 'dir';
  step?: string;
  tip?: string;
}

interface Group {
  section: string;
  title: string;
  desc: string;
  fields: Field[];
}

const SCHEMA: Group[] = [
  {
    section: 'main_agent',
    title: '主模型',
    desc: '负责拆解任务、动态决策和汇总结果',
    fields: [
      { key: 'base_url', label: '接口地址', type: 'text', tip: 'OpenAI 兼容地址，通常以 /v1 结尾' },
      { key: 'api_key', label: 'API 密钥', type: 'password', tip: '留空表示不修改；填新值才会覆盖' },
      { key: 'model', label: '模型', type: 'text', tip: '主 agent 调用时使用的 model id' },
      { key: 'temperature', label: '温度', type: 'number', step: '0.1', tip: '0 ~ 2，越大越发散' },
      { key: 'max_retries', label: '请求重试', type: 'number', tip: '单次请求失败后的重试次数' },
      { key: 'plan_retries', label: '拆解重试', type: 'number', tip: '拆解结果不是合法 JSON 时重试几次' },
    ],
  },
  {
    section: 'worker',
    title: '执行端',
    desc: '真正动手改文件的 CLI',
    fields: [
      { key: 'cli', label: 'CLI 命令', type: 'text', tip: '可执行名或绝对路径' },
      { key: 'model', label: '模型', type: 'text', tip: '留空 = 由 CLI 自己决定' },
      { key: 'workdir', label: '默认产出目录', type: 'dir', tip: '点击「选择…」打开系统文件夹选择器；每次任务还可以在首页单独指定' },
      { key: 'allowed_tools', label: '允许的工具', type: 'text', tip: '逗号分隔，例如 Read,Write,Edit,Bash' },
      { key: 'timeout_seconds', label: '单轮超时', type: 'number', step: '10', tip: '一次 CLI 调用最长跑多久（秒）' },
      { key: 'max_turns', label: '最大轮次', type: 'number', tip: 'CLI 内部最多交互几轮' },
      { key: 'max_retries', label: '失败重试', type: 'number', tip: '执行失败后换一次机会再跑' },
      { key: 'add_dirs', label: '额外目录', type: 'lines', tip: '每行一个额外授权的目录' },
      { key: 'env', label: '环境变量', type: 'lines', tip: '每行一个 KEY=VALUE' },
    ],
  },
  {
    section: 'verifier',
    title: '验收端',
    desc: '独立判定产出是否达标',
    fields: [
      { key: 'cli', label: 'CLI 命令', type: 'text', tip: '可执行名或绝对路径' },
      { key: 'model', label: '模型', type: 'text', tip: '建议和执行端不同，判定才真正独立' },
      { key: 'allowed_tools', label: '允许的工具', type: 'text', tip: '逗号分隔，一般需要 Read 和 Bash' },
      { key: 'scratch_dir', label: '测试脚本目录', type: 'text', tip: '相对产出目录' },
      { key: 'timeout_seconds', label: '单轮超时', type: 'number', step: '10', tip: '一次验收最长跑多久（秒）' },
      { key: 'max_rounds', label: '最大轮次', type: 'number', tip: '同一条任务最多「执行→验收」几轮' },
      { key: 'max_retries', label: '失败重试', type: 'number' },
      { key: 'env', label: '环境变量', type: 'lines', tip: '每行一个 KEY=VALUE' },
    ],
  },
  {
    section: 'ui',
    title: '控制台',
    desc: '退出时机与模型下拉候选',
    fields: [
      {
        key: 'drain_timeout_seconds', label: '退出等待上限', type: 'number', step: '10',
        tip: '点退出后最多等多久（等当前这轮回复跑完），超过才强制断开',
      },
      {
        key: 'models', label: '可选模型', type: 'lines',
        tip: '每行一个；留空即用内置列表，「默认（CLI 决定）」始终可选',
      },
    ],
  },
];

const SECTION_LABEL: Record<string, string> = Object.fromEntries(
  SCHEMA.map((g) => [g.section, g.title]),
);

const FIELD_LABEL: Record<string, string> = Object.fromEntries(
  SCHEMA.flatMap((g) => g.fields.map((f) => [`${g.section}.${f.key}`, f.label])),
);

/** 把后端校验信息里的 `worker.timeout_seconds` 换成看得懂的中文路径 */
function prettyError(msg: string): string {
  return msg.replace(
    /\b(main_agent|worker|verifier|ui)\.([a-z_]+)/g,
    (_m, section: string, key: string) => {
      const label = FIELD_LABEL[`${section}.${key}`];
      const group = SECTION_LABEL[section] ?? section;
      return label ? `${group} · ${label}` : `${group} · ${key}`;
    },
  );
}

type FormState = Record<string, Record<string, string>>;

function toForm(cfg: ConfigView): FormState {
  const out: FormState = {};
  for (const group of SCHEMA) {
    const section = (cfg as unknown as Record<string, Record<string, unknown>>)[group.section] ?? {};
    out[group.section] = {};
    for (const f of group.fields) {
      // api_key 不回填：留空表示不修改，避免把掩码当成真值再写回去
      out[group.section][f.key] = f.key === 'api_key' ? '' : String(section[f.key] ?? '');
    }
  }
  return out;
}

interface Props {
  open: boolean;
  onClose: () => void;
  onSaved: (cfg: ConfigView) => void;
}

export function ConfigDialog({ open, onClose, onSaved }: Props) {
  const [form, setForm] = useState<FormState | null>(null);
  const [apiKeyMasked, setApiKeyMasked] = useState('');
  const [active, setActive] = useState(SCHEMA[0].section);
  const [error, setError] = useState('');
  const [note, setNote] = useState('');
  const [saving, setSaving] = useState(false);
  const [picking, setPicking] = useState('');

  const load = useCallback(async () => {
    setForm(null);
    setError('');
    setNote('');
    try {
      const res = await fetchConfig();
      if (!res.ok || !res.config) throw new Error(res.error || '读取失败');
      setForm(toForm(res.config));
      setApiKeyMasked(res.config.main_agent.api_key);
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      reportClient('读取配置失败：' + msg);
      setError('读取配置失败：' + msg);
    }
  }, []);

  useEffect(() => {
    if (open) void load();
  }, [open, load]);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [open, onClose]);

  const setField = (section: string, key: string, value: string) => {
    setForm((prev) => {
      if (!prev) return prev;
      return { ...prev, [section]: { ...prev[section], [key]: value } };
    });
  };

  const chooseDir = async (section: string, key: string) => {
    if (picking) return;
    setPicking(`${section}.${key}`);
    setError('');
    setNote('');
    try {
      const res = await pickDirectory();
      if (!res.ok) {
        setError(res.error || '打开文件夹选择器失败');
        return;
      }
      if (res.path) setField(section, key, res.path);
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      setError('打开文件夹选择器失败：' + msg);
    } finally {
      setPicking('');
    }
  };

  const save = async () => {
    if (!form) return;
    setSaving(true);
    setError('');
    setNote('');
    try {
      const res = await saveConfig(form);
      if (!res.ok) {
        const list = res.errors?.length ? res.errors : [res.error ?? '保存失败'];
        setError(list.map(prettyError).join('；'));
        return;
      }
      const cfg = await fetchConfig();
      if (cfg.ok && cfg.config) {
        setForm(toForm(cfg.config));
        setApiKeyMasked(cfg.config.main_agent.api_key);
        onSaved(cfg.config);
      } else {
        await load();
      }
      setNote(res.note ?? '已保存');
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      reportClient('保存配置失败：' + msg);
      setError('保存失败：' + msg);
    } finally {
      setSaving(false);
    }
  };

  const group = SCHEMA.find((g) => g.section === active) ?? SCHEMA[0];

  return (
    <div
      className={`modal-mask${open ? ' on' : ''}`}
      onMouseDown={(e) => { if (e.target === e.currentTarget) onClose(); }}
    >
      <div className="modal" role="dialog" aria-modal="true" aria-label="设置" aria-hidden={!open}>
        <div className="modal-head">
          <h2>设置</h2>
          <span className="modal-sub">保存到 config.local.yaml，下次开始任务生效</span>
          <button className="modal-x" onClick={onClose} title="关闭">✕</button>
        </div>

        <div className="modal-body">
          <div className="cfg-tabs" role="tablist">
            {SCHEMA.map((g) => (
              <button
                key={g.section}
                role="tab"
                aria-selected={g.section === active}
                className={g.section === active ? 'on' : ''}
                onClick={() => setActive(g.section)}
              >
                {g.title}
              </button>
            ))}
          </div>

          <div className="cfg-pane">
            <div className="cfg-pane-head">
              <h3>{group.title}</h3>
              <p>{group.desc}</p>
            </div>

            {!form && !error && <div className="cfg-tip-block">正在读取配置…</div>}

            {form && group.fields.map((f) => {
              const value = form[group.section]?.[f.key] ?? '';
              return (
                <div className="cfg-field" key={f.key}>
                  <div className="cfg-row">
                    <label>
                      <span>{f.label}</span>
                      <code>{f.key}</code>
                    </label>
                    {f.type === 'dir' ? (
                      <div className="cfg-dir">
                        <input
                          type="text"
                          value={value}
                          readOnly
                          title={value}
                          placeholder="点击选择产出目录"
                          onClick={() => void chooseDir(group.section, f.key)}
                        />
                        <button
                          type="button"
                          className="ghost"
                          disabled={picking !== ''}
                          onClick={() => void chooseDir(group.section, f.key)}
                        >
                          {picking === `${group.section}.${f.key}` ? '选择中…' : '选择…'}
                        </button>
                      </div>
                    ) : f.type === 'lines' ? (
                      <textarea
                        value={value}
                        onChange={(e) => setField(group.section, f.key, e.target.value)}
                      />
                    ) : (
                      <input
                        type={f.type === 'password' ? 'password' : f.type === 'number' ? 'number' : 'text'}
                        step={f.step}
                        value={value}
                        placeholder={f.key === 'api_key' ? (apiKeyMasked || '未设置') : undefined}
                        onChange={(e) => setField(group.section, f.key, e.target.value)}
                      />
                    )}
                  </div>
                  {f.tip && <div className="cfg-tip">{f.tip}</div>}
                </div>
              );
            })}
          </div>
        </div>

        <div className="modal-foot">
          <span className={`modal-hint${error ? ' err' : ''}`}>{error || note}</span>
          <button className="ghost" onClick={onClose}>取消</button>
          <button className="primary" onClick={() => void save()} disabled={!form || saving || picking !== ''}>
            {saving ? '保存中…' : '保存'}
          </button>
        </div>
      </div>
    </div>
  );
}
