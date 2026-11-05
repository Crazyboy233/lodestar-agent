import { useEffect, useState } from 'react';
import { modelOptions, NODE_TEXT, sourcesFor, STATE_TEXT } from '../../ui';
import { ComposerTextarea } from './ComposerTextarea';
import { GuideTargets, type NoteTarget } from './GuideTargets';
import { KEEP, NodePicker, type NodeChoice } from './NodePicker';
import type { CardProps, NodeChoices, ResumeModels } from './types';
import type { NodeId } from '../../types';

/** 确认态几秒后自己撤销：避免很久以后再点一下就直接开跑 */
const ARM_TIMEOUT_MS = 5000;

/** 待续跑：会话信息 + 可选引导 + 模型 + 二次确认 */
const NODES: NodeId[] = ['main', 'worker', 'verifier'];
const NEED: Record<NodeId, 'chat' | 'agent'> = {
  main: 'chat',
  worker: 'agent',
  verifier: 'agent',
};
const KEPT: NodeChoices = {
  main: { source: KEEP, model: '' },
  worker: { source: KEEP, model: '' },
  verifier: { source: KEEP, model: '' },
};

export function ResumeCard({
  variant, meta, currentModels, onResume, onNewTask,
}: CardProps<'resume'>) {
  const { item } = variant;
  const [target, setTarget] = useState<NoteTarget>('main');
  const [note, setNote] = useState('');
  const [choices, setChoices] = useState<NodeChoices>(KEPT);
  const [armed, setArmed] = useState(false);
  const [hint, setHint] = useState('');
  const [sending, setSending] = useState(false);

  const hasNote = Boolean(note.trim());

  useEffect(() => {
    if (!armed) return;
    const id = window.setTimeout(() => setArmed(false), ARM_TIMEOUT_MS);
    return () => window.clearTimeout(id);
  }, [armed]);

  const confirm = async () => {
    if (sending) return;
    if (!armed) {
      setArmed(true);
      setHint('');
      return;
    }
    setArmed(false);
    setSending(true);
    // 保持「沿用」的节点不带字段：后端收到 None 才叫「沿用配置」，
    // 传空串是另一种语义（显式用该来源的默认模型）
    const models: ResumeModels = {};
    for (const node of NODES) {
      const pick = choices[node];
      if (pick.source !== KEEP) models[node] = { source: pick.source, model: pick.model };
    }
    const err = await onResume(
      item,
      hasNote ? { target, text: note.trim() } : null,
      models,
    );
    setSending(false);
    if (err) setHint(err);
  };

  // 这条待续跑是用户自己点「继续这个会话」带过来的，所以不提供「收掉」的入口；
  // 只有确认态能撤回（否则点了确认就只能干等 5 秒自动撤销）。
  const withdraw = () => {
    if (sending) return;
    setArmed(false);
    setHint('');
  };

  return (
    <div className={`composer-card resume-card${armed ? ' armed' : ''}`}>
      <div className="rb-head">
        <span className="rb-tag">待续跑</span>
        <span className="rb-thread">{item.thread}</span>
        <span>{STATE_TEXT[item.state] ?? item.state}</span>
      </div>
      <div className="rb-goal" title={item.goal}>{item.goal || '(没有记录目标)'}</div>
      <div className="rb-meta" title={item.workdir || '未记录产出目录'}>
        产出 {item.workdir || '—'}
      </div>

      <div className="rb-sep" />

      <ComposerTextarea
        value={note}
        onChange={(v) => {
          setNote(v);
          setHint('');
        }}
        onSubmit={() => void confirm()}
        disabled={sending}
        placeholder={target === 'main'
          ? '可选：给续跑的第一个规划调用留句话（会跟着这次续跑一起投递）'
          : '可选：给续跑后的第一条任务留句话（会跟着这次续跑一起投递）'}
      />

      <div className="launch-opts">
        <GuideTargets value={target} onChange={setTarget} disabled={sending} />
        <span className="composer-hint">
          {hasNote ? '开跑时先投这段引导，再做第一次调用' : '不写也可以，直接续跑'}
        </span>
      </div>

      <div className="launch-opts nodes-row">
        {NODES.map((node) => (
          <NodePicker
            key={node}
            node={node}
            value={choices[node]}
            sources={sourcesFor(meta, NEED[node])}
            models={modelOptions(meta, choices[node].source)}
            onChange={(next: NodeChoice) => setChoices((prev) => ({ ...prev, [node]: next }))}
            keepLabel={currentModels[node] || '当前配置'}
            disabled={sending}
            title={`不选就沿用当前配置；选了只改这一次运行的${NODE_TEXT[node]}`}
          />
        ))}
        <button
          className="ghost"
          onClick={onNewTask}
          disabled={sending}
          title="放弃这条待续跑，把目标 / 目录带到新任务表单里"
        >
          新建任务
        </button>
        {armed && (
          <button className="ghost" onClick={withdraw} disabled={sending}>
            撤回
          </button>
        )}
        <button
          className={`primary${armed ? ' armed' : ''}`}
          onClick={() => void confirm()}
          disabled={sending}
          title="要先确认，不会一点就跑"
        >
          {sending ? '启动中…' : armed ? '再点一次确认续跑' : '继续这个会话'}
        </button>
      </div>

      {hint && <div className="launch-hint">{hint}</div>}
      {armed && !hint && (
        <div className="rb-hint">
          {hasNote ? `先投递「${target === 'main' ? '主 agent' : 'worker'}」的引导，` : ''}
          再从上次的断点接着跑，已完成的任务不会重做；5 秒内没有再点会自动取消。
        </div>
      )}
    </div>
  );
}
