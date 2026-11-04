import { useEffect, useMemo, useState } from 'react';
import type { RunMeta } from '../types';
import { pickDirectory } from '../api';

export interface StartPayload {
  goal: string;
  thread: string;
  resume: boolean;
  workerModel: string;
  verifierModel: string;
  workdir: string;
}

interface Props {
  meta: RunMeta;
  running: boolean;
  /** 配置里的默认产出目录 */
  defaultWorkdir: string;
  /** 从历史「在此目录新建任务」带过来的预填值 */
  preset?: { goal: string; workdir: string; n: number } | null;
  onStart: (payload: StartPayload) => Promise<string | null>;
  /** 运行中把底部输入当引导用：发给主 agent，下一个节点边界生效 */
  onGuide: (text: string) => Promise<string | null>;
  /** 已经点了边界退出 / 正在收尾：不再接受新的引导 */
  guideLocked?: boolean;
}

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

export function LaunchPanel({
  meta, running, defaultWorkdir, preset, onStart, onGuide, guideLocked = false,
}: Props) {
  const [goal, setGoal] = useState('');
  const [thread, setThread] = useState('');
  const [resume, setResume] = useState(false);
  const [workerModel, setWorkerModel] = useState('');
  const [verifierModel, setVerifierModel] = useState('');
  const [workdir, setWorkdir] = useState('');
  const [hint, setHint] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [picking, setPicking] = useState(false);
  // 运行中：底部输入切成「给主 agent 的引导」
  const [note, setNote] = useState('');
  const [sending, setSending] = useState(false);
  const [sent, setSent] = useState('');

  // 上次用过的目录优先；设置保存时会同步更新 localStorage，切到新的默认目录
  useEffect(() => {
    const saved = localStorage.getItem('lodestar.workdir');
    setWorkdir(saved || defaultWorkdir || '');
  }, [defaultWorkdir]);

  // 从历史「在此目录新建任务」带过来的目标与目录
  useEffect(() => {
    if (!preset) return;
    setGoal(preset.goal || '');
    if (preset.workdir) {
      setWorkdir(preset.workdir);
      localStorage.setItem('lodestar.workdir', preset.workdir);
    }
  }, [preset]);

  const models = useMemo(() => {
    // 空串 = 「默认（由 CLI 决定）」，永远放在第一个：
    // 配置里的候选列表不一定包含它，但页面上必须能选回默认。
    const base = [''];
    for (const m of meta.models ?? []) {
      if (m && !base.includes(m)) base.push(m);
    }
    // 配置里写了列表外的自定义模型也要能显示，别被静默改成「默认」
    for (const v of [meta.workerModel, meta.verifierModel]) {
      if (v && !base.includes(v)) base.push(v);
    }
    return base;
  }, [meta.models, meta.workerModel, meta.verifierModel]);

  // 运行状态切换时清空引导草稿和上次的提示，避免把上一轮的残留带到下一轮
  useEffect(() => {
    setNote('');
    setSent('');
    setHint('');
    setSending(false);
  }, [running]);

  useEffect(() => {
    const pick = (key: string, fallback: string) => {
      const saved = localStorage.getItem(key);
      return saved !== null && models.includes(saved) ? saved : fallback;
    };
    setWorkerModel(pick('lodestar.workerModel', meta.workerModel));
    setVerifierModel(pick('lodestar.verifierModel', meta.verifierModel));
  }, [models, meta.workerModel, meta.verifierModel]);

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
      localStorage.setItem('lodestar.workdir', res.path);
    } catch (err) {
      setHint('打开文件夹选择器失败：' + (err instanceof Error ? err.message : String(err)));
    } finally {
      setPicking(false);
    }
  };

  const submit = async () => {
    if (picking) return;
    if (!goal.trim() && !resume) {
      setHint('先写个目标，或填 thread 并勾选续跑');
      return;
    }
    setSubmitting(true);
    setHint('');
    if (workdir.trim()) localStorage.setItem('lodestar.workdir', workdir.trim());
    const err = await onStart({
      goal: goal.trim(),
      thread: thread.trim(),
      resume,
      workerModel,
      verifierModel,
      workdir: workdir.trim(),
    });
    setSubmitting(false);
    if (err) {
      setHint(err);
      return;
    }
    // 启动成功后清掉这一轮输入：任务结束后底部恢复成空的新任务框，
    // 不会把上一次的目标/thread 再提交一遍
    setGoal('');
    setThread('');
    setResume(false);
  };

  const sendGuide = async () => {
    if (!note.trim() || sending) return;
    setSending(true);
    setHint('');
    setSent('');
    const err = await onGuide(note.trim());
    setSending(false);
    if (err) {
      setHint(err);
      return;
    }
    setNote('');
    setSent('已投递，会在下一个节点边界生效');
  };

  // 运行中：同一块圆角输入区，但只发引导，不显示目录 / 模型这些启动参数
  if (running) {
    return (
      <div className="launch-card composer-card running-card">
        <textarea
          value={note}
          onChange={(e) => {
            setNote(e.target.value);
            setSent('');
          }}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) {
              e.preventDefault();
              void sendGuide();
            }
          }}
          disabled={guideLocked}
          placeholder={guideLocked
            ? '正在收尾，已停止接受新的引导'
            : '给主 agent 投递引导：下一个节点生效，不打断当前这一轮'}
        />
        <div className="launch-opts">
          <span className="composer-hint">
            {guideLocked
              ? '正在等待当前这一轮结束，然后退出'
              : 'worker 的引导和待投递记录在右侧「引导」面板'}
          </span>
          <button
            className="primary"
            disabled={guideLocked || !note.trim() || sending}
            onClick={() => void sendGuide()}
          >
            {sending ? '发送中…' : '发送引导'}
          </button>
        </div>
        {hint && <div className="launch-hint">{hint}</div>}
        {sent && <div className="launch-ok">{sent}</div>}
      </div>
    );
  }

  return (
    <div className="launch-card composer-card">
      <textarea
        value={goal}
        onChange={(e) => setGoal(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) {
            e.preventDefault();
            void submit();
          }
        }}
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
            localStorage.setItem('lodestar.workdir', defaultWorkdir);
          }}
        >
          默认
        </button>
      </div>

      <div className="launch-opts">
        <input
          type="text"
          value={thread}
          onChange={(e) => setThread(e.target.value)}
          placeholder="thread（留空自动生成）"
        />
        <label className="chk">
          <input type="checkbox" checked={resume} onChange={(e) => setResume(e.target.checked)} />
          续跑
        </label>
      </div>

      <div className="launch-opts">
        <label className="sel">
          <span>worker</span>
          <select
            value={workerModel}
            onChange={(e) => {
              setWorkerModel(e.target.value);
              localStorage.setItem('lodestar.workerModel', e.target.value);
            }}
          >
            {models.map((m) => (
              <option key={m || '__default'} value={m}>{m || '默认（CLI 决定）'}</option>
            ))}
          </select>
        </label>
        <label className="sel" title="建议和 worker 选不同的模型，判定才真正独立">
          <span>验收</span>
          <select
            value={verifierModel}
            onChange={(e) => {
              setVerifierModel(e.target.value);
              localStorage.setItem('lodestar.verifierModel', e.target.value);
            }}
          >
            {models.map((m) => (
              <option key={m || '__default'} value={m}>{m || '默认（CLI 决定）'}</option>
            ))}
          </select>
        </label>
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
