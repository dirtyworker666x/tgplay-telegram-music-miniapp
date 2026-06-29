import type { Track } from "../types";

export type PlaybackRepeatMode = "off" | "one";

export function buildQueue(
  activePlaylistTracks: Track[] | null,
  tracks: Track[],
  playlist: Track[],
): Track[] {
  if (activePlaylistTracks && activePlaylistTracks.length > 0) return activePlaylistTracks;
  if (tracks.length > 0) return tracks;
  if (playlist.length > 0) return playlist;
  return [];
}

export function getRandomNextIndex(
  currentIndex: number,
  length: number,
  random: () => number = Math.random,
): number {
  if (length <= 0 || currentIndex < 0 || currentIndex >= length) return -1;
  if (length === 1) return 0;

  let idx = currentIndex;
  let safety = 0;
  while (idx === currentIndex && safety < 10) {
    idx = Math.floor(random() * length);
    safety += 1;
  }

  if (idx === currentIndex) {
    // fallback на линейный next, если random ведёт себя неожиданно
    return (currentIndex + 1) % length;
  }

  return idx;
}

/** Сколько последних проигранных id исключать при shuffle (меньше повторов подряд). */
export const SHUFFLE_RECENT_EXCLUDE_MAX = 14;

export function dedupeTracksById(tracks: Track[]): Track[] {
  const seen = new Set<string>();
  const out: Track[] = [];
  for (const t of tracks) {
    const id = t.id?.trim() ?? "";
    if (!id || seen.has(id)) continue;
    seen.add(id);
    out.push(t);
  }
  return out;
}

/**
 * Случайный следующий индекс при shuffle: не текущий и не из недавно проигранных id
 * (иначе при равномерном random одни и те же треки возвращаются слишком часто).
 */
export function getRandomNextIndexAvoidingRecent(
  currentIndex: number,
  queue: Track[],
  recentTrackIds: readonly string[],
  random: () => number = Math.random,
): number {
  const length = queue.length;
  if (length <= 0 || currentIndex < 0 || currentIndex >= length) return -1;
  if (length === 1) return 0;

  const recent = new Set(recentTrackIds);
  const pickFrom = (pred: (i: number) => boolean): number => {
    const candidates: number[] = [];
    for (let i = 0; i < length; i++) {
      if (!pred(i)) continue;
      candidates.push(i);
    }
    if (candidates.length === 0) return -1;
    return candidates[Math.floor(random() * candidates.length)]!;
  };

  let next = pickFrom((i) => {
    if (i === currentIndex) return false;
    const id = queue[i]?.id?.trim() ?? "";
    return id.length > 0 && !recent.has(id);
  });
  if (next === -1) {
    next = pickFrom((i) => i !== currentIndex);
  }
  return next;
}

