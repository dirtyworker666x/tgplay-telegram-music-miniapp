import type { Track } from "../types";

/**
 * Если в выдаче поиска нет обложки, подставляет URL из локальной библиотеки по тому же `id` (owner_id_audio_id).
 * Не требует запроса к VK: тот же трек уже мог быть сохранён с `artwork` при загрузке избранного / плейлиста.
 */
export function mergeLibraryArtworkIntoTracks(
  results: Track[],
  librarySources: readonly (readonly Track[] | null | undefined)[],
): Track[] {
  const byId = new Map<string, string>();
  for (const source of librarySources) {
    if (!source?.length) continue;
    for (const t of source) {
      const id = t.id;
      if (!id || byId.has(id)) continue;
      const a = typeof t.artwork === "string" ? t.artwork.trim() : "";
      if (a) byId.set(id, a);
    }
  }
  if (byId.size === 0) return results;
  return results.map((t) => {
    const cur = typeof t.artwork === "string" ? t.artwork.trim() : "";
    if (cur) return t;
    const lib = byId.get(t.id);
    return lib ? { ...t, artwork: lib } : t;
  });
}
