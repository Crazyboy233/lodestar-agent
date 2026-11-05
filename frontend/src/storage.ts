/** localStorage 的唯一入口：key 集中、读写都兜住（隐私模式下 setItem 会抛）。 */

const NS = 'lodestar';

export const KEY = {
  workdir: `${NS}.workdir`,
  /** 上次给三个节点选的来源 + 模型 */
  nodeChoices: `${NS}.nodeChoices`,
  collapsedProjects: `${NS}.collapsedProjects`,
} as const;

export function readText(key: string): string | null {
  try {
    return localStorage.getItem(key);
  } catch {
    return null;
  }
}

export function writeText(key: string, value: string): void {
  try {
    localStorage.setItem(key, value);
  } catch {
    /* 隐私模式写不进去就算了，不值得打断用户 */
  }
}

export function readJson<T>(key: string, fallback: T): T {
  const raw = readText(key);
  if (!raw) return fallback;
  try {
    return JSON.parse(raw) as T;
  } catch {
    return fallback;
  }
}

export function writeJson(key: string, value: unknown): void {
  try {
    localStorage.setItem(key, JSON.stringify(value));
  } catch {
    /* 同上 */
  }
}
