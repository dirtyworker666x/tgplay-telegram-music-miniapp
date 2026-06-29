import { useCallback, useRef, useState } from "react";
import { Check, CircleCheck, Download, ListPlus, Loader2, RefreshCw, Send, ThumbsDown, Trash2 } from "lucide-react";
import type { Track } from "../types";
import { formatTime } from "../lib/format";

const PREFETCH_DELAY_MS = 150;
/** Компактная панель действий — больше ширины под название трека. */
const ROW_ICON = "h-[18px] w-[18px] text-text-muted shrink-0";
const ROW_ACTION_PAD =
  "flex items-center justify-center rounded-lg px-0.5 py-1.5 min-h-[40px] min-w-[30px] active:bg-white/20 dark:active:bg-white/8";

type TrackRowProps = {
  track: Track;
  index?: number;
  onSelect: (track: Track) => void;
  /** Тап по исполнителю — не запускает трек */
  onArtistClick?: (artist: string) => void;
  /** Открыть Bottom Sheet выбора плейлиста (отдельная кнопка от «в чат с ботом») */
  onOpenAddToPlaylist?: (track: Track) => void;
  onAddAndSend?: (track: Track) => void | Promise<void>;
  /** Открыть меню «Поделиться» (в историю / пользователям) */
  onOpenShareMenu?: (track: Track) => void;
  onRemove?: (track: Track) => void;
  /** Предзагрузка URL при наведении/удержании — ускоряет старт воспроизведения при клике */
  onPreloadTrack?: (track: Track) => void;
  isLoggedIn?: boolean;
  isInPlaylist?: boolean;
  /** Дизлайк (например в рекомендациях) */
  onDislike?: (track: Track) => void | Promise<void>;
  /** Показывать кнопку «В плейлист» даже если трек уже в текущем списке (например в избранном) */
  allowAddToPlaylistInList?: boolean;
  /** Аудио уже в чате с ботом */
  deliveredToBot?: boolean;
  /** Сервер пометил как подтверждённую доставку — повтор «в бот» не показываем */
  repeatSendLocked?: boolean;
  /** Ждём доставку после нажатия «Скачать» */
  sendToBotPending?: boolean;
};

export const TrackRow = ({
  track,
  index = 0,
  onSelect,
  onArtistClick,
  onOpenAddToPlaylist,
  onAddAndSend,
  onOpenShareMenu,
  onRemove,
  onPreloadTrack,
  isLoggedIn,
  isInPlaylist,
  onDislike,
  allowAddToPlaylistInList = false,
  deliveredToBot = false,
  repeatSendLocked = false,
  sendToBotPending = false,
}: TrackRowProps) => {
  const [busy, setBusy] = useState(false);
  const [dislikeBusy, setDislikeBusy] = useState(false);
  const prefetchTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const clearPrefetchTimer = useCallback(() => {
    if (prefetchTimerRef.current) {
      clearTimeout(prefetchTimerRef.current);
      prefetchTimerRef.current = null;
    }
  }, []);

  const startPrefetch = useCallback(() => {
    clearPrefetchTimer();
    prefetchTimerRef.current = setTimeout(() => {
      prefetchTimerRef.current = null;
      onPreloadTrack?.(track);
    }, PREFETCH_DELAY_MS);
  }, [track, onPreloadTrack, clearPrefetchTimer]);

  const onShareClick = useCallback(
    (e: React.MouseEvent) => {
      e.stopPropagation();
      onOpenShareMenu?.(track);
    },
    [track, onOpenShareMenu],
  );

  const onArtistPointerDown = useCallback(
    (e: React.PointerEvent) => {
      e.stopPropagation();
    },
    [],
  );

  const onArtistClickHandler = useCallback(
    (e: React.MouseEvent) => {
      e.stopPropagation();
      const a = track.artist?.trim();
      if (a && onArtistClick) onArtistClick(a);
    },
    [track.artist, onArtistClick],
  );

  const onPlaylistClick = (e: React.MouseEvent) => {
    e.stopPropagation();
    onOpenAddToPlaylist?.(track);
  };

  const onSendToBotClick = async (e: React.MouseEvent) => {
    e.stopPropagation();
    if (sendToBotPending || !onAddAndSend || busy) return;
    setBusy(true);
    try {
      await onAddAndSend(track);
    } finally {
      setBusy(false);
    }
  };

  const showAddToPlaylist = isLoggedIn && onOpenAddToPlaylist && (!isInPlaylist || allowAddToPlaylistInList);
  /** Слот «в бот» держим всегда при доступной отправке — при verified_live показываем галочку вместо пустоты (без сдвига соседних кнопок). */
  const showBotSlot =
    Boolean(isLoggedIn && onAddAndSend && (!isInPlaylist || allowAddToPlaylistInList));
  /** В чат с ботом; повтор — только если не verified_live (старый backfill / неточный статус) */
  const canSendToBot =
    showBotSlot && (!deliveredToBot || !repeatSendLocked);
  const showSendToBotPending = canSendToBot && sendToBotPending;
  const showRemove = isLoggedIn && onRemove && isInPlaylist;
  const showDislike = Boolean(onDislike);

  const onRowKeyDown = useCallback(
    (e: React.KeyboardEvent) => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        onSelect(track);
      }
    },
    [onSelect, track],
  );

  const onDislikeClick = async (e: React.MouseEvent) => {
    e.stopPropagation();
    if (!onDislike || dislikeBusy) return;
    setDislikeBusy(true);
    try {
      await onDislike(track);
    } finally {
      setDislikeBusy(false);
    }
  };

  const durationStr =
    track.duration != null && Number.isFinite(track.duration) && track.duration >= 0
      ? formatTime(track.duration)
      : null;

  return (
    <div
      role="button"
      tabIndex={0}
      data-testid="track-row"
      className="w-full flex items-center gap-0.5 pl-2.5 pr-0.5 py-2.5 rounded-2xl active:bg-white/12 dark:active:bg-white/5 text-left touch-manipulation select-none cursor-pointer"
      onClick={() => onSelect(track)}
      onKeyDown={onRowKeyDown}
      onMouseEnter={startPrefetch}
      onMouseLeave={clearPrefetchTimer}
      onTouchStart={startPrefetch}
      onTouchEnd={clearPrefetchTimer}
      onTouchCancel={clearPrefetchTimer}
    >
      <div className="relative h-12 w-12 shrink-0 rounded-2xl overflow-hidden flex items-center justify-center track-cover shadow-md">
        {track.artwork ? (
          <img
            src={track.artwork}
            alt={`${track.title} cover`}
            className="h-full w-full object-cover"
            loading={index < 4 ? "eager" : "lazy"}
            decoding="async"
            fetchPriority={index < 3 ? "high" : undefined}
            referrerPolicy="no-referrer"
          />
        ) : (
          <img src="/icon-track.png" alt="" className="h-full w-full object-cover" />
        )}
        {deliveredToBot ? (
          <span
            className="pointer-events-none absolute left-px top-px z-10 flex h-[15px] w-[15px] items-center justify-center rounded-full border border-white/[0.2] bg-black/38 text-white shadow-[0_1px_4px_rgba(0,0,0,0.35),inset_0_1px_0_rgba(255,255,255,0.32)] backdrop-blur-md backdrop-saturate-150 supports-[backdrop-filter]:bg-black/28"
            title="В чате с ботом"
            aria-label="В чате с ботом"
          >
            <Check
              className="h-[8px] w-[8px] text-white [filter:drop-shadow(0_0.5px_0.75px_rgba(0,0,0,0.5))]"
              strokeWidth={2.75}
              aria-hidden
            />
          </span>
        ) : null}
      </div>
      <div className="min-w-0 flex-1 flex flex-col justify-center gap-0.5 pr-0.5">
        <p className="text-[14px] font-semibold text-text line-clamp-2 break-words min-w-0">{track.title}</p>
        {durationStr || track.artist?.trim() ? (
          <div className="flex items-center gap-1.5 min-w-0 mt-0.5">
            {track.artist?.trim() ? (
              onArtistClick ? (
                <button
                  type="button"
                  className="text-[12px] text-text-muted line-clamp-1 text-left min-w-0 flex-1 font-medium underline underline-offset-[3px] decoration-dotted decoration-from-font decoration-text-muted/45 hover:text-text hover:decoration-solid hover:decoration-text/35 active:opacity-80 touch-manipulation transition-[color,text-decoration-color] duration-150"
                  onClick={onArtistClickHandler}
                  onPointerDown={onArtistPointerDown}
                  aria-label={`Треки: ${track.artist}`}
                >
                  {track.artist}
                </button>
              ) : (
                <p className="text-[12px] text-text-muted line-clamp-1 min-w-0 flex-1">{track.artist}</p>
              )
            ) : (
              <span className="min-w-0 flex-1" aria-hidden />
            )}
            {durationStr ? (
              <span className="text-[12px] text-text-muted tabular-nums shrink-0">{durationStr}</span>
            ) : null}
          </div>
        ) : null}
      </div>
      <div className="flex shrink-0 items-center justify-end gap-0 self-center">
        {showDislike ? (
          <span
            role="button"
            tabIndex={0}
            className={`${ROW_ACTION_PAD} touch-manipulation select-none ${dislikeBusy ? "opacity-50 pointer-events-none" : ""}`}
            onClick={onDislikeClick}
            title="Не нравится"
            onKeyDown={(e) => {
              if (e.key === "Enter" || e.key === " ") {
                e.preventDefault();
                void onDislikeClick(e as unknown as React.MouseEvent);
              }
            }}
            aria-label="Не нравится"
          >
            <ThumbsDown className={ROW_ICON} />
          </span>
        ) : null}
        {onOpenShareMenu ? (
          <span
            role="button"
            tabIndex={0}
            className={`${ROW_ACTION_PAD} touch-manipulation select-none`}
            onClick={onShareClick}
            title="Поделиться"
            onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); onShareClick(e as unknown as React.MouseEvent); } }}
            aria-label="Поделиться"
          >
            <Send className={ROW_ICON} />
          </span>
        ) : null}
        {showAddToPlaylist ? (
          <span
            role="button"
            tabIndex={0}
            className={`${ROW_ACTION_PAD} touch-manipulation select-none`}
            onClick={onPlaylistClick}
            title="В плейлист"
            onKeyDown={(e) => {
              if (e.key === "Enter" || e.key === " ") {
                e.preventDefault();
                onPlaylistClick(e as unknown as React.MouseEvent);
              }
            }}
            aria-label="В плейлист"
          >
            <ListPlus className={ROW_ICON} />
          </span>
        ) : null}
        {showBotSlot ? (
          canSendToBot ? (
            <span
              role="button"
              tabIndex={0}
              className={`${ROW_ACTION_PAD} touch-manipulation select-none ${busy || showSendToBotPending ? "opacity-60 pointer-events-none" : ""}`}
              onClick={(e) => void onSendToBotClick(e)}
              title={
                showSendToBotPending
                  ? "Отправка…"
                  : deliveredToBot
                    ? "Отправить в чат снова"
                    : "В чат с ботом"
              }
              onKeyDown={(e) => {
                if (e.key === "Enter" || e.key === " ") {
                  e.preventDefault();
                  void onSendToBotClick(e as unknown as React.MouseEvent);
                }
              }}
              aria-label={deliveredToBot ? "Отправить в чат снова" : "В чат с ботом"}
            >
              {showSendToBotPending ? (
                <Loader2 className={`${ROW_ICON} animate-spin`} aria-hidden />
              ) : deliveredToBot ? (
                <RefreshCw className={ROW_ICON} aria-hidden />
              ) : (
                <Download className={ROW_ICON} aria-hidden />
              )}
            </span>
          ) : (
            <span
              className={`${ROW_ACTION_PAD} touch-manipulation select-none pointer-events-none opacity-90`}
              title="В чате с ботом"
              aria-label="В чате с ботом"
            >
              <CircleCheck className={ROW_ICON} strokeWidth={2} aria-hidden />
            </span>
          )
        ) : null}
        {showRemove ? (
          <span
            role="button"
            tabIndex={0}
            className={`${ROW_ACTION_PAD} touch-manipulation select-none`}
            onClick={(e) => { e.stopPropagation(); onRemove(track); }}
            title="Удалить"
            onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); onRemove(track); } }}
          >
            <Trash2 className={ROW_ICON} />
          </span>
        ) : null}
      </div>
    </div>
  );
};
