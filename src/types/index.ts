export type Track = {
  id: string;
  title: string;
  artist: string;
  artwork?: string | null;
  duration?: number;
  provider?: "soundcloud" | "vk" | "youtube" | string;
  /** VK audio.genre_id из рекомендаций / меты (для дизлайка жанра) */
  genreId?: number;
  /**
   * false — трек из текущего поиска/подборок: resolve с YouTube-fallback при мёртвом VK.
   * undefined / true — старое избранное с VK: только VK-кэш/API как раньше (без лишнего query).
   */
  vk_legacy?: boolean;
};

/** Плейлист в списке (Избранное или кастомный) */
export type PlaylistMeta = {
  id: string;
  name: string;
  is_public?: boolean;
  share_id?: string | null;
  track_count: number;
};

/** Ответ GET /api/playlists */
export type PlaylistsResponse = {
  favorites: Track[];
  playlists: PlaylistMeta[];
  max_free_playlists: number;
};

/** Публичный плейлист по ссылке */
export type SharedPlaylistResponse = {
  name: string;
  items: Track[];
};

export type SearchResponse =
  | Track[]
  | {
      tracks?: Track[];
      items?: Track[];
      results?: Track[];
    };
