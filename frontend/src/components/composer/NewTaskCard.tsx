import { useEffect, useMemo, useRef, useState } from 'react';
import { pickDirectory } from '../../api';
import { modelOptions, NODE_TEXT, sourcesFor } from '../../ui';
import { KEY, readJson, readText, writeJson, writeText } from '../../storage';
import { ComposerTextarea } from './ComposerTextarea';
import { NodePicker, type NodeChoice } from './NodePicker';
import type { CardProps, NodeChoices } from './types';
import type { NodeId, RunMeta } from '../../types';

const NODES: NodeId[] = ['main', 'worker', 'verifier'];
/** 节点 → 它需要哪类能力：主 agent 只调模型，另两个要动手 */
const NEED: Record<NodeId, 'chat' | 'agent'> = {
  main: 'chat',
  worker: 'agent',
  verifier: 'agent',
};

function FolderIcon() {
  return (
    <svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true">
      <path
        d="M1.8 4.2c0-.7.6-1.2 1.2-1.2h3l1.3 1.4h5.7c.7 0 1.2.6 1.2 1.2v6.2c0 .7-.6 1.2-1.2 1.2H3c-.7 0-1.2-.6-1.2-1.2V4.2Z"
        fill="none" stroke="currentColor" strokeWidth="1.3" strokeLinejoin="round"
      />
    </svg>
  );
}

/** 预填：上次选过的优先，但来源已经不存在了就退回配置值 */
function initialChoices(meta: RunMeta): NodeChoices {
  const saved = readJson<Partial<NodeChoices>>(KEY.nodeChoices, {});
  const known = new Set((meta.sources ?? []).map((s) => s.name));
  const out = {} as NodeChoices;
  for (const node of NODES) {
    const fallback = meta.nodes?.[node] ?? { source: '', model: '' };
    const pick = saved?.[node];
    out[node] = pick && known.has(pick.source)
      ? { source: pick.source, model: pick.model ?? '' }
      : { source: fallback.source, model: fallback.model ?? '' };
  }
  return out;
}

/** 新任务：目标 + 产出目录 + 三个节点各自的来源与模型 */
export function NewTaskCard({ meta, defaultWorkdir, preset, onStart }: CardProps<'new'>) {
  const [goal, setGoal] = useState('');
  const [nodes, setNodes] = useState<NodeChoices>(() => initialChoices(meta));
  const [workdir, setWorkdir] = useState('');
  const [hint, setHint] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [picking, setPicking] = useState(false);

  // 上次用过的目录优先；设置保存时会同步更新，切到新的默认目录
  useEffect(() => {
    setWorkdir(readText(KEY.workdir) || defaultWorkdir || '');
  }, [defaultWorkdir]);

  // 配置变了（设置里改了来源 / 模型）→ 重新预填，别让页面停在旧值上
  const metaKey = useMemo(
    () => NODES.map((n) => `${meta.nodes?.[n]?.source}:${meta.nodes?.[n]?.model}`).join('|')
      + `|${(meta.sources ?? []).map((s) => s.name).join(',')}`,
    [meta],
  );
  useEffect(() => {
    setNodes(initialChoices(meta));
    // metaKey 就是这份配置的指纹，够用了
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [metaKey]);

  // 从历史「在此目录新建任务」带过来的目标与目录。
  // 用 n 记住「已经应用过哪一次」，避免重新挂载时把用户新写的内容覆盖掉。
  const appliedPreset = useRef(0);
  useEffect(() => {
    if (!preset || preset.n === appliedPreset.current) return;
    appliedPreset.current = preset.n;
    setGoal(preset.goal || '');
    if (preset.workdir) {
      setWorkdir(preset.workdir);
      writeText(KEY.workdir, preset.workdir);
    }
  }, [preset]);

  const update = (node: NodeId, next: NodeChoice) => {
    setNodes((prev) => {
      const merged = { ...prev, [node]: next };
      writeJson(KEY.nodeChoices, merged);
      return merged;
    });
  };

  const chooseDir = async () => {
    if (picking) return;
    setPicking(true);
    setHint('');
    try {
      const res = await pickDirectory();
      if (!res.ok) {
        setHint(res.error || '打开文件夹选择器失败');
        return;
      }
      if (!res.path) return; // 用户点了取消
      setWorkdir(res.path);
      writeText(KEY.workdir, res.path);
    } catch (err) {
      setHint('打开文件夹选择器失败：' + (err instanceof Error ? err.message : String(err)));
    } finally {
      setPicking(false);
    }
  };

  const submit = async () => {
    if (picking || submitting) return;
    if (!goal.trim()) {
      setHint('先写个目标');
      return;
    }
    setSubmitting(true);
    setHint('');
    if (workdir.trim()) writeText(KEY.workdir, workdir.trim());
    const err = await onStart({
      goal: goal.trim(),
      workdir: workdir.trim(),
      nodes,
    });
    setSubmitting(false);
    if (err) {
      setHint(err);
      return;
    }
    setGoal('');
  };

  return (
    <div className="composer-card new-card">
      <ComposerTextarea
        value={goal}
        onChange={setGoal}
        onSubmit={() => void submit()}
        placeholder="例如：在 workspace/fib 里写 fib.py，打印前 10 个斐波那契数（每行一个），确认第一行是 1"
      />

      <div className="launch-opts">
        <span className="field-label">产出目录</span>
        <button
          type="button"
          className="dir-pick"
          onClick={() => void chooseDir()}
          disabled={picking}
          title={workdir || defaultWorkdir || '点击选择产出目录'}
        >
          <FolderIcon />
          <span className="dir-path">
            {workdir || defaultWorkdir || '点击选择产出目录…'}
          </span>
          <span className="dir-browse">{picking ? '选择中…' : '浏览'}</span>
        </button>
        <button
          type="button"
          className="ghost"
          disabled={picking}
          onClick={() => {
            setWorkdir(defaultWorkdir);
            writeText(KEY.workdir, defaultWorkdir);
          }}
        >
          默认
        </button>
      </div>

      <div className="launch-opts nodes-row">
        {NODES.map((node) => (
          <NodePicker
            key={node}
            node={node}
            value={nodes[node]}
            sources={sourcesFor(meta, NEED[node])}
            models={modelOptions(meta, nodes[node].source)}
            onChange={(next) => update(node, next)}
            title={node === 'verifier'
              ? '建议和 worker 用不同模型，判定才真正独立'
              : `${NODE_TEXT[node]}这次用哪个来源的哪个模型`}
          />
        ))}
        <button
          className="primary"
          onClick={() => void submit()}
          disabled={submitting || picking}
        >
          {submitting ? '启动中…' : '开始'}
        </button>
      </div>

      {hint && <div className="launch-hint">{hint}</div>}
    </div>
  );
}
