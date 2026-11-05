import type { HistoryDetail, NodeId, RunMeta } from '../../types';
import type { NodeChoice } from './NodePicker';
import type { NoteTarget } from './GuideTargets';

export type NodeChoices = Record<NodeId, NodeChoice>;

export interface StartPayload {
  goal: string;
  workdir: string;
  /** 三个节点这次各用什么来源 + 模型（新建任务时总是显式给出） */
  nodes: NodeChoices;
}

/** 开跑前写好的引导：跟着 run 一起投递，在第一个节点边界被取走 */
export interface SeedNote {
  target: NoteTarget;
  text: string;
}

/** 这次续跑要覆盖的来源 / 模型；没给的字段 = 沿用当前配置 */
export type ResumeModels = Partial<Record<NodeId, Partial<NodeChoice>>>;

/** 从「在此目录新建任务」带过来的预填值；n 只用来标识「又递了一次」 */
export interface Preset {
  goal: string;
  workdir: string;
  n: number;
}

export type ComposerVariant =
  | { kind: 'new' }
  | { kind: 'guide'; locked: boolean; pending: Record<string, number> }
  | { kind: 'resume'; item: HistoryDetail };

export interface ComposerProps {
  variant: ComposerVariant;
  meta: RunMeta;
  /** 配置里的默认产出目录 */
  defaultWorkdir: string;
  /** 当前生效的三个节点（续跑卡的「沿用」项要写具体名字） */
  currentModels: Record<NodeId, string>;
  preset: Preset | null;
  onStart: (payload: StartPayload) => Promise<string | null>;
  onGuide: (target: NoteTarget, text: string) => Promise<string | null>;
  onResume: (
    item: HistoryDetail,
    guide: SeedNote | null,
    models: ResumeModels,
  ) => Promise<string | null>;
  /** 续跑卡上的「新建任务」：把目标 / 目录带回新任务表单 */
  onNewTask: () => void;
}

/** 某个变体下的完整 props（其余字段照旧传，variant 收窄成对应的 kind） */
export type CardProps<K extends ComposerVariant['kind']> =
  Omit<ComposerProps, 'variant'> & { variant: Extract<ComposerVariant, { kind: K }> };
