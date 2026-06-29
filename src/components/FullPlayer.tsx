import { createPortal } from "react-dom";
import { AnimatePresence, motion } from "framer-motion";
import { ChevronDown, CircleCheck, Download, Heart, List, ListPlus, Loader2, Send, ThumbsDown, Trash2 } from "lucide-react";
import { useCallback, useState } from "react";
import type { Track } from "../types";
import type { PlaybackRepeatMode } from "../lib/playerQueue";
import { WaveformSeekBar } from "./WaveformSeekBar";
import { PlayerControls } from "./PlayerControls";
import { isAndroid } from "../lib/telegram";

const iconBtnClass =
  "flex items-center justify-center w-11 h-11 rounded-full bg-white/15 text-white active:bg-white/25 border-0 touch-manipulation select-none disabled:opacity-50";

type FullPlayerProps = {
  isOpen: boolean;
  track: Track | null;
  isPlaying: boolean;
  isBuffering?: boolean;
  isShuffle: boolean;
  repeatMode: PlaybackRepeatMode;
  currentTime: number;
  duration: number;
  onClose: () => void;
  onToggle: () => void;
  onNext: () => void;
  onPrev: () => void;
  onSeek: (value: number) => void;
  onToggleShuffle: () => void;
  onCycleRepeatMode: () => void;
  /** Открыть Bottom Sheet выбора плейлиста; при наличии кнопка «В плейлист» открывает его */
  onOpenAddToPlaylist?: (track: Track) => void;
  onAddToPlaylist?: (track: Track) => void | Promise<void>;
  onAddAndSend?: (track: Track) => void | Promise<void>;
  onRemove?: (track: Track) => void | Promise<void>;
  /** Открыть меню «Поделиться» (в историю / пользователям) */
  onOpenShareMenu?: (track: Track) => void;
  isLoggedIn?: boolean;
  isInPlaylist?: boolean;
  /** Аудио уже доставлено в чат с ботом — галочка вместо «Скачать» */
  addedToCache?: boolean;
  /** Подтверждённая доставка на сервере — галочка без повтора */
  downloadRepeatLocked?: boolean;
  /** Ждём фактическую доставку после запроса */
  sendToBotPending?: boolean;
  /** Переход в плейлист (закрыть плеер и прокрутить к плейлисту) */
  onGoToPlaylist?: () => void;
  /** Тап по исполнителю — каталог треков на главной */
  onArtistClick?: (artist: string) => void;
  /** Дизлайк (персональная подборка / штраф показа в рекомендациях) */
  onDislike?: (track: Track) => void | Promise<void>;
  /** Полная версия (Открыть / ?startapp) — сближенные отступы и ограничение высоты */
  compactSpacing?: boolean;
  /** Telegram Web (десктоп): те же пропорции, что в сжатой версии; смартфоны не меняются */
  useCompressedProportions?: boolean;
};

export const FullPlayer = ({
  isOpen,
  track,
  isPlaying,
  isBuffering,
  isShuffle,
  repeatMode,
  currentTime,
  duration,
  onClose,
  onToggle,
  onNext,
  onPrev,
  onSeek,
  onToggleShuffle,
  onCycleRepeatMode,
  onOpenAddToPlaylist,
  onAddToPlaylist,
  onAddAndSend,
  onRemove,
  onOpenShareMenu,
  isLoggedIn,
  isInPlaylist,
  addedToCache = false,
  downloadRepeatLocked = false,
  sendToBotPending = false,
  onGoToPlaylist,
  onArtistClick,
  onDislike,
  compactSpacing = false,
  useCompressedProportions = false,
}: FullPlayerProps) => {
  // Компактный layout используем только для Telegram Web (десктоп),
  // а на мобильных отдельно различаем полноэкранный и оконный режим.
  const useCompactLayout = useCompressedProportions; // только десктоп-web
  const [busyAdd, setBusyAdd] = useState(false);
  const [busySend, setBusySend] = useState(false);
  const [busyDislike, setBusyDislike] = useState(false);
  const isAndroidPlatform = isAndroid();

  const onWaveSeekStart = useCallback(() => {}, []);
  const onWaveSeekMove = useCallback(() => {}, []);
  const onWaveSeekEnd = useCallback((time: number) => {
    onSeek(time);
  }, [onSeek]);

  if (!track) return null;

  const player = (
    <AnimatePresence>
      {isOpen ? (
        <motion.div
          className={`fullplayer-root fixed z-[99999] flex flex-col w-full overflow-hidden left-0 right-0 md:max-w-[520px] md:left-1/2 md:right-auto md:-translate-x-1/2 md:rounded-2xl md:shadow-2xl${compactSpacing ? " fullplayer--compact-spacing" : ""}${useCompressedProportions ? " fullplayer--web-desktop" : ""}`}
          style={{
            top: "calc(-1 * env(safe-area-inset-top, 0px))",
            bottom: 0,
            width: "100%",
            height: "calc(100dvh + env(safe-area-inset-top, 0px))",
            minHeight: "calc(100dvh + env(safe-area-inset-top, 0px))",
            margin: 0,
            padding: 0,
            touchAction: "pan-x",
            background: "rgb(var(--surface, 0 0 0))",
          }}
          initial={{ y: "100%" }}
          animate={{ y: 0 }}
          exit={{ y: "100%" }}
          transition={{ type: "spring", damping: 28, stiffness: 300 }}
        >
          {/* Сплошной слой, чтобы при restore из фона ничего не просачивалось */}
          <div className="absolute inset-0 pointer-events-none" style={{ background: "rgb(var(--surface))" }} aria-hidden />
          <div className="relative flex-1 overflow-hidden flex flex-col">
            {/* Размытый фон обложки */}
            {track.artwork ? (
              <img
                src={track.artwork}
                alt=""
                referrerPolicy="no-referrer"
                className="absolute inset-0 h-full w-full object-cover opacity-60 blur-3xl scale-110 pointer-events-none"
                aria-hidden
              />
            ) : (
              <div
                className="absolute inset-0 opacity-60 blur-3xl scale-110 pointer-events-none"
                style={{
                  backgroundImage: "linear-gradient(135deg, rgba(0,136,204,0.6), rgba(0,102,153,0.6))",
                  backgroundSize: "cover",
                  backgroundPosition: "center",
                }}
              />
            )}
            {/* Градиент — снизу заметно затемнён; в компактном режиме усиливается через CSS */}
            <div className="fullplayer-overlay-gradient absolute inset-0 bg-gradient-to-b from-black/0 via-black/25 to-black/75 pointer-events-none" />
            {/* Лёгкое затемнение фона — лучше виден логотип; в компактном режиме сильнее */}
            <div className="fullplayer-overlay-dark absolute inset-0 bg-black/20 pointer-events-none" />

            {/* Контент — отступ сверху = safe area (чтобы не уйти под вырез) */}
            <div
              className="relative z-10 flex flex-col flex-1 px-5 overflow-hidden"
              style={{
                paddingTop: "env(safe-area-inset-top, 0)",
                paddingBottom: "max(16px, env(safe-area-inset-bottom, 0px))",
              }}
            >
              {/* Хедер: сжатая и Telegram Web десктоп — absolute; полная на смартфоне — в потоке */}
              {useCompactLayout ? (
                <div
                  className="fullplayer-header absolute left-0 right-0 flex items-center justify-center px-5 z-20"
                  style={{ top: "calc(env(safe-area-inset-top, 0) - 0.625em)", paddingTop: "0.25rem", paddingBottom: "0.25rem" }}
                >
                  <button
                    onClick={onClose}
                    className="absolute left-0 p-2 -ml-2 rounded-full text-white/70 hover:text-white active:opacity-80 border-0 touch-manipulation"
                    style={{ left: "1ch" }}
                    type="button"
                    aria-label="Закрыть"
                  >
                    <ChevronDown className="h-7 w-7" />
                  </button>
                  <div className="flex items-center justify-center" style={{ gap: "max(0rem, calc(0.25rem - 1ch))" }}>
                    <img
                      src="/icon.png"
                      alt=""
                      className="w-14 h-14 object-contain shrink-0 opacity-50"
                    />
                    <div className="text-[14px] text-white/50 font-medium uppercase tracking-[0.15em] leading-none">TGPlay</div>
                  </div>
                  {onGoToPlaylist ? (
                    <button
                      onClick={onGoToPlaylist}
                      className="absolute right-0 p-2 -mr-2 rounded-full text-white/70 hover:text-white active:opacity-80 border-0 touch-manipulation"
                      style={{ right: "1ch" }}
                      type="button"
                      aria-label="В плейлист"
                    >
                      <List className="h-7 w-7" />
                    </button>
                  ) : null}
                </div>
              ) : (
                <div className="relative flex items-center justify-center flex-shrink-0 py-1 px-5">
                  <button
                    onClick={onClose}
                    className="absolute left-0 p-2 -ml-2 rounded-full text-white/70 hover:text-white active:opacity-80 border-0 touch-manipulation"
                    type="button"
                    aria-label="Закрыть"
                  >
                    <ChevronDown className="h-7 w-7" />
                  </button>
                  <div className="flex items-center justify-center" style={{ gap: "0.25rem" }}>
                    <img src="/icon.png" alt="" className="w-12 h-12 object-contain shrink-0 opacity-50" />
                    <div className="text-[13px] text-white/50 font-medium uppercase tracking-[0.15em] leading-none">TGPlay</div>
                  </div>
                  {onGoToPlaylist ? (
                    <button
                      onClick={onGoToPlaylist}
                      className="absolute right-0 p-2 -mr-2 rounded-full text-white/70 hover:text-white active:opacity-80 border-0 touch-manipulation"
                      type="button"
                      aria-label="В плейлист"
                    >
                      <List className="h-7 w-7" />
                    </button>
                  ) : null}
                </div>
              )}

              {/* Обложка: сжатая и Telegram Web десктоп — наш отступ и размер; полная на смартфоне — из CSS */}
              <div
                className={`fullplayer-cover-section flex flex-col items-center flex-shrink min-h-0 mb-6 md:mb-12 ${
                  useCompactLayout ? "mt-[calc(4.25rem-1.2125em)]" : ""
                }`}
              >
                <motion.div
                  className="fullplayer-cover-box rounded-3xl overflow-hidden shadow-2xl min-w-0 min-h-0 flex-shrink-0"
                  animate={{ scale: 1 }}
                  transition={{ duration: 0.4, ease: "easeOut" }}
                  style={
                    useCompactLayout
                      ? {
                          width: "min(90vw, 42vh, 368px)",
                          height: "min(90vw, 42vh, 368px)",
                        }
                      : {
                          width: "min(80vw, 40vh, 260px)",
                          height: "min(80vw, 40vh, 260px)",
                        }
                  }
                >
                  <img
                    src={track.artwork || "/icon-track.png"}
                    alt=""
                    className="h-full w-full object-cover"
                    decoding="async"
                    fetchPriority="high"
                    referrerPolicy={track.artwork ? "no-referrer" : undefined}
                  />
                </motion.div>
                <div className="fullplayer-cover-caption text-center space-y-0.5 px-4 w-full mt-1">
                  <p className="text-lg font-semibold text-white line-clamp-1">{track.title}</p>
                  {onArtistClick && track.artist?.trim() ? (
                    <button
                      type="button"
                      className="text-[13px] text-white/55 line-clamp-1 w-full max-w-full mx-auto font-medium underline underline-offset-[3px] decoration-dotted decoration-from-font decoration-white/35 hover:text-white/90 hover:decoration-solid hover:decoration-white/55 active:opacity-80 touch-manipulation border-0 bg-transparent p-0 transition-[color,text-decoration-color] duration-150"
                      onClick={() => onArtistClick(track.artist.trim())}
                      aria-label={`Треки: ${track.artist}`}
                    >
                      {track.artist}
                    </button>
                  ) : (
                    <p className="text-[13px] text-white/60 line-clamp-1">{track.artist}</p>
                  )}
                </div>
              </div>

              {/* Блок дорожки и кнопок влево/вправо/плей — отступ задаётся в index.css по классу fullplayer-waveform-block */}
              <div className="fullplayer-waveform-block flex flex-col min-h-0" style={{ marginTop: "2.75em" }}>
                {/* Аудиодорожка */}
                <div className="fullplayer-waveform-row flex items-center justify-center min-h-0 py-4">
                  <div className="w-full -mx-2">
                    <WaveformSeekBar
                      trackId={track.id}
                      currentTime={currentTime}
                      duration={duration}
                      onSeekStart={onWaveSeekStart}
                      onSeekMove={onWaveSeekMove}
                      onSeekEnd={onWaveSeekEnd}
                    />
                  </div>
                </div>
                {/* Кнопки shuffle / prev / play / next / loop */}
                <div className="fullplayer-controls-row">
                  <PlayerControls
                    variant="full"
                    isPlaying={isPlaying}
                    isBuffering={isBuffering}
                    isShuffle={isShuffle}
                    repeatMode={repeatMode}
                    onTogglePlay={onToggle}
                    onNext={onNext}
                    onPrev={onPrev}
                    onToggleShuffle={onToggleShuffle}
                    onCycleRepeatMode={onCycleRepeatMode}
                  />
                </div>
              </div>

              {/* Блок кнопок с сердечком, шерингом и т.д. — отдельный блок под управлением; на Android в полноэкранном режиме чуть ближе к дорожке */}
              <div className="flex flex-col items-center shrink-0 pb-1" style={{ gap: 4 }}>
                {/* Один ряд: сердечко/урна | самолётик | в плейлист | скачать — только иконки, без подписей */}
                {track && (
                  <div
                    className={`fullplayer-action-row flex items-center justify-center gap-3 ${
                      isAndroidPlatform && compactSpacing ? "mt-2" : "mt-4"
                    }`}
                  >
                    {/* В избранное (сердечко) / удалить (урна после лайка) */}
                    {isLoggedIn && (isInPlaylist && onRemove ? (
                      <button
                        className={iconBtnClass}
                        onClick={() => onRemove(track)}
                        type="button"
                        title="Удалить из плейлиста"
                        aria-label="Удалить из плейлиста"
                      >
                        <Trash2 className="h-5 w-5" />
                      </button>
                    ) : onAddToPlaylist ? (
                      <button
                        className={iconBtnClass}
                        onClick={() => {
                          if (busyAdd) return;
                          setBusyAdd(true);
                          void (onAddToPlaylist?.(track) ?? Promise.resolve()).finally(() => setBusyAdd(false));
                        }}
                        type="button"
                        title="В избранное"
                        aria-label="В избранное"
                        disabled={busyAdd}
                      >
                        <Heart className="h-5 w-5" />
                      </button>
                    ) : null)}
                    {isLoggedIn && onDislike ? (
                      <button
                        className={`${iconBtnClass} ${busyDislike ? "opacity-50 pointer-events-none" : ""}`}
                        type="button"
                        title="Не нравится"
                        aria-label="Не нравится"
                        disabled={busyDislike}
                        onClick={() => {
                          if (busyDislike) return;
                          setBusyDislike(true);
                          void Promise.resolve(onDislike(track)).finally(() => setBusyDislike(false));
                        }}
                      >
                        <ThumbsDown className="h-5 w-5" />
                      </button>
                    ) : null}
                    {onOpenShareMenu ? (
                      <button
                        type="button"
                        className={iconBtnClass}
                        onClick={() => onOpenShareMenu(track)}
                        title="Поделиться"
                        aria-label="Поделиться"
                      >
                        <Send className="h-5 w-5" />
                      </button>
                    ) : null}
                    {/* В плейлист (открыть выбор плейлиста) — иконка ListPlus */}
                    {isLoggedIn && onOpenAddToPlaylist ? (
                      <button
                        className={iconBtnClass}
                        onClick={() => onOpenAddToPlaylist(track)}
                        type="button"
                        title="В плейлист"
                        aria-label="В плейлист"
                      >
                        <ListPlus className="h-5 w-5" />
                      </button>
                    ) : null}
                    {/* В чат с ботом: после verified_live — только индикатор; до того (backfill) — кнопка «отправить снова» */}
                    {isLoggedIn && onAddAndSend ? (
                      sendToBotPending || busySend ? (
                        <span className={`${iconBtnClass} opacity-70`} title="Отправка в чат…" aria-label="Отправка в чат">
                          <Loader2 className="h-5 w-5 animate-spin" />
                        </span>
                      ) : addedToCache && downloadRepeatLocked ? (
                        <span className={`${iconBtnClass} opacity-90`} title="В чате с ботом" aria-label="В чате с ботом">
                          <CircleCheck className="h-5 w-5" strokeWidth={2} />
                        </span>
                      ) : (
                        <button
                          className={addedToCache ? `${iconBtnClass} opacity-90` : iconBtnClass}
                          type="button"
                          title={
                            addedToCache
                              ? "В чате с ботом — нажмите, чтобы отправить снова"
                              : "В чат с ботом"
                          }
                          aria-label={
                            addedToCache
                              ? "В чате с ботом, отправить аудио снова"
                              : "В чат с ботом"
                          }
                          onClick={async () => {
                            if (busySend) return;
                            setBusySend(true);
                            try {
                              await onAddAndSend(track);
                            } finally {
                              setBusySend(false);
                            }
                          }}
                          disabled={busySend}
                        >
                          {addedToCache ? (
                            <CircleCheck className="h-5 w-5" strokeWidth={2} />
                          ) : (
                            <Download className="h-5 w-5" />
                          )}
                        </button>
                      )
                    ) : null}
                  </div>
                )}
              </div>
            </div>
          </div>
        </motion.div>
      ) : null}
    </AnimatePresence>
  );

  return createPortal(player, document.body);
};
