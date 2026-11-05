import { useEffect, useMemo, useRef, useState } from 'react';
import { pickDirectory } from '../../api';
import { modelOptions } from '../../ui';
import { KEY, readText, writeText } from '../../storage';
import { ComposerTextarea } from './ComposerTextarea';
import { ModelPicker } from './ModelPicker';
import type { CardProps } from './types';

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

/** 新任务：目标 + 产出目录 + worker / 验收模型 */
export function NewTaskCard({ meta, defaultWorkdir, preset, onStart }: CardProps<'new'>) {
  const [goal, setGoal] = useState('');
  const [workerModel, setWorkerModel] = useState('');
  const [verifierModel, setVerifierModel] = useState('');
  const [workdir, setWorkdir] = useState('');
  const [hint, setHint] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [picking, setPicking] = useState(false);

  const models = useMemo(() => modelOptions(meta), [meta]);

  // 上次用过的目录优先；设置保存时会同步更新，切到新的默认目录
  useEffect(() => {
    setWorkdir(readText(KEY.workdir) || defaultWorkdir || '');
  }, [defaultWorkdir]);

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

  // 记住上次选的模型；记的值已经不在候选里就退回配置值
  useEffect(() => {
    const pick = (key: string, fallback: string) => {
      const saved = readText(key);
      return saved !== null && models.includes(saved) ? saved : fallback;
    };
    setWorkerModel(pick(KEY.workerModel, meta.workerModel));
    setVerifierModel(pick(KEY.verifierModel, meta.verifierModel));
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
      workerModel,
      verifierModel,
      workdir: workdir.trim(),
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

      <div className="launch-opts">
        <ModelPicker
          label="worker"
          value={workerModel}
          options={models}
          onChange={(v) => {
            setWorkerModel(v);
            writeText(KEY.workerModel, v);
          }}
        />
        <ModelPicker
          label="验收"
          value={verifierModel}
          options={models}
          onChange={(v) => {
            setVerifierModel(v);
            writeText(KEY.verifierModel, v);
          }}
          title="建议和 worker 选不同的模型，判定才真正独立"
        />
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

