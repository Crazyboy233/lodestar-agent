import type { ComposerProps } from './composer/types';
import { GuideCard } from './composer/GuideCard';
import { NewTaskCard } from './composer/NewTaskCard';
import { ResumeCard } from './composer/ResumeCard';

export type {
  ComposerProps, ComposerVariant, Preset, ResumeModels, SeedNote, StartPayload,
} from './composer/types';

/**
 * 事件流下方那块输入区。三种状态各是一个子组件，靠 variant 切换：
 *
 *   new    新任务：目标 + 产出目录 + 模型
 *   guide  运行中：只发引导（投给主 agent / worker）
 *   resume 待续跑：这条会话的信息 + 可选引导 + 模型 + 二次确认
 *
 * 三者共用同一套外壳和零件（ComposerTextarea / ModelPicker / GuideTargets）。
 * 切换状态时整块卸载重建，所以不需要「running 变化时清空另一侧状态」这类手工清理。
 */
export function Composer(props: ComposerProps) {
  const { variant } = props;
  if (variant.kind === 'guide') return <GuideCard {...props} variant={variant} />;
  if (variant.kind === 'resume') return <ResumeCard {...props} variant={variant} />;
  return <NewTaskCard {...props} variant={variant} />;
}
