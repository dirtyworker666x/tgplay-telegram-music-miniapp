import { describe, it, expect } from "vitest";
import { normalizeTrack } from "./normalizeTrack";

describe("normalizeTrack", () => {
  it("normalizes id from id, trackId or _id", () => {
    expect(normalizeTrack({ id: "1_2" }).id).toBe("1_2");
    expect(normalizeTrack({ trackId: "3_4" }).id).toBe("3_4");
    expect(normalizeTrack({ _id: "5_6" }).id).toBe("5_6");
    expect(normalizeTrack({}).id).toBe("");
  });

  it("normalizes title and artist with fallbacks", () => {
    expect(normalizeTrack({ id: "1_2", title: "Foo", artist: "Bar" })).toMatchObject({
      title: "Foo",
      artist: "Bar",
    });
    expect(normalizeTrack({ id: "1_2", name: "Baz" })).toMatchObject({ title: "Baz" });
    expect(normalizeTrack({ id: "1_2", artist_name: "Qux" })).toMatchObject({ artist: "Qux" });
    expect(normalizeTrack({ id: "1_2", author: "Auth" })).toMatchObject({ artist: "Auth" });
    expect(normalizeTrack({ id: "1_2" })).toMatchObject({
      title: "Unknown title",
      artist: "Unknown artist",
    });
  });

  it("does not force Unknown artist for SoundCloud", () => {
    expect(normalizeTrack({ id: "sc:1", provider: "soundcloud" })).toMatchObject({
      title: "Unknown title",
      artist: "",
      provider: "soundcloud",
    });
  });

  it("normalizes duration from number or string", () => {
    expect(normalizeTrack({ id: "1_2", duration: 180 }).duration).toBe(180);
    expect(normalizeTrack({ id: "1_2", duration: "240" }).duration).toBe(240);
    expect(normalizeTrack({ id: "1_2" }).duration).toBeUndefined();
  });

  it("normalizes artwork from cover_url, artwork, cover or image", () => {
    expect(normalizeTrack({ id: "1_2", cover_url: "https://a" }).artwork).toBe("https://a");
    expect(normalizeTrack({ id: "1_2", artwork: "https://b" }).artwork).toBe("https://b");
    expect(normalizeTrack({ id: "1_2", cover: "https://c" }).artwork).toBe("https://c");
    expect(normalizeTrack({ id: "1_2", image: "https://d" }).artwork).toBe("https://d");
  });

  it("filters empty id to empty string", () => {
    expect(normalizeTrack({ id: "" }).id).toBe("");
  });

  it("normalizes genreId from genre_id or genreId", () => {
    expect(normalizeTrack({ id: "1_2", genre_id: 2 }).genreId).toBe(2);
    expect(normalizeTrack({ id: "1_2", genreId: 5 }).genreId).toBe(5);
    expect(normalizeTrack({ id: "1_2", genre_id: "7" }).genreId).toBe(7);
    expect(normalizeTrack({ id: "1_2" }).genreId).toBeUndefined();
  });
});
