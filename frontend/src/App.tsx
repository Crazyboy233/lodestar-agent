import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react';
import { flushSync } from 'react-dom';
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
import { ReviewBar } from './components/ReviewBar';
import { ReviewView } from './components/ReviewView';
import { pickLaunchLine } from './ui';
import { withViewTransition } from './viewTransition';

const errText = (err: unknown) => (err instanceof Error ? err.message : String(err));

export default function App() {
  const run = useRunSession();
  const ui = useUiMode(run.session.state);
  const history = useHistory({ polling: run.session.state === 'running' });

  const [meta, setMeta] = useState<RunMeta>(EMPTY_META);
  const [workdir, setWorkdir] = useState('');
  const [preset, setPreset] = useState<Preset | null>(null);
  // 清屏页那一句开场白：进来先随机一句，每次点「新任务」再换一句
  const [launchLine, setLaunchLine] = useState(() => pickLaunchLine());
  // 调试入口：?cfg=1 直接打开设置窗口
  const [cfgOpen, setCfgOpen] = useState(
    () => new URLSearchParams(location.search).has('cfg'),
  );

  const { applySnapshot, adopt, requestStop } = run;
  const {
    refresh, load, clearDetail, remove: removeHistory,
    stream: recordStream, streamLoading, streamError, streamNote, loadStream,
  } = history;
  const { runStarted, showWelcome, focusRun, openThread, closeThread } = ui;
  const { openReview, closeReview } = ui;
  const prepareResumeUi = ui.prepareResume;

  // 初次加载：GET /state 铺底（session / status / meta / 产出目录）。
  // 只有还在跑的那一轮会被恢复；上一轮已经结束的话，主区域落在新任务页（见 applySnapshot）。
  useEffect(() => {
    fetchState()
      .then((snap) => {
        applySnapshot(snap);
        setMeta(snap.meta);
        setWorkdir(snap.workdir || snap.meta?.workdir || '');
      })
      .catch((err: unknown) => {
        applySnapshot(null);
        reportClient('初始 /state 失败：' + errText(err));
      });
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

  // data-view 决定主区域的布局模式（launch 居中 flex / run 与 review 两列 grid /
  // history 单列）。必须用 layout effect：开跑和新建任务都包在视图过渡里，浏览器拍
  // 「新快照」是在 flushSync 返回之前完成的；普通 effect 跑在那之后，快照会拍到旧的
  // 布局模式，于是动画按旧结构演、结束时再跳一下。
  useLayoutEffect(() => {
    document.body.dataset.view = ui.screen;
  }, [ui.screen]);

  const start = useCallback(async (p: StartPayload): Promise<string | null> => {
    const res = await runCommand({
      goal: p.goal,
      workdir: p.workdir,
      // 新建任务时三个节点都显式给出，避免「页面显示 A、后端用了 B」
      nodes: p.nodes,
    });
    if (!res.ok || !res.session) return res.error ?? '启动失败';
    const session = res.session;
    // 开跑这一刻整块布局都换了（居中 → 底部 + 右侧抽屉），
    // 一次提交里连带清掉欢迎态，再交给视图过渡补间：卡片沉底、抽屉滑出。
    withViewTransition(() => flushSync(() => {
      runStarted();
      adopt(session);
      if (session.workdir) setWorkdir(session.workdir);
    }));
    return null;
  }, [adopt, runStarted]);

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
      nodes: models,
    });
    if (!res.ok || !res.session) return res.error ?? '续跑失败';
    const session = res.session;
    // 续跑同样是一次布局切换；顺手清掉「待续跑」草稿，落定后直接是引导卡
    withViewTransition(() => flushSync(() => {
      runStarted();
      adopt(session);
    }));
    return null;
  }, [adopt, runStarted]);

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

  // 删掉一条历史：如果删的正是当前打开的那条，顺手把只读视图关掉，
  // 否则页面会停在一个已经不存在的会话上（标题还在，正文没了）。
  const deleteHistoryEntry = useCallback(async (thread: string): Promise<string | null> => {
    const fail = await removeHistory(thread);
    if (fail) return fail;
    if (ui.thread === thread) closeHistory();
    return null;
  }, [removeHistory, ui.thread, closeHistory]);

  // 从历史「继续这个会话」= 回到操作界面挂成待续跑，真正开跑要再确认一次
  const prepareResume = useCallback((item: HistoryDetail) => {
    prepareResumeUi(item);
    clearDetail();
  }, [prepareResumeUi, clearDetail]);

  // 历史记录 → 当时的操作页面：主区域换成信息流，布局跟运行页一样，只是只读
  const openRecordStream = useCallback((item: HistoryDetail) => {
    openReview();
    void loadStream(item.thread);
  }, [openReview, loadStream]);

  const useAsNew = useCallback((item: HistoryDetail) => {
    withViewTransition(() => flushSync(() => {
      setPreset({ goal: item.goal, workdir: item.workdir, n: Date.now() });
      showWelcome();
    }));
  }, [showWelcome]);

  const newTask = useCallback(() => {
    withViewTransition(() => flushSync(() => {
      setLaunchLine((prev) => pickLaunchLine(prev));
      showWelcome();
    }));
  }, [showWelcome]);

  // 保存设置后重新取一次 /state：来源清单、各节点当前值、默认目录都以服务端为准，
  // 不在前端拼第二份「配置 → meta」的映射（那种重复迟早对不上）。
  const onSaved = useCallback((_cfg: ConfigView) => {
    void fetchState().then((snap) => {
      setMeta(snap.meta);
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

  // 正常跑完就收起输入框：这时候没东西可输入了，再开新任务走左上角。
  // 但挂着「继续这个会话」的待续跑草稿时不能收，否则连确认框一起藏掉了。
  const showComposer = !(
    ui.screen === 'run' && run.session.state === 'done' && !ui.resume
  );

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
          onDelete={deleteHistoryEntry}
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
          {ui.screen === 'review' && (
            <ReviewBar
              detail={history.detail}
              onBack={closeReview}
              onClose={closeHistory}
            />
          )}
          <main>
            {ui.screen === 'history' && (
              <HistoryView
                detail={history.detail}
                loading={history.detailLoading}
                error={history.detailError}
                running={run.session.state === 'running'}
                onResume={prepareResume}
                onOpenStream={openRecordStream}
                onClose={closeHistory}
              />
            )}

            {/* 回看历史记录：运行页的布局，但只读、不接命令 */}
            {ui.screen === 'review' && (
              <ReviewView
                detail={history.detail}
                events={recordStream}
                loading={streamLoading}
                error={streamError}
                note={streamNote}
              />
            )}

            {(ui.screen === 'launch' || ui.screen === 'run') && (
              <>
                {ui.screen === 'launch' ? (
                  // 新任务 = 清屏：只留一句开场白，下面是输入卡
                  <section className="stream-wrap launch-blank">
                    <p className="launch-line" key={launchLine}>{launchLine}</p>
                  </section>
                ) : (
                  <Stream events={run.events} />
                )}

                {showComposer && (
                  <div className="composer-zone">
                    <Composer
                      variant={variant}
                      meta={meta}
                      defaultWorkdir={workdir || meta.workdir}
                      currentModels={{
                        main: run.session.nodes?.main?.label || meta.nodes.main.label,
                        worker: run.session.nodes?.worker?.label || meta.nodes.worker.label,
                        verifier: run.session.nodes?.verifier?.label || meta.nodes.verifier.label,
                      }}
                      preset={preset}
                      onStart={start}
                      onGuide={guide}
                      onResume={resume}
                      onNewTask={() => resumeDraft && useAsNew(resumeDraft)}
                    />
                  </div>
                )}

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
