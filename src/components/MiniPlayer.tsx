import { motion, useMotionValue, useTransform, AnimatePresence } from "framer-motion";
import { X } from "lucide-react";
import type { Track } from "../types";
import { PlayerControls } from "./PlayerControls";
import { isAndroid } from "../lib/telegram";

type MiniPlayerProps = {
  track: Track | null;
  isPlaying: boolean;
  isBuffering?: boolean;
  isShuffle: boolean;
  repeatMode: import("../lib/playerQueue").PlaybackRepeatMode;
  onToggle: () => void;
  onNext: () => void;
  onPrev: () => void;
  onToggleShuffle: () => void;
  onCycleRepeatMode: () => void;
  onOpen: () => void;
  onClose: () => void;
};

export const MiniPlayer = ({
  track,
  isPlaying,
  isBuffering,
  isShuffle,
  repeatMode,
  onToggle,
  onNext,
  onPrev,
  onToggleShuffle,
  onCycleRepeatMode,
  onOpen,
  onClose,
}: MiniPlayerProps) => {
  const y = useMotionValue(0);
  const opacity = useTransform(y, [0, 120], [1, 0]);
  const extraBottomPx = isAndroid() ? 48 : 24;

  return (
    <AnimatePresence>
      {track ? (
        <motion.div
          className="fixed inset-x-0 bottom-0 z-40 px-3"
          initial={{ y: 80, opacity: 0 }}
          animate={{ y: 0, opacity: 1 }}
          exit={{ y: 120, opacity: 0 }}
          transition={{ type: "spring", damping: 24, stiffness: 260 }}
          style={{
            y,
            opacity,
            // На Android поднимаем ещё выше (safe-area + 48px), на остальных safe-area + 24px
            paddingBottom: `calc(max(8px, env(safe-area-inset-bottom, 0px)) + ${extraBottomPx}px)`,
          }}
          drag="y"
          dragConstraints={{ top: 0, bottom: 0 }}
          dragElastic={0.6}
          onDragEnd={(_e, info) => {
            if (info.offset.y > 60 || info.velocity.y > 300) onClose();
          }}
        >
          <div className="rounded-3xl shadow-card px-4 py-3 flex items-center gap-3 bg-white/75 backdrop-blur-md border border-white/20">
            <button
              className="flex items-center gap-2.5 flex-1 min-w-0 touch-manipulation select-none"
              onClick={onOpen}
              type="button"
            >
              <div className="h-11 w-11 shrink-0 rounded-2xl overflow-hidden track-cover shadow-md">
                <img
                  src={track.artwork || "/icon-track.png"}
                  decoding="async"
                  alt=""
                  className="h-full w-full object-cover"
                  referrerPolicy={track.artwork ? "no-referrer" : undefined}
                />
              </div>
              <div className="text-left min-w-0">
                <p className="text-[13px] font-semibold line-clamp-1">{track.title}</p>
                <p className="text-[11px] text-text-muted line-clamp-1">{track.artist}</p>
              </div>
            </button>
            <div className="flex items-center shrink-0 gap-0.5">
              <PlayerControls
                variant="mini"
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
              <button className="p-2 rounded-full active:opacity-80 ml-0.5 text-text-muted touch-manipulation select-none" onClick={onClose} type="button">
                <X className="h-4 w-4" />
              </button>
            </div>
          </div>
        </motion.div>
      ) : null}
    </AnimatePresence>
  );
};
