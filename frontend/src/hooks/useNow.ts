import { useCallback, useEffect, useState } from 'react';

/** 每 500ms 重渲染一次，用来驱动计时器；不做状态同步，纯粹为显示。 */
export function useNow(interval = 500): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now()), interval);
    return () => window.clearInterval(id);
  }, [interval]);
  return now;
}

/** 复制到剪贴板，失败静默（本地 http 下 navigator.clipboard 可能不可用） */
export function useCopy(): [string | null, (text: string) => void] {
  const [copied, setCopied] = useState<string | null>(null);
  const copy = useCallback((text: string) => {
    const done = () => {
      setCopied(text);
      window.setTimeout(() => setCopied(null), 1500);
    };
    if (navigator.clipboard?.writeText) {
      navigator.clipboard.writeText(text).then(done).catch(() => undefined);
    }
  }, []);
  return [copied, copy];
}
