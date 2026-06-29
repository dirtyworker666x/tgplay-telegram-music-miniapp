import type { ReactNode } from "react";
import type { Track } from "../types";
import { canonicalPlaylistTrackId } from "../lib/shareTrackId";
import { TrackRow } from "./TrackRow";

type TrackListProps = {
  title: string;
  /** Кнопки справа от заголовка (например «Обновить», «Моя волна») */
  actions?: ReactNode;
  /** Тап по имени исполнителя в строке (каталог треков по популярности) */
  onArtistClick?: (artist: string) => void;
  tracks: Track[];
  playlist?: Track[];
  onSelect: (track: Track) => void;
  /** Открыть Bottom Sheet выбора плейлиста (если задан — кнопка «Добавить» открывает его) */
  onOpenAddToPlaylist?: (track: Track) => void;
  onAddAndSend?: (track: Track) => void | Promise<void>;
  /** Открыть меню «Поделиться» */
  onOpenShareMenu?: (track: Track) => void;
  onRemove?: (track: Track) => void;
  /** Предзагрузка URL при наведении на трек (ускоряет воспроизведение) */
  onPreloadTrack?: (track: Track) => void;
  isLoggedIn?: boolean;
  /** Дизлайк в персональной подборке (кнопка «не нравится») */
  onDislike?: (track: Track) => void | Promise<void>;
  /** Показывать кнопку «В плейлист» даже для треков из текущего списка (например в избранном) */
  allowAddToPlaylistInList?: boolean;
  /** Уже доставлено в чат с ботом (галочка на обложке) */
  deliveredToBotIds?: ReadonlySet<string>;
  /** Подтверждённая доставка (verified_live) — скрыть повтор «в бот» */
  repeatSendLockedIds?: ReadonlySet<string>;
  /** Ожидание доставки после «Скачать» */
  sendToBotPendingIds?: ReadonlySet<string>;
};

export const TrackList = ({
  title,
  tracks,
  actions,
  onArtistClick,
  playlist = [],
  onSelect,
  onOpenAddToPlaylist,
  onAddAndSend,
  onOpenShareMenu,
  onRemove,
  onPreloadTrack,
  isLoggedIn,
  onDislike,
  allowAddToPlaylistInList = false,
  deliveredToBotIds,
  repeatSendLockedIds,
  sendToBotPendingIds,
}: TrackListProps) => {
  if (tracks.length === 0) {
    return null;
  }

  return (
    <section className="space-y-3" data-testid="track-list">
      <div
        className={`flex items-center gap-2 px-0.5 relative min-h-[20px] ${actions ? "justify-between" : "justify-center"}`}
      >
        <h2
          className={`text-[11px] font-semibold uppercase tracking-[0.18em] text-text-muted ${actions ? "text-left truncate min-w-0" : "text-center"}`}
        >
          {title}
        </h2>
        {actions ? <div className="flex items-center gap-1 shrink-0">{actions}</div> : null}
        <span
          className={`text-[11px] text-text-muted tabular-nums shrink-0 ${actions ? "" : "absolute right-0 top-1/2 -translate-y-1/2"}`}
        >
          {tracks.length}
        </span>
      </div>
      <div className="glass rounded-3xl p-2 space-y-1.5 shadow-card">
        {tracks.map((track, index) => (
          <TrackRow
            key={track.id}
            track={track}
            index={index}
            onSelect={onSelect}
            onArtistClick={onArtistClick}
            onOpenAddToPlaylist={onOpenAddToPlaylist}
            onAddAndSend={onAddAndSend}
            onOpenShareMenu={onOpenShareMenu}
            onRemove={onRemove}
            onPreloadTrack={onPreloadTrack}
            isLoggedIn={isLoggedIn}
            isInPlaylist={playlist.some(
              (t) => canonicalPlaylistTrackId(t.id) === canonicalPlaylistTrackId(track.id),
            )}
            allowAddToPlaylistInList={allowAddToPlaylistInList}
            onDislike={onDislike}
            deliveredToBot={Boolean(
              deliveredToBotIds?.has(canonicalPlaylistTrackId(track.id)) ||
                repeatSendLockedIds?.has(canonicalPlaylistTrackId(track.id)),
            )}
            repeatSendLocked={Boolean(repeatSendLockedIds?.has(canonicalPlaylistTrackId(track.id)))}
            sendToBotPending={Boolean(sendToBotPendingIds?.has(canonicalPlaylistTrackId(track.id)))}
          />
        ))}
      </div>
    </section>
  );
};
