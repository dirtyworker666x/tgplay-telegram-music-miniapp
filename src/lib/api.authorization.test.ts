import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const getInitDataMock = vi.fn(() => "");
const getTelegramUserMock = vi.fn<() => { id: number } | null>(() => null);
const lsStore: Record<string, string> = {};
const lsMock = {
  getItem: (k: string) => (k in lsStore ? lsStore[k]! : null),
  setItem: (k: string, v: string) => {
    lsStore[k] = v;
  },
  removeItem: (k: string) => {
    delete lsStore[k];
  },
  clear: () => {
    for (const k of Object.keys(lsStore)) delete lsStore[k];
  },
  key: (i: number) => Object.keys(lsStore)[i] ?? null,
  get length() {
    return Object.keys(lsStore).length;
  },
};

vi.mock("./telegram", async (importOriginal) => {
  const mod = await importOriginal<typeof import("./telegram")>();
  return {
    ...mod,
    getInitData: () => getInitDataMock(),
    getTelegramUser: () => getTelegramUserMock(),
  };
});

import { WEB_SESSION_STORAGE_KEY, getAuthorizationHeaderValue } from "./api";

describe("getAuthorizationHeaderValue", () => {
  const origLs = globalThis.localStorage;

  beforeEach(() => {
    for (const k of Object.keys(lsStore)) delete lsStore[k];
    Object.defineProperty(globalThis, "localStorage", { value: lsMock, configurable: true });
    getInitDataMock.mockReset();
    getInitDataMock.mockReturnValue("");
    getTelegramUserMock.mockReset();
    getTelegramUserMock.mockReturnValue(null);
  });

  afterEach(() => {
    Object.defineProperty(globalThis, "localStorage", { value: origLs, configurable: true });
  });

  it("uses tma when initData user matches web session user", () => {
    lsMock.setItem(
      WEB_SESSION_STORAGE_KEY,
      JSON.stringify({ accessToken: "web-jwt", user: { id: 1, first_name: "Web" } }),
    );
    getInitDataMock.mockReturnValue("query_id=1&user=x&hash=abc");
    getTelegramUserMock.mockReturnValue({ id: 1 });
    expect(getAuthorizationHeaderValue()).toBe("tma query_id=1&user=x&hash=abc");
  });

  it("uses Bearer when initData user mismatches web session user", () => {
    lsMock.setItem(
      WEB_SESSION_STORAGE_KEY,
      JSON.stringify({ accessToken: "web-jwt", user: { id: 99, first_name: "Web" } }),
    );
    getInitDataMock.mockReturnValue("query_id=1&user=x&hash=abc");
    getTelegramUserMock.mockReturnValue({ id: 1 });
    expect(getAuthorizationHeaderValue()).toBe("Bearer web-jwt");
  });

  it("uses Bearer when there is no initData", () => {
    lsMock.setItem(
      WEB_SESSION_STORAGE_KEY,
      JSON.stringify({ accessToken: "only-bearer", user: { id: 2, first_name: "B" } }),
    );
    expect(getAuthorizationHeaderValue()).toBe("Bearer only-bearer");
  });

  it("returns null without initData and without web session", () => {
    expect(getAuthorizationHeaderValue()).toBeNull();
  });
});
