import { useCallback, useEffect, useRef, useState } from 'react';
import type { ConfigView, HistoryDetail, RunMeta } from './types';
import { EMPTY_META } from './types';
import { command, fetchState, reportClient, runCommand } from './api';
import { KEY, writeText } from './storage';
import { useRunSession } from './hooks/useRunSession';
import { useHistory } from './hooks/useHistory';
import { useUiMode } from './hooks/useUiMode';
import { Header } from './components/Header';
import { Sidebar } from './components/Sidebar';
import { Workbar } from './components/Workbar';
import {
  Composer, type ComposerVariant, type Preset, type ResumeModels,
  type SeedNote, type StartPayload,
} from './components/Composer';
import { Stream } from './components/Stream';
import { TaskPanel } from './components/TaskPanel';
import { NoteList } from './components/NoteList';
import { QuitPanel } from './components/QuitPanel';
import { ConfigDialog } from './components/ConfigDialog';
import { HistoryView } from './components/HistoryView';

const errText = (err: unknown) => (err instanceof Error ? err.message : String(err));

export default function App() {
  const run = useRunSession();
  const ui = useUiMode(run.session.state);
  const history = useHistory({ polling: run.session.state === 'running' });

  const [meta, setMeta] = useState<RunMeta>(EMPTY_META);
  const [workdir, setWorkdir] = useState('');
  const [preset, setPreset] = useState<Preset | null>(null);
  // 调试入口：?cfg=1 直接打开设置窗口
  const [cfgOpen, setCfgOpen] = useState(
    () => new URLSearchParams(location.search).has('cfg'),
  );

  const { applySnapshot, adopt, requestStop } = run;
  const { refresh, load, clearDetail } = history;
  const { runStarted, showWelcome, focusRun, openThread, closeThread } = ui;
  const prepareResumeUi = ui.prepareResume;

  // 初次加载：GET /state 铺底（session / status / meta / 产出目录）
  useEffect(() => {
    fetchState()
      .then((snap) => {
        applySnapshot(snap);
        setMeta(snap.meta);
        setWorkdir(snap.workdir || snap.meta?.workdir || '');
      })
      .catch((err: unknown) => reportClient('初始 /state 失败：' + errText(err)));
  }, [applySnapshot]);

  // 运行状态的迁移只在这里收口：开跑就清掉临时视图状态，结束就刷新左侧历史。
  // （以前这段逻辑散在 start / resume / SSE / 三个入口里各写一遍）
  const prevRunState = useRef(run.session.state);
  useEffect(() => {
    const next = run.session.state;
    if (next === prevRunState.current) return;
    prevRunState.current = next;
    if (next === 'running') runStarted();
    if (next !== 'idle') void refresh();
  }, [run.session.state, runStarted, refresh]);

  useEffect(() => {
    document.body.dataset.view = ui.screen;
  }, [ui.screen]);

  const start = useCallback(async (p: StartPayload): Promise<string | null> => {
    const res = await runCommand({
      goal: p.goal,
      workerModel: p.workerModel,
      verifierModel: p.verifierModel,
      workdir: p.workdir,
    });
    if (!res.ok || !res.session) return res.error ?? '启动失败';
    adopt(res.session);
    if (res.session.workdir) setWorkdir(res.session.workdir);
    return null;
  }, [adopt]);

  const guide = useCallback(async (
    target: 'main' | 'worker', text: string,
  ): Promise<string | null> => {
    const res = await command({ type: 'guide', target, text });
    return res.ok ? null : (res.error ?? '发送失败');
  }, []);

  // 真续跑：模型字段缺省 = 沿用配置；引导跟 run 一起投递，第一个节点就能看到
  const resume = useCallback(async (
    item: HistoryDetail, note: SeedNote | null, models: ResumeModels,
  ): Promise<string | null> => {
    const res = await runCommand({
      goal: item.goal,
      thread: item.thread,
      resume: true,
      workdir: item.workdir || undefined,
      seedNotes: note ? [note] : undefined,
      workerModel: models.worker,
      verifierModel: models.verifier,
    });
    if (!res.ok || !res.session) return res.error ?? '续跑失败';
    adopt(res.session);
    return null;
  }, [adopt]);

  const openHistory = useCallback((thread: string) => {
    // 正在跑的这一条没有「只读历史」可看：它的界面就是当前操作界面
    if (run.session.state === 'running' && thread === run.session.thread) {
      focusRun();
      return;
    }
    openThread(thread);
    void load(thread);
  }, [run.session.state, run.session.thread, focusRun, openThread, load]);

  const closeHistory = useCallback(() => {
    closeThread();
    clearDetail();
  }, [closeThread, clearDetail]);

  // 从历史「继续这个会话」= 回到操作界面挂成待续跑，真正开跑要再确认一次
  const prepareResume = useCallback((item: HistoryDetail) => {
    prepareResumeUi(item);
    clearDetail();
  }, [prepareResumeUi, clearDetail]);

  const useAsNew = useCallback((item: HistoryDetail) => {
    setPreset({ goal: item.goal, workdir: item.workdir, n: Date.now() });
    showWelcome();
  }, [showWelcome]);

  const newTask = useCallback(() => showWelcome(), [showWelcome]);

  const onSaved = useCallback((cfg: ConfigView) => {
    setMeta((prev) => ({
      ...prev,
      models: cfg.ui.models.split('\n').map((s) => s.trim()).filter(Boolean),
      workerModel: String(cfg.worker.model ?? ''),
      verifierModel: String(cfg.verifier.model ?? ''),
    }));
    void fetchState().then((snap) => {
      const wd = snap.workdir || snap.meta?.workdir || '';
      setWorkdir(wd);
      // 设置里改了「默认产出目录」：顺手更新首页记住的目录，
      // 否则保存完还停在旧目录，看起来像没生效。
      if (wd) writeText(KEY.workdir, wd);
    }).catch(() => undefined);
  }, []);

  const variant: ComposerVariant = ui.composer === 'guide'
    ? {
      kind: 'guide',
      locked: run.status.state === 'draining' || run.drainStart !== null,
      pending: run.status.pending,
    }
    : (ui.composer === 'resume' && ui.resume)
      ? { kind: 'resume', item: ui.resume }
      : { kind: 'new' };

  const resumeDraft = ui.resume;

  return (
    <>
      <div className="app">
        <Sidebar
          projects={history.groups.projects}
          total={history.groups.total}
          loading={history.loading}
          error={history.groups.error}
          selected={ui.thread}
          currentThread={run.session.state === 'running' ? run.session.thread : null}
          busy={run.session.state === 'running'}
          connected={run.connected}
          onNew={newTask}
          onOpenSettings={() => setCfgOpen(true)}
          onSelect={openHistory}
        />

        <div className="main-col">
          <Header
            session={run.session}
            status={run.status}
            elapsed={run.elapsed}
            drainStart={run.drainStart}
            history={history.detail}
            running={run.session.state === 'running'}
          />
          {ui.screen === 'run' && (
            <Workbar status={run.status} sessionState={run.session.state} />
          )}
          <main>
            {ui.screen === 'history' ? (
              <HistoryView
                detail={history.detail}
                loading={history.detailLoading}
                error={history.detailError}
                running={run.session.state === 'running'}
                onResume={prepareResume}
                onUseAsNew={useAsNew}
                onClose={closeHistory}
              />
            ) : (
              <>
                {ui.screen === 'launch' ? (
                  <section className="stream-wrap welcome-wrap">
                    <div className="welcome">
                      <div className="welcome-mark">Lodestar</div>
                      <p>给一个目标，主 agent 拆解 → worker 执行 → 独立验收。</p>
                      <span>任务开始后，执行过程会打印在这里。</span>
                    </div>
                  </section>
                ) : (
                  <Stream events={run.events} />
                )}

                <div className="composer-zone">
                  <Composer
                    variant={variant}
                    meta={meta}
                    defaultWorkdir={workdir || meta.workdir}
                    currentModels={{
                      worker: run.session.workerModel || meta.workerModel,
                      verifier: run.session.verifierModel || meta.verifierModel,
                    }}
                    preset={preset}
                    onStart={start}
                    onGuide={guide}
                    onResume={resume}
                    onNewTask={() => resumeDraft && useAsNew(resumeDraft)}
                  />
                </div>

                {/* 侧栏只在运行界面出现：欢迎页没有任务清单可看 */}
                {ui.screen === 'run' && (
                  <aside>
                    <TaskPanel tasks={run.tasks} summary={run.summary} />
                    <NoteList notes={run.notes} />
                    <QuitPanel
                      status={run.status}
                      session={run.session}
                      events={run.events}
                      drainStart={run.drainStart}
                      onStop={requestStop}
                    />
                  </aside>
                )}
              </>
            )}
          </main>
        </div>
      </div>

      <ConfigDialog
        open={cfgOpen}
        onClose={() => setCfgOpen(false)}
        onSaved={onSaved}
      />
    </>
  );
}
