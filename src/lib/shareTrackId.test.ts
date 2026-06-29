import { describe, it, expect } from "vitest";
import {
  canonicalPlaylistTrackId,
  getShareableTrackId,
  getStartParamTrackId,
  parseStartParamTrackId,
} from "./shareTrackId";

describe("getShareableTrackId", () => {
  it("keeps VK owner_audio id", () => {
    expect(getShareableTrackId("-123_456")).toBe("-123_456");
    expect(getShareableTrackId("123_456789")).toBe("123_456789");
  });

  it("keeps bare 11-char id", () => {
    expect(getShareableTrackId("dQw4w9WgXcQ")).toBe("dQw4w9WgXcQ");
  });

  it("extracts video id from watch URL", () => {
    expect(getShareableTrackId("https://www.youtube.com/watch?v=dQw4w9WgXcQ")).toBe("dQw4w9WgXcQ");
  });

  it("extracts video id from short youtu.be URL", () => {
    expect(getShareableTrackId("https://youtu.be/dQw4w9WgXcQ")).toBe("dQw4w9WgXcQ");
  });

  it("keeps SoundCloud id", () => {
    expect(getShareableTrackId("sc:2210782439")).toBe("sc:2210782439");
    expect(getShareableTrackId("sc_2210782439")).toBe("sc:2210782439");
  });
});

describe("start param track id", () => {
  it("uses underscore for SoundCloud in startapp", () => {
    expect(getStartParamTrackId("sc:12345")).toBe("sc_12345");
  });

  it("parses legacy and new start param", () => {
    expect(parseStartParamTrackId("sc_12345")).toBe("sc:12345");
    expect(parseStartParamTrackId("sc:12345")).toBe("sc:12345");
  });
});

describe("canonicalPlaylistTrackId", () => {
  it("strips VK access_key suffix", () => {
    expect(canonicalPlaylistTrackId("123_456789_abcXYZ")).toBe("123_456789");
  });

  it("matches YouTube URL to bare id", () => {
    expect(canonicalPlaylistTrackId("https://www.youtube.com/watch?v=dQw4w9WgXcQ")).toBe("dQw4w9WgXcQ");
  });
});
