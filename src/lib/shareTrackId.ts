/**
 * ID для шеринга и deep link tr_*: SoundCloud (sc:<id>) / VK — как есть;
 * YouTube — только 11-симв. video id (полный URL ломает startapp).
 * В startapp/start_param для SC — sc_<id> без «:» (Telegram иногда обрезает ссылку).
 */
const YT_IN_URL = /(?:v=|youtu\.be\/|shorts\/)([\w-]{11})\b/;
const SC_TRACK_ID = /^sc:(\d+)$/;
const SC_START_PARAM = /^sc_(\d+)$/;

export function getShareableTrackId(rawId: string): string {
  const s = (rawId || "").trim();
  if (!s) return s;
  const sc = SC_TRACK_ID.exec(s);
  if (sc) return `sc:${sc[1]}`;
  const scStart = SC_START_PARAM.exec(s);
  if (scStart) return `sc:${scStart[1]}`;
  if (/^-?\d+_\d+$/.test(s)) return s;
  if (/^[\w-]{11}$/.test(s)) return s;
  const m = s.match(YT_IN_URL);
  if (m) return m[1];
  if (/youtube\.com|youtu\.be/i.test(s)) {
    const cand = s.match(/[\w-]{11}/);
    if (cand && /^[\w-]{11}$/.test(cand[0])) return cand[0];
  }
  return s;
}

/** Токен для startapp=tr_* (без двоеточия у SoundCloud). */
export function getStartParamTrackId(rawId: string): string {
  const canon = getShareableTrackId(rawId);
  const sc = SC_TRACK_ID.exec(canon);
  if (sc) return `sc_${sc[1]}`;
  return canon;
}

/** tr_* / share_tr_* payload → канонический id приложения (sc:<id>). */
export function parseStartParamTrackId(param: string): string {
  const s = (param || "").trim();
  if (!s) return s;
  const scStart = SC_START_PARAM.exec(s);
  if (scStart) return `sc:${scStart[1]}`;
  return getShareableTrackId(s);
}

/** Как на бэкенде в избранном: SC/VK канонизируются, YouTube — 11-симв. id (или из URL). */
export function canonicalPlaylistTrackId(rawId: string): string {
  const s = (rawId || "").trim();
  if (!s) return s;
  const sc = SC_TRACK_ID.exec(s);
  if (sc) return `sc:${sc[1]}`;
  const scStart = SC_START_PARAM.exec(s);
  if (scStart) return `sc:${scStart[1]}`;
  const vk = /^(-?\d+)_(\d+)(?:_[A-Za-z0-9_-]+)?$/.exec(s);
  if (vk) return `${vk[1]}_${vk[2]}`;
  return getShareableTrackId(s);
}
