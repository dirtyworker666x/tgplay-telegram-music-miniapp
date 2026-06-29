import { describe, it, expect } from "vitest";
import type { Track } from "../types";
import { getDownloadUrl, looksLikeVkTrackId, youtubeResolveMetaForTrack } from "./api";

/**
 * Регрессия: title/artist в query только для VK owner_audio id.
 * YouTube watch URL в id + meta давал гигантский GET → 414 nginx / поломка поиска.
 */
describe("resolve meta / query string (VK id only)", () => {
  const ytWatch =
    "https://www.youtube.com/watch?v=dQw4w9WgXcQ";

  it("looksLikeVkTrackId false для полного YouTube URL", () => {
    expect(looksLikeVkTrackId(ytWatch)).toBe(false);
  });

  it("youtubeResolveMetaForTrack не отдаёт title/artist для YouTube id", () => {
    const tr: Track = {
      id: ytWatch,
      title: "Long Title ".repeat(40),
      artist: "Artist ".repeat(40),
      vk_legacy: false,
    };
    expect(youtubeResolveMetaForTrack(tr)).toBeUndefined();
  });

  it("youtubeResolveMetaForTrack отдаёт meta для VK id без vk_legacy (избранное)", () => {
    const tr: Track = { id: "-1_999", title: "A", artist: "B" };
    expect(youtubeResolveMetaForTrack(tr)).toEqual({ title: "A", artist: "B" });
  });

  it("getDownloadUrl не дописывает query при явном meta для не-VK id", () => {
    const u = getDownloadUrl(ytWatch, { title: "T", artist: "A" });
    expect(u).not.toContain("title=");
    expect(u).not.toContain("artist=");
  });

  it("looksLikeVkTrackId true для канонического VK id", () => {
    expect(looksLikeVkTrackId("-2000000001_12345")).toBe(true);
  });

  it("youtubeResolveMetaForTrack не отдаёт meta при vk_legacy true", () => {
    const tr: Track = {
      id: "-1_2",
      title: "Song",
      artist: "Band",
      vk_legacy: true,
    };
    expect(youtubeResolveMetaForTrack(tr)).toBeUndefined();
  });
});
