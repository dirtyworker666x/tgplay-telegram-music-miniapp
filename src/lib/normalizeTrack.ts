import type { Track } from "../types";

const pick = (value: unknown, fallback: string) =>
  typeof value === "string" && value.trim().length > 0 ? value : fallback;

/** Нормализация сырого объекта трека из API VK/бэкенда. */
export const normalizeTrack = (raw: Record<string, unknown>): Track => {
  const id = String(raw.id ?? raw.trackId ?? raw._id ?? "");
  const provider = typeof raw.provider === "string" ? raw.provider : undefined;
  const artwork = pick(raw.cover_url ?? raw.artwork ?? raw.cover ?? raw.image, "");
  const duration =
    typeof raw.duration === "number" ? raw.duration
    : typeof raw.duration === "string" ? parseInt(raw.duration, 10)
    : undefined;
  let genreId: number | undefined;
  const rawGid = raw.genre_id ?? raw.genreId;
  if (typeof rawGid === "number" && Number.isFinite(rawGid)) genreId = rawGid;
  else if (typeof rawGid === "string" && /^\d+$/.test(rawGid.trim())) genreId = parseInt(rawGid.trim(), 10);

  const base: Track = {
    id,
    title: pick(raw.title ?? raw.name, "Unknown title"),
    // For SoundCloud we prefer "empty artist" over noisy "Unknown artist"/username-like fallbacks.
    artist:
      provider === "soundcloud"
        ? pick(raw.artist ?? raw.artist_name ?? raw.author, "")
        : pick(raw.artist ?? raw.artist_name ?? raw.author, "Unknown artist"),
    artwork: artwork || undefined,
    duration: Number.isFinite(duration) ? duration : undefined,
    provider,
    ...(genreId !== undefined ? { genreId } : {}),
  };
  if (raw.vk_legacy === false) {
    base.vk_legacy = false;
  } else if (raw.vk_legacy === true) {
    base.vk_legacy = true;
  }
  return base;
};
