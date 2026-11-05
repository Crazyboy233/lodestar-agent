import { useEffect, useMemo, useState } from 'react';
import type { HistoryDetail, RunMeta } from '../types';
import { STATE_TEXT, modelOptions } from '../ui';

/** 下拉里的「沿用当前配置」：不传这个字段，等价于后端拿 None（不动 cfg） */
const KEEP = '__keep';

/** 开跑前写好的引导：跟着这次续跑一起投递，在第一个节点边界被取走 */
export interface SeedNote {
  target: 'main' | 'worker';
  text: string;
}

/** 这次续跑要覆盖的模型；字段缺省 = 沿用当前配置 */
export interface ResumeModels {
  worker?: string;
  verifier?: string;
}

interface Props {
  item: HistoryDetail;
  meta: RunMeta;
  /** 当前配置里实际生效的模型（空串 = CLI 决定），用来把「沿用」写成具体名字 */
  current: { worker: string; verifier: string };
  /** 有任务在跑：一次只能跑一个，按钮先禁用 */
  running: boolean;
  onResume: (
    item: HistoryDetail,
    guide: SeedNote | null,
    models: ResumeModels,
  ) => Promise<string | null>;
  /** 不想续了，改成在这里新建任务（把目标 / 目录带回新任务表单） */
  onNewTask: () => void;
}

/**
 * 「准备续跑」卡片：从历史点『继续这个会话』只是回到操作界面并把这条挂在这里。
 *
 * 它代替了输入框的位置，原因有两个：
 *   1. 还没开跑时，引导投不进任何地方（`Runner.start` 里的 reset 会清空信箱），
 *      所以这里收的引导会跟续跑命令一起送到后端，在 reset 之后立刻补投；
 *   2. 新任务表单放在这里会和续跑打架 —— 目标框点「开始」开的是新任务。
 * 真正开跑仍然要点两次（armed 二次确认）。
 */
export function ResumePanel({ item, meta, current, running, onResume, onNewTask }: Props) {
  const [target, setTarget] = useState<'main' | 'worker'>('main');
  const [note, setNote] = useState('');
  const [workerModel, setWorkerModel] = useState(KEEP);
  const [verifierModel, setVerifierModel] = useState(KEEP);
  const [armed, setArmed] = useState(false);
  const [hint, setHint] = useState('');
  const [sending, setSending] = useState(false);

  // 确认态几秒后自己撤销：避免很久以后再点一下就直接开跑
  useEffect(() => {
    if (!armed) return;
    const id = window.setTimeout(() => setArmed(false), 5000);
    return () => window.clearTimeout(id);
  }, [armed]);

  const models = useMemo(() => modelOptions(meta), [meta]);

  const click = async () => {
    if (running || sending) return;
    if (!armed) {
      setArmed(true);
      setHint('');
      return;
    }
    setArmed(false);
    setSending(true);
    const text = note.trim();
    // KEEP 就不带这个字段：后端收到 None 才叫「沿用配置」，
    // 传空串是另一种语义（显式用 CLI 默认模型）
    const err = await onResume(
      item,
      text ? { target, text } : null,
      {
        worker: workerModel === KEEP ? undefined : workerModel,
        verifier: verifierModel === KEEP ? undefined : verifierModel,
      },
    );
    setSending(false);
    if (err) setHint(err);
  };

  // 这条待续跑是用户自己点「继续这个会话」带过来的，所以不提供「收掉」的入口；
  // 只有确认态能撤回这次确认（否则一旦点了确认，就只能干等 5 秒自动撤销）。
  const withdraw = () => {
    if (sending) return;
    setArmed(false);
    setHint('');
  };

  const hasNote = Boolean(note.trim());

  return (
    <div className={`launch-card composer-card resume-card${armed ? ' armed' : ''}`}>
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

      <textarea
        value={note}
        onChange={(e) => {
          setNote(e.target.value);
          setHint('');
        }}
        onKeyDown={(e) => {
          if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) {
            e.preventDefault();
            void click();
          }
        }}
        disabled={sending}
        placeholder={target === 'main'
          ? '可选：给续跑的第一个规划调用留句话（会跟着这次续跑一起投递）'
          : '可选：给续跑后的第一条任务留句话（会跟着这次续跑一起投递）'}
      />

      <div className="launch-opts">
        <div className="guide-targets" role="group" aria-label="引导投给哪个节点">
          {(['main', 'worker'] as const).map((t) => (
            <button
              key={t}
              type="button"
              className={target === t ? 'on' : ''}
              disabled={sending}
              onClick={() => setTarget(t)}
            >
              {t === 'main' ? '主 agent' : 'worker'}
            </button>
          ))}
        </div>
        <span className="composer-hint">
          {hasNote
            ? '开跑时先投这段引导，再做第一次调用'
            : '不写也可以，直接续跑'}
        </span>
      </div>

      <div className="launch-opts">
        <label className="sel" title="不选就沿用当前配置；选了只改这一次运行">
          <span>worker</span>
          <select
            value={workerModel}
            onChange={(e) => setWorkerModel(e.target.value)}
            disabled={sending}
          >
            <option value={KEEP} title="沿用当前配置，不改动 cfg">
              沿用 · {current.worker || 'CLI 决定'}
            </option>
            {models.map((m) => (
              <option key={m || '__default'} value={m}>{m || '默认（CLI 决定）'}</option>
            ))}
          </select>
        </label>
        <label className="sel" title="建议和 worker 选不同的模型，判定才真正独立">
          <span>验收</span>
          <select
            value={verifierModel}
            onChange={(e) => setVerifierModel(e.target.value)}
            disabled={sending}
          >
            <option value={KEEP} title="沿用当前配置，不改动 cfg">
              沿用 · {current.verifier || 'CLI 决定'}
            </option>
            {models.map((m) => (
              <option key={m || '__default'} value={m}>{m || '默认（CLI 决定）'}</option>
            ))}
          </select>
        </label>
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
          onClick={() => void click()}
          disabled={running || sending}
          title={running ? '任务运行中，等它结束再续跑' : '要先确认，不会一点就跑'}
        >
          {sending ? '启动中…' : armed ? '再点一次确认续跑' : '继续这个会话'}
        </button>
      </div>

      {hint && <div className="launch-hint">{hint}</div>}
      {armed && !hint && (
        <div className="rb-hint">
          {hasNote
            ? `先投递「${target === 'main' ? '主 agent' : 'worker'}」的引导，`
            : ''}
          再从上次的断点接着跑，已完成的任务不会重做；5 秒内没有再点会自动取消。
        </div>
      )}
    </div>
  );
}
