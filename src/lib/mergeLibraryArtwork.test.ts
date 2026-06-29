import { describe, it, expect } from "vitest";
import { mergeLibraryArtworkIntoTracks } from "./mergeLibraryArtwork";
import type { Track } from "../types";

describe("mergeLibraryArtworkIntoTracks", () => {
  const lib: Track[] = [{ id: "10_20", title: "A", artist: "B", artwork: "https://cover" }];

  it("fills missing artwork from library by id", () => {
    const search: Track[] = [{ id: "10_20", title: "A", artist: "B" }];
    expect(mergeLibraryArtworkIntoTracks(search, [lib])).toEqual([
      { id: "10_20", title: "A", artist: "B", artwork: "https://cover" },
    ]);
  });

  it("does not override search artwork", () => {
    const search: Track[] = [{ id: "10_20", title: "A", artist: "B", artwork: "https://from-search" }];
    expect(mergeLibraryArtworkIntoTracks(search, [lib])).toEqual(search);
  });

  it("skips empty library artwork", () => {
    const bare: Track[] = [{ id: "10_20", title: "A", artist: "B" }];
    expect(mergeLibraryArtworkIntoTracks(bare, [[{ id: "10_20", title: "A", artist: "B" }]])).toEqual(bare);
  });

  it("first library source wins for duplicate ids", () => {
    const a: Track[] = [{ id: "1_1", title: "x", artist: "y", artwork: "https://first" }];
    const b: Track[] = [{ id: "1_1", title: "x", artist: "y", artwork: "https://second" }];
    expect(mergeLibraryArtworkIntoTracks([{ id: "1_1", title: "x", artist: "y" }], [a, b])[0].artwork).toBe(
      "https://first",
    );
  });
});
