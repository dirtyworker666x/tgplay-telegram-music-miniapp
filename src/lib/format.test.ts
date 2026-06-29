import { describe, it, expect } from "vitest";
import { formatTime, formatDuration } from "./format";

describe("formatTime", () => {
  it("formats seconds as m:ss", () => {
    expect(formatTime(0)).toBe("0:00");
    expect(formatTime(65)).toBe("1:05");
    expect(formatTime(90)).toBe("1:30");
    expect(formatTime(3661)).toBe("61:01");
  });

  it("returns 0:00 for non-finite values", () => {
    expect(formatTime(NaN)).toBe("0:00");
    expect(formatTime(Infinity)).toBe("0:00");
  });
});

describe("formatDuration", () => {
  it("delegates to formatTime", () => {
    expect(formatDuration(125)).toBe("2:05");
  });
});
