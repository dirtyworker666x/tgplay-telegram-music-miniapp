import { describe, expect, it } from "vitest";
import { shouldClearRecommendationsLoading } from "./recommendationsRequest";

describe("shouldClearRecommendationsLoading", () => {
  it("returns true when this controller is still the active one", () => {
    const ac = new AbortController();
    const ref: { current: AbortController | null } = { current: ac };
    expect(shouldClearRecommendationsLoading(ac, ref)).toBe(true);
  });

  it("returns false when a newer request replaced the ref", () => {
    const first = new AbortController();
    const second = new AbortController();
    const ref: { current: AbortController | null } = { current: second };
    expect(shouldClearRecommendationsLoading(first, ref)).toBe(false);
  });

  it("returns false when ref was cleared", () => {
    const ac = new AbortController();
    const ref: { current: AbortController | null } = { current: null };
    expect(shouldClearRecommendationsLoading(ac, ref)).toBe(false);
  });
});
