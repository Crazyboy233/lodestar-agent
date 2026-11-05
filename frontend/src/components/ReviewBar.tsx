import type { HistoryDetail } from '../types';
import { STATE_TEXT } from '../ui';

interface Props {
  detail: HistoryDetail | null;
  onBack: () => void;
  onClose: () => void;
}

/**
 * 回看条：说明「这一屏是历史记录当时的操作页面」，并给出回去的路。
 *
 * 这屏不接命令（不能引导、不能退出、不能开新任务），所以既没有工作栏也没有输入卡，
 * 顶上这条窄栏就是全部的控制项。
 */
export function ReviewBar({ detail, onBack, onClose }: Props) {
  const state = detail?.state;
  return (
    <div className="reviewbar">
      <span className="rv-tag">回看</span>
      <span className="rv-thread">{detail?.thread || '—'}</span>
      <span className="rv-note">
        {state ? `${STATE_TEXT[state] ?? state} · ` : ''}
        信息流来自这次的运行日志，只读
      </span>
      <span className="spacer" />
      <button className="ghost" onClick={onBack}>返回会话详情</button>
      <button className="ghost" onClick={onClose}>关闭</button>
    </div>
  );
}
