import { useCallback, useEffect, useState } from 'react';
import type {
  ConfigView, HistoryDetail, HistoryGroups, LogEvent, NoteItem, RunMeta,
  ServerEvent, Session, Status, TaskItem, TaskSummary,
} from './types';
import { EMPTY_META, EMPTY_SESSION, EMPTY_STATUS } from './types';
import {
  command, fetchHistory, fetchHistoryDetail, fetchState, reportClient,
} from './api';
import { useStream } from './hooks/useStream';
import { Header } from './components/Header';
import { Sidebar } from './components/Sidebar';
import { Workbar } from './components/Workbar';
import { LaunchPanel, type StartPayload } from './components/LaunchPanel';
import { Stream } from './components/Stream';
import { TaskPanel } from './components/TaskPanel';
import { GuidePanel } from './components/GuidePanel';
import { QuitPanel } from './components/QuitPanel';
import { ConfigDialog } from './components/ConfigDialog';
import { HistoryView } from './components/HistoryView';
import { ResumePanel, type ResumeModels, type SeedNote } from './components/ResumePanel';

const EMPTY_SUMMARY: TaskSummary = { total: 0, done: 0, passed: 0, failed: 0 };
const EMPTY_HISTORY: HistoryGroups = { ok: true, total: 0, projects: [] };

type View = 'launch' | 'run' | 'history';

export default function App() {
  const [session, setSession] = useState<Session>(EMPTY_SESSION);
  const [status, setStatus] = useState<Status>(EMPTY_STATUS);
  const [meta, setMeta] = useState<RunMeta>(EMPTY_META);
  const [workdir, setWorkdir] = useState('');
  const [events, setEvents] = useState<LogEvent[]>([]);
  const [tasks, setTasks] = useState<TaskItem[]>([]);
  const [summary, setSummary] = useState<TaskSummary>(EMPTY_SUMMARY);
  const [notes, setNotes] = useState<NoteItem[]>([]);
  const [elapsed, setElapsed] = useState({ base: 0, at: Date.now() });
  const [drainStart, setDrainStart] = useState<number | null>(null);
  const [forceLaunch, setForceLaunch] = useState(false);
  const [preset, setPreset] = useState<{ goal: string; workdir: string; n: number } | null>(null);
  // 调试入口：?cfg=1 直接打开设置窗口
  const [cfgOpen, setCfgOpen] = useState(
    () => new URLSearchParams(location.search).has('cfg'),
  );

  const [history, setHistory] = useState<HistoryGroups>(EMPTY_HISTORY);
  const [historyLoading, setHistoryLoading] = useState(true);
  const [selected, setSelected] = useState<string | null>(null);
  // 「准备续跑」的会话：从历史点进来先挂在这里，确认之后才真正开跑
  const [resumeDraft, setResumeDraft] = useState<HistoryDetail | null>(null);
  const [detail, setDetail] = useState<HistoryDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailError, setDetailError] = useState('');

  const refreshHistory = useCallback(async () => {
    try {
      const data = await fetchHistory();
      setHistory(data);
    } catch (err) {
      reportClient('读取历史失败：' + (err instanceof Error ? err.message : String(err)));
    } finally {
      setHistoryLoading(false);
    }
  }, []);

  useEffect(() => { void refreshHistory(); }, [refreshHistory]);

  // 运行中定时刷新：新会话是后台线程写 history.json 的，开始那一刻可能还没落盘，
  // 不补这一次，左侧要等任务跑完才冒出「运行中」的会话。
  useEffect(() => {
    if (session.state !== 'running') return;
    const first = window.setTimeout(() => void refreshHistory(), 1200);
    const timer = window.setInterval(() => void refreshHistory(), 6000);
    return () => {
      window.clearTimeout(first);
      window.clearInterval(timer);
    };
  }, [session.state, refreshHistory]);

  const onEvent = useCallback((ev: ServerEvent) => {
    switch (ev.type) {
      case 'log':
        setEvents((prev) => {
          const next = [...prev, {
            seq: ev.seq, ts: ev.ts, kind: ev.kind, text: ev.text,
            hr: ev.meta?.hr, role: ev.meta?.role,
          }];
          return next.length > 2500 ? next.slice(next.length - 2500) : next;
        });
        break;
      case 'status':
        setStatus(ev as unknown as Status);
        setElapsed({ base: ev.elapsed, at: Date.now() });
        break;
      case 'session':
        setSession((prev) => ({
          ...prev,
          state: ev.state,
          goal: ev.goal ?? prev.goal,
          thread: ev.thread ?? prev.thread,
          code: ev.code ?? prev.code,
          error: ev.error ?? prev.error,
          workdir: ev.workdir ?? prev.workdir,
        }));
        if (ev.workdir) setWorkdir(ev.workdir);
        if (ev.state === 'running') {
          setForceLaunch(false);
          setDrainStart(null);
          setSelected(null);
          setResumeDraft(null);
          void refreshHistory();
        } else {
          void refreshHistory();
        }
        break;
      case 'tasks':
        setTasks(ev.tasks);
        setSummary(ev.summary);
        break;
      case 'note':
        setNotes((prev) => [...prev.filter((n) => n.id !== ev.note.id), ev.note]);
        break;
      case 'note_consumed':
        setNotes((prev) => prev.map((n) => (
          ev.ids.includes(n.id) ? { ...n, consumed: true } : n
        )));
        break;
      case 'reset':
        // 旧事件已经由 useStream 按 seq 过滤掉，这里只清界面状态
        setEvents([]);
        setTasks([]);
        setNotes([]);
        setSummary(EMPTY_SUMMARY);
        setDrainStart(null);
        break;
      case 'stop':
        setDrainStart(Date.now());
        break;
      default:
        break;
    }
  }, [refreshHistory]);

  const { connected } = useStream(onEvent);

  useEffect(() => {
    fetchState()
      .then((snap) => {
        setSession(snap.session);
        setStatus(snap as unknown as Status);
        setMeta(snap.meta);
        setWorkdir(snap.workdir || snap.meta?.workdir || '');
        setElapsed({ base: snap.elapsed ?? 0, at: Date.now() });
        if (snap.stop_requested) {
          setDrainStart(Date.now() - (snap.waited ?? 0) * 1000);
        }
      })
      .catch((err: unknown) => {
        reportClient('初始 /state 失败：' + (err instanceof Error ? err.message : String(err)));
      });
  }, []);

  const view: View = selected
    ? 'history'
    : (forceLaunch || session.state === 'idle' ? 'launch' : 'run');

  useEffect(() => {
    document.body.dataset.view = view;
  }, [view]);

  const start = useCallback(async (payload: StartPayload): Promise<string | null> => {
    const res = await command({
      type: 'run',
      goal: payload.goal,
      worker_model: payload.workerModel,
      verifier_model: payload.verifierModel,
      workdir: payload.workdir || null,
    });
    if (!res.ok || !res.session) return res.error ?? '启动失败';
    setSession(res.session);
    if (res.session.workdir) setWorkdir(res.session.workdir);
    setForceLaunch(false);
    setSelected(null);
    setResumeDraft(null);
    void refreshHistory();
    return null;
  }, [refreshHistory]);

  const sendNote = useCallback(async (
    target: 'main' | 'worker', text: string,
  ): Promise<string | null> => {
    const res = await command({ type: 'guide', target, text });
    if (!res.ok) return res.error ?? '发送失败';
    return null;
  }, []);

  const stop = useCallback(async (): Promise<string | null> => {
    const res = await command({ type: 'stop' });
    if (!res.ok) return res.error ?? '请求失败';
    setDrainStart(Date.now());
    return null;
  }, []);

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
      try {
        if (wd) localStorage.setItem('lodestar.workdir', wd);
      } catch {
        /* 隐私模式下写不进去就算了 */
      }
    }).catch(() => undefined);
  }, []);

  const openHistory = useCallback(async (thread: string) => {
    // 正在跑的这一条没有「只读历史」可看：它的界面就是当前操作界面。
    // 点它必须回到运行视图，否则会卡在历史预览里，回不到输入框和事件流。
    if (session.state === 'running' && thread === session.thread) {
      setSelected(null);
      setDetail(null);
      setDetailError('');
      return;
    }
    setSelected(thread);
    setDetail(null);
    setDetailError('');
    setDetailLoading(true);
    try {
      const res = await fetchHistoryDetail(thread);
      if (!res.ok || !res.item) throw new Error(res.error || '读取失败');
      setDetail(res.item);
    } catch (err) {
      setDetailError('读取会话失败：' + (err instanceof Error ? err.message : String(err)));
    } finally {
      setDetailLoading(false);
    }
  }, [session.state, session.thread]);

  const closeHistory = useCallback(() => {
    setSelected(null);
    setDetail(null);
    setDetailError('');
  }, []);

  // 从历史「继续这个会话」= 只回到操作界面，把这条挂成待续跑，不立即开跑。
  // 真正启动在操作页面下方的续跑条里点，且要点两次（二次确认）。
  const prepareResume = useCallback((item: HistoryDetail) => {
    setResumeDraft(item);
    setSelected(null);
    setDetail(null);
    setDetailError('');
    setForceLaunch(false);
    setDetailLoading(false);
  }, []);

  // 真续跑：不传模型，沿用当前配置（避免把「默认」误当成显式覆盖）。
  // guide 是开跑前写好的引导，跟 run 一起送过去 —— 后端在 reset 之后立刻补投，
  // 所以续跑的第一个节点就能看到它。
  const resumeFromHistory = useCallback(async (
    item: HistoryDetail, guide: SeedNote | null, models: ResumeModels,
  ): Promise<string | null> => {
    setDetailError('');
    const payload: Record<string, unknown> = {
      type: 'run',
      goal: item.goal,
      thread: item.thread,
      resume: true,
      workdir: item.workdir || null,
      seed_notes: guide ? [guide] : [],
    };
    // 字段缺省才叫「沿用配置」；传空串是显式指定 CLI 默认模型，两种语义别混
    if (models.worker !== undefined) payload.worker_model = models.worker;
    if (models.verifier !== undefined) payload.verifier_model = models.verifier;
    const res = await command(payload);
    if (!res.ok || !res.session) return res.error ?? '续跑失败';
    setSession(res.session);
    setSelected(null);
    setResumeDraft(null);
    setForceLaunch(false);
    void refreshHistory();
    return null;
  }, [refreshHistory]);

  const useAsNew = useCallback((item: HistoryDetail) => {
    setPreset({ goal: item.goal, workdir: item.workdir, n: Date.now() });
    setSelected(null);
    setResumeDraft(null);
    setForceLaunch(true);
  }, []);

  const newTask = useCallback(() => {
    setSelected(null);
    setResumeDraft(null);
    setForceLaunch(true);
  }, []);

  return (
    <>
      <div className="app">
        <Sidebar
          projects={history.projects}
          total={history.total}
          loading={historyLoading}
          error={history.error}
          selected={selected}
          currentThread={session.state === 'running' ? session.thread : null}
          busy={session.state === 'running'}
          connected={connected}
          onNew={newTask}
          onOpenSettings={() => setCfgOpen(true)}
          onSelect={(thread) => void openHistory(thread)}
        />

        <div className="main-col">
          <Header
            session={session}
            status={status}
            elapsed={elapsed}
            drainStart={drainStart}
            history={detail}
            running={session.state === 'running'}
          />
          <Workbar status={status} sessionState={session.state} />
          <main>
            {view === 'history' ? (
              <HistoryView
                detail={detail}
                loading={detailLoading}
                error={detailError}
                running={session.state === 'running'}
                onResume={prepareResume}
                onUseAsNew={useAsNew}
                onClose={closeHistory}
              />
            ) : (
              <>
                {view === 'launch' ? (
                  <section className="stream-wrap welcome-wrap">
                    <div className="welcome">
                      <div className="welcome-mark">Lodestar</div>
                      <p>给一个目标，主 agent 拆解 → worker 执行 → 独立验收。</p>
                      <span>任务开始后，执行过程会打印在这里。</span>
                    </div>
                  </section>
                ) : (
                  <Stream events={events} />
                )}

                <div className="composer-zone">
                  {/* 有待续跑的会话时，这里不摆新任务表单：
                      没开跑之前的输入框既开不了新任务（会和续跑打架），也发不出引导
                      （引导是投进运行中的实例，开始运行那一刻会被 reset 清空）。
                      跑起来之后才切成引导框。 */}
                  {resumeDraft ? (
                    <ResumePanel
                      item={resumeDraft}
                      meta={meta}
                      /* 上次运行用过的模型比启动时的 meta 更新：Runner.start 会改 cfg */
                      current={{
                        worker: session.workerModel || meta.workerModel,
                        verifier: session.verifierModel || meta.verifierModel,
                      }}
                      running={session.state === 'running'}
                      onResume={resumeFromHistory}
                      onNewTask={() => useAsNew(resumeDraft)}
                    />
                  ) : (
                    <LaunchPanel
                      meta={meta}
                      running={session.state === 'running'}
                      defaultWorkdir={workdir || meta.workdir}
                      preset={preset}
                      onStart={start}
                      onGuide={sendNote}
                      guideLocked={status.state === 'draining' || drainStart !== null}
                      pending={status.pending}
                    />
                  )}
                </div>

                <aside>
                  <TaskPanel tasks={tasks} summary={summary} />
                  <GuidePanel notes={notes} />
                  <QuitPanel
                    status={status}
                    session={session}
                    events={events}
                    drainStart={drainStart}
                    onStop={stop}
                  />
                </aside>
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
