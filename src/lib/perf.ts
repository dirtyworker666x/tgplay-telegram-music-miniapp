/**
 * Метки производительности для замеров (e2e, ручной анализ).
 * Не влияют на прод; используются в measure:e2e-perf.
 */
export function perfMark(name: string): void {
  if (typeof performance !== "undefined" && performance.mark) {
    performance.mark(name);
  }
}

/** Время от навигации до метки в мс (если есть navigationStart). */
export function perfMeasure(name: string): number | null {
  if (typeof performance === "undefined") return null;
  const nav = performance.getEntriesByType?.("navigation")?.[0] as PerformanceNavigationTiming | undefined;
  const start = nav?.startTime ?? 0;
  const marks = performance.getEntriesByName?.(name, "mark") ?? [];
  const last = marks[marks.length - 1];
  if (!last) return null;
  return last.startTime - start;
}

/** Вызвать при первом воспроизведении — для e2e скрипта. */
export function perfAudioPlaying(): void {
  perfMark("audio-playing");
  if (typeof window !== "undefined") {
    (window as unknown as { __audioPlayingAt?: number }).__audioPlayingAt = performance.now();
  }
}
