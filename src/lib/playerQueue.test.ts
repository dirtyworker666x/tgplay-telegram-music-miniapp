import { describe, expect, it } from "vitest";
import { buildQueue, dedupeTracksById, getRandomNextIndex, getRandomNextIndexAvoidingRecent } from "./playerQueue";

const makeTrack = (id: string) => ({ id, title: id, artist: "test" });

describe("buildQueue", () => {
  it("prefers activePlaylistTracks when present", () => {
    const active = [makeTrack("a1"), makeTrack("a2")];
    const search = [makeTrack("s1")];
    const playlist = [makeTrack("p1")];

    const q = buildQueue(active, search, playlist);
    expect(q.map((t) => t.id)).toEqual(["a1", "a2"]);
  });

  it("falls back to search tracks when no active playlist", () => {
    const active = null;
    const search = [makeTrack("s1"), makeTrack("s2")];
    const playlist = [makeTrack("p1")];

    const q = buildQueue(active, search, playlist);
    expect(q.map((t) => t.id)).toEqual(["s1", "s2"]);
  });

  it("falls back to favorites playlist when no active or search tracks", () => {
    const active = null;
    const search: ReturnType<typeof makeTrack>[] = [];
    const playlist = [makeTrack("p1"), makeTrack("p2")];

    const q = buildQueue(active, search, playlist);
    expect(q.map((t) => t.id)).toEqual(["p1", "p2"]);
  });

  it("returns empty queue when no sources", () => {
    const q = buildQueue(null, [], []);
    expect(q).toEqual([]);
  });
});

describe("getRandomNextIndex", () => {
  it("returns -1 for invalid state", () => {
    expect(getRandomNextIndex(-1, 3, () => 0.5)).toBe(-1);
    expect(getRandomNextIndex(0, 0, () => 0.5)).toBe(-1);
    expect(getRandomNextIndex(3, 3, () => 0.5)).toBe(-1);
  });

  it("returns 0 when only one element", () => {
    expect(getRandomNextIndex(0, 1, () => 0.5)).toBe(0);
  });

  it("uses provided random fn and never repeats current index", () => {
    const seq = [0.1, 0.9, 0.4];
    let i = 0;
    const rnd = () => {
      const v = seq[i] ?? 0.5;
      i += 1;
      return v;
    };

    const currentIndex = 1;
    const length = 3;
    const next = getRandomNextIndex(currentIndex, length, rnd);

    expect(next).not.toBe(currentIndex);
    expect(next).toBeGreaterThanOrEqual(0);
    expect(next).toBeLessThan(length);
  });

  it("falls back to linear next when random keeps returning current index", () => {
    const next = getRandomNextIndex(1, 3, () => 1 / 3); // всегда даёт индекс 1
    expect(next).toBe(2);
  });
});

describe("dedupeTracksById", () => {
  it("keeps first occurrence order", () => {
    const q = [makeTrack("a"), makeTrack("b"), makeTrack("a"), makeTrack("c")];
    expect(dedupeTracksById(q).map((t) => t.id)).toEqual(["a", "b", "c"]);
  });
});

describe("getRandomNextIndexAvoidingRecent", () => {
  const q = [makeTrack("a"), makeTrack("b"), makeTrack("c"), makeTrack("d")];

  it("never picks an index whose id is in recent (when alternatives exist)", () => {
    const recent = ["b", "c"];
    for (let k = 0; k < 30; k++) {
      const idx = getRandomNextIndexAvoidingRecent(0, q, recent, Math.random);
      expect(idx).not.toBe(0);
      expect(q[idx]!.id).not.toBe("b");
      expect(q[idx]!.id).not.toBe("c");
    }
  });

  it("falls back when all others are recent", () => {
    const recent = ["a", "b", "c", "d"];
    const idx = getRandomNextIndexAvoidingRecent(0, q, recent, () => 0.5);
    expect(idx).not.toBe(0);
    expect(idx).toBeGreaterThanOrEqual(0);
    expect(idx).toBeLessThan(4);
  });
});

