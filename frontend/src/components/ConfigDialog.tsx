import { useCallback, useEffect, useState } from 'react';
import type { ConfigView, NodeId } from '../types';
import { fetchConfig, pickDirectory, reportClient, saveConfig } from '../api';
import { Dropdown } from './Dropdown';
import { NODE_TEXT } from '../ui';

interface Field {
  key: string;
  label: string;
  type: 'text' | 'password' | 'number' | 'lines' | 'dir';
  step?: string;
  tip?: string;
}

interface Group {
  title: string;
  desc: string;
  fields: Field[];
}

/**
 * 字段表：只描述「长什么样、叫什么」，不描述「存在哪」——
 * 存到哪个 section 由渲染时的位置决定（节点 / 某个来源 / 控制台）。
 * 白名单与范围校验在服务端（config.py 的 NODE_FIELDS / PROVIDER_FIELDS），这里只负责好看。
 */
const NODE_SCHEMA: Record<NodeId, Group> = {
  main: {
    title: '主 agent',
    desc: '拆解方案、每轮动态决策、汇总 —— 只调模型，不碰文件',
    fields: [
      { key: 'model', label: '模型', type: 'text', tip: '留空 = 该来源的默认模型' },
      { key: 'temperature', label: '温度', type: 'number', step: '0.1', tip: '0 ~ 2，越大越发散' },
      { key: 'max_retries', label: '请求重试', type: 'number', tip: '单次请求失败后的重试次数' },
      { key: 'plan_retries', label: '拆解重试', type: 'number', tip: '拆解结果不是合法 JSON 时重试几次' },
    ],
  },
  worker: {
    title: 'worker',
    desc: '真正动手改文件、跑命令的节点',
    fields: [
      { key: 'model', label: '模型', type: 'text', tip: '留空 = 该来源的默认模型' },
      { key: 'workdir', label: '默认产出目录', type: 'dir', tip: '点「选择…」打开系统文件夹选择器；每次任务还能单独指定' },
      { key: 'allowed_tools', label: '允许的工具', type: 'text', tip: '逗号分隔：Read,Write,Edit,Bash（API 来源也按这份清单暴露工具）' },
      { key: 'timeout_seconds', label: '单次超时', type: 'number', step: '10', tip: '一次调用最长跑多久（秒）' },
      { key: 'max_turns', label: '最大轮次', type: 'number', tip: 'CLI：0 = 不传，用 CLI 默认；API：工具循环轮数' },
      { key: 'max_retries', label: '失败重试', type: 'number', tip: '执行失败后换一次机会再跑' },
      { key: 'add_dirs', label: '额外目录', type: 'lines', tip: '每行一个额外授权的目录' },
      { key: 'env', label: '环境变量', type: 'lines', tip: '每行一个 KEY=VALUE，只给这个节点' },
    ],
  },
  verifier: {
    title: '验收',
    desc: '独立判定产出是否达标：自己写测试脚本自己跑',
    fields: [
      { key: 'model', label: '模型', type: 'text', tip: '建议和 worker 不同，判定才真正独立' },
      { key: 'allowed_tools', label: '允许的工具', type: 'text', tip: '逗号分隔，一般需要 Read 和 Bash（写测试脚本要 Write）' },
      { key: 'scratch_dir', label: '测试脚本目录', type: 'text', tip: '相对产出目录' },
      { key: 'timeout_seconds', label: '单次超时', type: 'number', step: '10', tip: '一次验收最长跑多久（秒）' },
      { key: 'max_rounds', label: '最大轮次', type: 'number', tip: '同一条任务最多「执行→验收」几轮' },
      { key: 'max_retries', label: '失败重试', type: 'number' },
      { key: 'env', label: '环境变量', type: 'lines', tip: '每行一个 KEY=VALUE' },
    ],
  },
};

const PROVIDER_SCHEMA: Record<'api' | 'cli', Group> = {
  api: {
    title: 'HTTP 接口',
    desc: 'OpenAI 兼容：既能当纯文本模型，也能当会干活的 worker',
    fields: [
      { key: 'base_url', label: '接口地址', type: 'text', tip: 'OpenAI 兼容地址，通常以 /v1 结尾' },
      { key: 'api_key', label: 'API 密钥', type: 'password', tip: '留空表示不修改；填新值才会覆盖' },
      { key: 'temperature', label: '温度', type: 'number', step: '0.1', tip: '这个来源当 worker 用时（工具循环）的温度' },
      { key: 'timeout_seconds', label: '单次超时', type: 'number', step: '10', tip: '一次调用最长跑多久（秒）' },
    ],
  },
  cli: {
    title: '本机 CLI',
    desc: 'codebuddy / codex / trae …：命令行 agent 也能当纯文本模型',
    fields: [
      { key: 'command', label: 'CLI 命令', type: 'text', tip: '可执行名或绝对路径' },
      { key: 'dialect', label: '方言', type: 'text', tip: 'codebuddy = 内置参数与输出解析；留空按命令名推断；其余走模板' },
      { key: 'timeout_seconds', label: '单次超时', type: 'number', step: '10', tip: '当纯文本模型用时，一次调用的墙钟上限' },
      { key: 'chat_tools', label: '纯文本工具参数', type: 'text', tip: '当纯文本模型用时传的工具参数；留空 = 不传，由 CLI 决定' },
      { key: 'chat_cwd', label: '纯文本工作目录', type: 'text', tip: '留空 = 配置文件所在目录' },
      { key: 'env', label: '环境变量', type: 'lines', tip: '每行一个 KEY=VALUE，注入给该 CLI 的子进程' },
    ],
  },
};

const UI_FIELDS: Field[] = [
  {
    key: 'drain_timeout_seconds', label: '退出等待上限', type: 'number', step: '10',
    tip: '点退出后最多等多久（等当前这轮回复跑完），超过才强制断开',
  },
];

const NODES: NodeId[] = ['main', 'worker', 'verifier'];

/** 节点字段的中文名（把后端校验信息里的 nodes.worker.timeout_seconds 换成看得懂的） */
const FIELD_LABEL: Record<string, string> = (() => {
  const out: Record<string, string> = {};
  for (const node of NODES) {
    for (const f of NODE_SCHEMA[node].fields) out[`nodes.${node}.${f.key}`] = `${NODE_TEXT[node]} · ${f.label}`;
    out[`nodes.${node}.source`] = `${NODE_TEXT[node]} · 来源`;
  }
  for (const group of Object.values(PROVIDER_SCHEMA)) {
    for (const f of group.fields) {
      out[`providers.*.${f.key}`] = `${group.title} · ${f.label}`;
    }
  }
  out['ui.drain_timeout_seconds'] = '控制台 · 退出等待上限';
  return out;
})();

/** 把后端校验信息里的字段路径换成看得懂的中文 */
function prettyError(msg: string): string {
  return msg.replace(
    /\b(nodes|providers)\.([a-z_]+)\.([a-z_]+)/g,
    (_m, section: string, mid: string, key: string) => {
      const path = section === 'nodes' ? `nodes.${mid}.${key}` : `providers.*.${key}`;
      return FIELD_LABEL[path] ?? `${section}.${mid}.${key}`;
    },
  );
}

type Section = { kind: 'node'; id: NodeId } | { kind: 'provider'; name: string; pkind: 'api' | 'cli' } | { kind: 'ui' };

interface Form {
  nodes: Record<string, Record<string, string>>;
  providers: Record<string, Record<string, string>>;
  ui: { drain: string; models: Record<string, string> };
}

function toForm(cfg: ConfigView): Form {
  const form: Form = { nodes: {}, providers: {}, ui: { drain: '', models: {} } };
  for (const node of NODES) {
    const src = cfg.nodes?.[node] ?? {};
    form.nodes[node] = {};
    for (const key of ['source', ...NODE_SCHEMA[node].fields.map((f) => f.key)]) {
      form.nodes[node][key] = String(src[key] ?? '');
    }
  }
  for (const p of cfg.providers ?? []) {
    const fields = PROVIDER_SCHEMA[p.kind]?.fields ?? [];
    form.providers[p.name] = {};
    for (const f of fields) {
      // api_key 不回填：留空表示不修改，避免把掩码当成真值再写回去
      form.providers[p.name][f.key] = f.key === 'api_key' ? '' : String(p.fields[f.key] ?? '');
    }
  }
  form.ui.drain = String(cfg.ui?.drain_timeout_seconds ?? '');
  form.ui.models = { ...(cfg.ui?.models ?? {}) };
  return form;
}

interface Props {
  open: boolean;
  onClose: () => void;
  onSaved: (cfg: ConfigView) => void;
}

export function ConfigDialog({ open, onClose, onSaved }: Props) {
  const [cfg, setCfg] = useState<ConfigView | null>(null);
  const [form, setForm] = useState<Form | null>(null);
  const [active, setActive] = useState<string>('nodes');
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
      setCfg(res.config);
      setForm(toForm(res.config));
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

  const setNodeField = (node: string, key: string, value: string) => {
    setForm((prev) => prev && {
      ...prev,
      nodes: { ...prev.nodes, [node]: { ...prev.nodes[node], [key]: value } },
    });
  };

  const setProviderField = (name: string, key: string, value: string) => {
    setForm((prev) => prev && {
      ...prev,
      providers: { ...prev.providers, [name]: { ...prev.providers[name], [key]: value } },
    });
  };

  const setUi = (key: 'drain' | 'models', value: string | Record<string, string>) => {
    setForm((prev) => prev && { ...prev, ui: { ...prev.ui, [key]: value } });
  };

  const chooseDir = async (node: string, key: string) => {
    if (picking) return;
    setPicking(`${node}.${key}`);
    setError('');
    setNote('');
    try {
      const res = await pickDirectory();
      if (!res.ok) {
        setError(res.error || '打开文件夹选择器失败');
        return;
      }
      if (res.path) setNodeField(node, key, res.path);
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
      const payload = {
        nodes: form.nodes,
        providers: form.providers,
        ui: { drain_timeout_seconds: form.ui.drain, models: form.ui.models },
      };
      const res = await saveConfig(payload);
      if (!res.ok) {
        const list = res.errors?.length ? res.errors : [res.error ?? '保存失败'];
        setError(list.map(prettyError).join('；'));
        return;
      }
      const next = await fetchConfig();
      if (next.ok && next.config) {
        setCfg(next.config);
        setForm(toForm(next.config));
        onSaved(next.config);
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

  // ── 渲染 ──────────────────────────────────────────────
  const sections: Section[] = [
    { kind: 'node', id: 'main' },
    { kind: 'node', id: 'worker' },
    { kind: 'node', id: 'verifier' },
    ...(cfg?.providers ?? []).map((p): Section => ({
      kind: 'provider', name: p.name, pkind: p.kind,
    })),
    { kind: 'ui' },
  ];
  const tabKey = (s: Section) => (s.kind === 'ui' ? 'ui' : s.kind === 'node' ? `node:${s.id}` : `p:${s.name}`);
  const tabText = (s: Section) => (s.kind === 'ui'
    ? '控制台'
    : s.kind === 'node' ? NODE_TEXT[s.id] : s.name);

  const sources = (cfg?.providers ?? []).map((p) => p.name);

  const renderFields = (group: Group, section: string, value: Record<string, string>,
                        onChange: (k: string, v: string) => void, asNode?: NodeId) => (
    <>
      {asNode && (
        <div className="cfg-field">
          <div className="cfg-row">
            <label><span>来源</span><code>source</code></label>
            <Dropdown
              className="cfg-select"
              value={value.source || ''}
              options={sources.map((name) => ({ value: name, label: name }))}
              onChange={(next) => onChange('source', next)}
              ariaLabel="来源"
              placeholder="选择来源"
              minWidth={200}
            />
          </div>
          <div className="cfg-tip">这个节点用哪个来源的模型：api、codebuddy，或配置里新增的任意 CLI</div>
        </div>
      )}
      {group.fields.map((f) => {
        const v = value[f.key] ?? '';
        return (
          <div className="cfg-field" key={`${section}.${f.key}`}>
            <div className="cfg-row">
              <label><span>{f.label}</span><code>{f.key}</code></label>
              {f.type === 'dir' ? (
                <div className="cfg-dir">
                  <input
                    type="text" value={v} readOnly title={v}
                    placeholder="点击选择产出目录"
                    onClick={() => asNode && void chooseDir(asNode, f.key)}
                  />
                  <button
                    type="button" className="ghost"
                    disabled={picking !== ''}
                    onClick={() => asNode && void chooseDir(asNode, f.key)}
                  >
                    {picking === `${asNode}.${f.key}` ? '选择中…' : '选择…'}
                  </button>
                </div>
              ) : f.type === 'lines' ? (
                <textarea value={v} onChange={(e) => onChange(f.key, e.target.value)} />
              ) : (
                <input
                  type={f.type === 'password' ? 'password' : f.type === 'number' ? 'number' : 'text'}
                  step={f.step}
                  value={v}
                  placeholder={f.key === 'api_key' ? (apiKeyMask(section) || '未设置') : undefined}
                  onChange={(e) => onChange(f.key, e.target.value)}
                />
              )}
            </div>
            {f.tip && <div className="cfg-tip">{f.tip}</div>}
          </div>
        );
      })}
    </>
  );

  const apiKeyMask = (name: string): string => {
    const p = cfg?.providers.find((x) => x.name === name);
    const value = p?.fields.api_key;
    return typeof value === 'string' ? value : '';
  };

  const activeSection = sections.find((s) => tabKey(s) === active) ?? sections[0];

  const body = () => {
    if (!form) return null;
    if (activeSection.kind === 'node') {
      const node = activeSection.id;
      const group = NODE_SCHEMA[node];
      return (
        <>
          <div className="cfg-pane-head"><h3>{group.title}</h3><p>{group.desc}</p></div>
          {renderFields(
            group, `nodes.${node}`, form.nodes[node] ?? {},
            (k, v) => setNodeField(node, k, v), node,
          )}
        </>
      );
    }
    if (activeSection.kind === 'provider') {
      const group = PROVIDER_SCHEMA[activeSection.pkind];
      return (
        <>
          <div className="cfg-pane-head">
            <h3>{activeSection.name} · {group.title}</h3>
            <p>{group.desc}</p>
          </div>
          {renderFields(
            group, activeSection.name, form.providers[activeSection.name] ?? {},
            (k, v) => setProviderField(activeSection.name, k, v),
          )}
        </>
      );
    }
    return (
      <>
        <div className="cfg-pane-head">
          <h3>控制台</h3>
          <p>退出时机与各来源的模型候选</p>
        </div>
        {UI_FIELDS.map((f) => (
          <div className="cfg-field" key={f.key}>
            <div className="cfg-row">
              <label><span>{f.label}</span><code>{f.key}</code></label>
              <input
                type="number" step={f.step} value={form.ui.drain}
                onChange={(e) => setUi('drain', e.target.value)}
              />
            </div>
            {f.tip && <div className="cfg-tip">{f.tip}</div>}
          </div>
        ))}
        {sources.map((name) => (
          <div className="cfg-field" key={`models.${name}`}>
            <div className="cfg-row">
              <label><span>{name} 可选模型</span><code>ui.models.{name}</code></label>
              <textarea
                value={form.ui.models[name] ?? ''}
                onChange={(e) => setUi('models', { ...form.ui.models, [name]: e.target.value })}
              />
            </div>
            <div className="cfg-tip">
              每行一个；只是下拉候选，不限制手工填别的型号。留空 = 用该来源的默认候选，
              「默认模型」（空串）始终可选。
            </div>
          </div>
        ))}
      </>
    );
  };

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
            {sections.map((s) => (
              <button
                key={tabKey(s)}
                role="tab"
                aria-selected={tabKey(s) === active}
                className={tabKey(s) === active ? 'on' : ''}
                onClick={() => setActive(tabKey(s))}
              >
                {tabText(s)}
              </button>
            ))}
          </div>

          <div className="cfg-pane">
            {!form && !error && <div className="cfg-tip-block">正在读取配置…</div>}
            {form && body()}
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
