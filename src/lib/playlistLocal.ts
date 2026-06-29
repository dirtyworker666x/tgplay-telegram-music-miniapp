const ADDED_KEY_PREFIX = "tgplay_added_playlists_";
export const SHARED_SAVED_PREFIX = "tgplay_shared_saved_";

const safeParse = (raw: string | null): string[] => {
  if (!raw) return [];
  try {
    const parsed = JSON.parse(raw);
    return Array.isArray(parsed) ? parsed.filter((id) => typeof id === "string") : [];
  } catch {
    return [];
  }
};

export const getAddedPlaylistIds = (userId: number): string[] => {
  if (typeof window === "undefined") return [];
  const raw = window.localStorage.getItem(`${ADDED_KEY_PREFIX}${userId}`);
  return safeParse(raw);
};

export const addAddedPlaylistId = (userId: number, playlistId: string): void => {
  if (typeof window === "undefined") return;
  const key = `${ADDED_KEY_PREFIX}${userId}`;
  const current = safeParse(window.localStorage.getItem(key));
  if (current.includes(playlistId)) return;
  current.push(playlistId);
  try {
    window.localStorage.setItem(key, JSON.stringify(current));
  } catch {
    // ignore quota errors
  }
};

export const canAddMoreAddedPlaylists = (userId: number, max: number): boolean => {
  const current = getAddedPlaylistIds(userId);
  return current.length < max;
};

export const clearSharedMappingForPlaylist = (playlistId: string): void => {
  if (typeof window === "undefined") return;
  try {
    const storage = window.localStorage;
    const keysToRemove: string[] = [];
    for (let i = 0; i < storage.length; i += 1) {
      const key = storage.key(i);
      if (!key || !key.startsWith(SHARED_SAVED_PREFIX)) continue;
      const value = storage.getItem(key);
      if (value === playlistId) {
        keysToRemove.push(key);
      }
    }
    keysToRemove.forEach((k) => storage.removeItem(k));
  } catch {
    // ignore
  }
};

