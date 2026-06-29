import { useCallback, useEffect, useState } from "react";
import { createPortal } from "react-dom";
import { AnimatePresence, motion } from "framer-motion";
import { Camera, MessageCircle, X } from "lucide-react";
import { toast } from "../lib/toast";
import { invalidateTrackCard, prepareShareMessage, prepareStoryMedia, getTrackShareUrl } from "../lib/api";
import { trackEvent } from "../lib/analytics";
import { getWebApp, openTelegramDeepLink } from "../lib/telegram";
import type { Track } from "../types";

type ShareTrackSheetProps = {
  track: Track | null;
  isOpen: boolean;
  onClose: () => void;
};

export function ShareTrackSheet({ track, isOpen, onClose }: ShareTrackSheetProps) {
  const [busy, setBusy] = useState<"story" | "chat" | null>(null);
  const [preparedMessageId, setPreparedMessageId] = useState<string | null>(null);

  useEffect(() => {
    if (!isOpen || !track) {
      setPreparedMessageId(null);
      return;
    }
    let cancelled = false;
    prepareShareMessage(track.id).then((id) => {
      if (!cancelled) setPreparedMessageId(id);
    }).catch(() => {
      if (!cancelled) setPreparedMessageId(null);
    });
    return () => { cancelled = true; };
  }, [isOpen, track?.id]);

  const handleClose = useCallback(() => {
    if (track) invalidateTrackCard(track.id);
    onClose();
  }, [track, onClose]);

  const shareToStory = useCallback(async () => {
    if (!track) return;
    setBusy("story");
    trackEvent("button_share_story", { track_id: track.id });
    const storyText = `▶️ PLAY: ${getTrackShareUrl(track.id)}`;
    const tg = getWebApp() as unknown as {
      shareToStory?: (url: string, params?: { text?: string }) => void;
    };
    try {
      if (tg?.shareToStory) {
        const mediaUrl = await prepareStoryMedia(track.id);
        if (!mediaUrl) {
          toast.error("Не удалось подготовить картинку");
          return;
        }
        // Только JPEG URL + подпись. widget_link часто даёт чёрный «видео»-слот в редакторе сторис.
        tg.shareToStory(mediaUrl, { text: storyText });
        handleClose();
      } else {
        if (navigator.clipboard?.writeText) await navigator.clipboard.writeText(storyText);
        toast.success("Ссылка скопирована");
        handleClose();
      }
    } catch {
      toast.error("Не удалось поделиться");
    } finally {
      setBusy(null);
    }
  }, [track, handleClose]);

  // Поделиться «Пользователям»: 1) shareMessage(prepared_message_id) — нативное сообщение с обложкой и кнопкой; 2) fallback — share/url с превью по og:image.
  const shareToChat = useCallback(() => {
    if (!track) return;
    setBusy("chat");
    trackEvent("button_share_to_users", { track_id: track.id });
    const tg = getWebApp();

    const hasShareMessage = tg?.shareMessage && (typeof tg.isVersionAtLeast !== "function" || tg.isVersionAtLeast("7.10"));
    const validPreparedId = typeof preparedMessageId === "string" && preparedMessageId.trim().length > 0;
    if (hasShareMessage && validPreparedId && tg.shareMessage) {
      const onResult = (ok?: unknown) => {
        setBusy(null);
        if (ok) toast.success("Отправлено!");
        handleClose();
        invalidateTrackCard(track.id);
      };
      if (tg.shareMessage.length > 1) {
        (tg.shareMessage as (id: string, cb: (ok?: unknown) => void) => void)(preparedMessageId, onResult);
      } else {
        (tg.shareMessage as (id: string) => void)(preparedMessageId);
        handleClose();
        invalidateTrackCard(track.id);
        setBusy(null);
      }
      return;
    }

    // Окно с прокруткой (url= пустой). Формат: название, пустая строка, ▶️ PLAY: t.me/...
    const text = `Я слушаю «${track.artist} — ${track.title}» в TGPlay 🎧\n\n▶️ PLAY: ${getTrackShareUrl(track.id)}`;
    const shareUrl = `https://t.me/share/url?url=&text=${encodeURIComponent(text)}`;
    if (tg?.openTelegramLink) {
      tg.openTelegramLink(shareUrl);
      handleClose();
      invalidateTrackCard(track.id);
    } else {
      openTelegramDeepLink(shareUrl);
      handleClose();
      invalidateTrackCard(track.id);
    }
    setBusy(null);
  }, [track, handleClose, preparedMessageId]);

  if (!track) return null;

  const content = (
    <AnimatePresence>
      {isOpen ? (
        <>
          <motion.div
            className="fixed inset-0 z-[99998] bg-black/50"
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            onClick={handleClose}
            aria-hidden
          />
          <motion.div
            role="dialog"
            aria-modal="true"
            aria-label="Поделиться треком"
            className="fixed left-0 right-0 bottom-0 z-[99999] rounded-t-3xl overflow-hidden"
            style={{
              paddingBottom: "env(safe-area-inset-bottom)",
              background: "rgb(var(--surface))",
              boxShadow: "0 -8px 32px rgba(0,0,0,0.3)",
            }}
            initial={{ y: "100%" }}
            animate={{ y: 0 }}
            exit={{ y: "100%" }}
            transition={{ type: "spring", damping: 28, stiffness: 300 }}
          >
            <div className="w-full h-1.5 rounded-full bg-white/20 mx-auto mt-2.5 shrink-0" />
            <div className="px-4 py-3">
              <div className="flex items-start justify-between gap-3 mb-3">
                <h2 className="text-lg font-semibold text-text">Поделиться треком</h2>
                <button
                  type="button"
                  onClick={handleClose}
                  className="p-2 -mr-2 rounded-full text-text-muted hover:text-text active:bg-white/10 touch-manipulation"
                  aria-label="Закрыть"
                >
                  <X className="h-5 w-5" />
                </button>
              </div>
              <p className="text-[13px] text-text-muted truncate mb-4">{track.title} · {track.artist}</p>
              <ul className="space-y-2">
                <li>
                  <button
                    type="button"
                    className="w-full flex items-center gap-3 p-3 rounded-2xl active:bg-white/10 text-left touch-manipulation border border-white/10"
                    onClick={shareToStory}
                    disabled={busy !== null}
                  >
                    <span className="w-10 h-10 rounded-xl bg-white/15 flex items-center justify-center shrink-0">
                      <Camera className="h-5 w-5 text-text" />
                    </span>
                    <div className="flex-1 min-w-0">
                      <span className="font-medium text-text block">В историю</span>
                      <span className="text-[12px] text-text-muted">Ссылка будет в подписи</span>
                    </div>
                    {busy === "story" && <span className="text-text-muted text-sm">...</span>}
                  </button>
                </li>
                <li>
                  <button
                    type="button"
                    className="w-full flex items-center gap-3 p-3 rounded-2xl active:bg-white/10 text-left touch-manipulation border border-white/10"
                    onClick={shareToChat}
                    disabled={busy !== null}
                  >
                    <span className="w-10 h-10 rounded-xl bg-white/15 flex items-center justify-center shrink-0">
                      <MessageCircle className="h-5 w-5 text-text" />
                    </span>
                    <div className="flex-1 min-w-0">
                      <span className="font-medium text-text block">Пользователям</span>
                      <span className="text-[12px] text-text-muted">Отправить в чат с кнопкой «Слушать»</span>
                    </div>
                  </button>
                </li>
              </ul>
            </div>
          </motion.div>
        </>
      ) : null}
    </AnimatePresence>
  );

  return createPortal(content, document.body);
}
