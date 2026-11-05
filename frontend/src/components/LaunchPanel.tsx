import { useEffect, useMemo, useState } from 'react';
import type { RunMeta } from '../types';
import { pickDirectory } from '../api';
import { modelOptions } from '../ui';

export interface StartPayload {
  goal: string;
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
  /** 运行中把底部输入当引导用：按收件人投递，下一个节点边界生效 */
  onGuide: (target: 'main' | 'worker', text: string) => Promise<string | null>;
  /** 已经点了边界退出 / 正在收尾：不再接受新的引导 */
  guideLocked?: boolean;
  /** 各收件人还排着几条没注入 */
  pending?: Record<string, number>;
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
  meta, running, defaultWorkdir, preset, onStart, onGuide, guideLocked = false, pending,
}: Props) {
  const [goal, setGoal] = useState('');
  const [workerModel, setWorkerModel] = useState('');
  const [verifierModel, setVerifierModel] = useState('');
  const [workdir, setWorkdir] = useState('');
  const [hint, setHint] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [picking, setPicking] = useState(false);
  // 运行中：底部输入切成「引导」，投给哪个节点在按钮上选
  const [target, setTarget] = useState<'main' | 'worker'>('main');
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

  // 空串 = 「默认（由 CLI 决定）」，永远放第一个，见 ui.modelOptions
  const models = useMemo(() => modelOptions(meta), [meta]);

  // 运行状态切换时清空引导草稿和上次的提示，避免把上一轮的残留带到下一轮
  useEffect(() => {
    setNote('');
    setSent('');
    setHint('');
    setSending(false);
    setTarget('main');
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
    if (!goal.trim()) {
      setHint('先写个目标');
      return;
    }
    setSubmitting(true);
    setHint('');
    if (workdir.trim()) localStorage.setItem('lodestar.workdir', workdir.trim());
    const err = await onStart({
      goal: goal.trim(),
      workerModel,
      verifierModel,
      workdir: workdir.trim(),
    });
    setSubmitting(false);
    if (err) {
      setHint(err);
      return;
    }
    // 启动成功后清掉目标：任务结束后底部恢复成空的新任务框，
    // 不会把上一次的目标再提交一遍
    setGoal('');
  };

  const sendGuide = async () => {
    if (!note.trim() || sending) return;
    setSending(true);
    setHint('');
    setSent('');
    const err = await onGuide(target, note.trim());
    setSending(false);
    if (err) {
      setHint(err);
      return;
    }
    setNote('');
    setSent(`已投递给${target === 'main' ? '主 agent' : 'worker'}，会在下一个节点边界生效`);
  };

  const queued = pending
    ? [
      pending.main ? `主 agent ${pending.main}` : '',
      pending.worker ? `worker ${pending.worker}` : '',
    ].filter(Boolean).join(' · ')
    : '';

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
            : target === 'main'
              ? '给主 agent 的引导：下一个节点边界生效，不打断当前这一轮'
              : '给 worker 的引导：它下一次被调用时生效，不打断当前这一轮'}
        />
        <div className="launch-opts">
          <div className="guide-targets" role="group" aria-label="引导投给哪个节点">
            {(['main', 'worker'] as const).map((t) => (
              <button
                key={t}
                type="button"
                className={target === t ? 'on' : ''}
                disabled={guideLocked}
                onClick={() => setTarget(t)}
              >
                {t === 'main' ? '主 agent' : 'worker'}
                {pending?.[t] ? <span className="n">{pending[t]}</span> : null}
              </button>
            ))}
          </div>
          <span className="composer-hint">
            {guideLocked
              ? '正在等待当前这一轮结束，然后退出'
              : (queued ? `已排队 ${queued}` : '⌘ / Ctrl + ↵ 发送')}
          </span>
          <button
            className="primary"
            disabled={guideLocked || !note.trim() || sending}
            onClick={() => void sendGuide()}
          >
            {sending ? '发送中…' : '投递引导'}
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
