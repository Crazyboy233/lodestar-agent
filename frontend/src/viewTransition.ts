import { flushSync } from 'react-dom';

/**
 * 把一次 React 状态更新包成浏览器视图过渡（View Transitions）。
 *
 * 页面从「清屏页」切到「运行页」时，布局是整体换的：输入卡从中间掉到底部、
 * 右侧抽屉从无到有。这种改动 CSS 没法补间（display / 网格结构都变了），
 * 交给浏览器拍两张快照去插值最省事，命名元素各自平移缩放，其余整体交叉淡入。
 *
 * 两个必须注意的点：
 * - 回调里要 flushSync：React 默认把更新排到微任务，等浏览器来拍「新快照」时
 *   DOM 还没变，动画会从旧画面过渡到旧画面（等于没动）。
 * - 浏览器不支持（Firefox）或用户开了「减少动态效果」时直接更新，退化成普通切换。
 */
type TransitionDocument = Document & {
  startViewTransition?: (callback: () => void) => unknown;
};

export function withViewTransition(update: () => void): void {
  // 走 unknown 中转：不同 TS 版本的 lib.dom 对 startViewTransition 有/无声明，
  // 直接交叉断言会在「已声明」的那些版本上报类型冲突
  const doc = document as unknown as TransitionDocument;
  const reduce = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches;
  if (!doc.startViewTransition || reduce) {
    update();
    return;
  }
  doc.startViewTransition(() => {
    flushSync(update);
  });
}
