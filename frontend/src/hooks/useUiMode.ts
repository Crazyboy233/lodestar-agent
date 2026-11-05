import { useCallback, useState } from 'react';
import type { HistoryDetail, SessionState } from '../types';

/** 主区域显示什么 */
export type Screen = 'launch' | 'run' | 'history' | 'review';
/** 下方输入区是哪种形态 */
export type ComposerKind = 'new' | 'guide' | 'resume';

interface UiState {
  /** 打开着的历史会话（非 null 时主区域是只读预览） */
  thread: string | null;
  /** 用户主动回到「新任务」欢迎页 */
  welcome: boolean;
  /** 待续跑的会话 */
  resume: HistoryDetail | null;
  /** 在历史会话上打开了「操作页面」：主区域换成当时的信息流 */
  review: boolean;
}

const INITIAL: UiState = { thread: null, welcome: false, resume: null, review: false };
/** 新任务那一屏：welcome 必须真的是 true。
    screen 的判定是「welcome || idle → launch」，只清干净不清成 true 的话，
    上一次跑完（state 还是 done）点「新任务」就原地不动 —— 界面停在运行页。 */
const WELCOME: UiState = { thread: null, welcome: true, resume: null, review: false };

/**
 * 页面在「哪一屏 + 哪一种输入区」上的唯一真相。
 *
 * 之前这三个值散在 App 里（selected / forceLaunch / resumeDraft），每次状态迁移都要
 * 手工把另外两个清干净，六个地方各写一遍 —— 漏一个就会出现「点了历史回不去」
 * 或「续跑条收不掉」这类组合态。这里只暴露 6 个动作，清理集中在一处。
 */
export function useUiMode(sessionState: SessionState) {
  const [state, setState] = useState<UiState>(INITIAL);

  /** 新任务：回到欢迎页，丢掉待续跑 */
  const showWelcome = useCallback(() => setState(WELCOME), []);

  /** 回到运行界面（例如点的是正在跑的那条历史），丢掉待续跑 */
  const focusRun = useCallback(() => setState({ ...INITIAL, welcome: false }), []);

  /** 打开一条历史会话：先进只读详情，回看要再点一次 */
  const openThread = useCallback((thread: string) => {
    setState((prev) => ({ ...prev, thread, review: false }));
  }, []);

  const closeThread = useCallback(() => {
    setState((prev) => ({ ...prev, thread: null, review: false }));
  }, []);

  /** 历史记录 → 当时的操作页面（信息流） */
  const openReview = useCallback(() => {
    setState((prev) => (prev.thread ? { ...prev, review: true } : prev));
  }, []);

  /** 从回看退回会话详情 */
  const closeReview = useCallback(() => {
    setState((prev) => ({ ...prev, review: false }));
  }, []);

  /** 从历史点「继续这个会话」：回到操作界面，把这条挂成待续跑 */
  const prepareResume = useCallback((item: HistoryDetail) => {
    setState({ thread: null, welcome: false, resume: item, review: false });
  }, []);

  /** 真的开跑了：清掉所有临时态，跟着运行状态走 */
  const runStarted = useCallback(() => setState(INITIAL), []);

  const screen: Screen = state.thread
    ? (state.review ? 'review' : 'history')
    : (state.welcome || sessionState === 'idle' ? 'launch' : 'run');

  const composer: ComposerKind = sessionState === 'running'
    ? 'guide'
    : state.resume ? 'resume' : 'new';

  return {
    screen, composer,
    resume: state.resume,
    thread: state.thread,
    review: state.review,
    showWelcome, focusRun, openThread, closeThread,
    openReview, closeReview, prepareResume, runStarted,
  };
}
