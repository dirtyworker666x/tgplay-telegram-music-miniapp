#!/usr/bin/env node
/**
 * E2E замер производительности: загрузка приложения, поиск → карточки, клик → воспроизведение.
 * Требует собранный фронт и доступный бэкенд (или preview).
 * Запуск: BASE_URL=https://tgplay.fun node scripts/e2e-perf.mjs
 * Или: npm run build && npx vite preview & sleep 3 && BASE_URL=http://localhost:4173 node scripts/e2e-perf.mjs
 */
import { chromium } from "@playwright/test";

const BASE = process.env.BASE_URL || "https://tgplay.fun";
const SEARCH_QUERY = process.env.SEARCH_QUERY || "beatles";
const TIMEOUT_MS = 60000;

async function main() {
  if (process.env.SKIP_E2E_BROWSER === "1") {
    console.log("E2E perf skipped (SKIP_E2E_BROWSER=1). Run: npx playwright install chromium");
    return;
  }
  let browser;
  try {
    browser = await chromium.launch({ headless: true });
  } catch (e) {
    console.error("Playwright browser not found. Run once: npx playwright install chromium");
    process.exit(1);
  }
  const context = await browser.newContext({
    userAgent: "Mozilla/5.0 (Windows NT 10.0; rv:109.0) Gecko/20100101 Firefox/115.0",
    viewport: { width: 390, height: 844 },
  });
  const page = await context.newPage();

  const timings = {
    loadMs: null,
    appReadyMs: null,
    searchToCardsMs: null,
    clickToPlayingMs: null,
  };

  try {
    const navPromise = page.goto(BASE, { waitUntil: "load", timeout: TIMEOUT_MS });
    await navPromise;

    const loadPerf = await page.evaluate(() => {
      const nav = performance.getEntriesByType("navigation")[0];
      if (!nav || !("loadEventEnd" in nav)) return null;
      return nav.loadEventEnd - nav.startTime;
    });
    timings.loadMs = loadPerf;

    const appReady = await page.evaluate(() => {
      const marks = performance.getEntriesByName("app-ready", "mark");
      const nav = performance.getEntriesByType("navigation")[0];
      if (!marks.length || !nav) return null;
      return marks[0].startTime - nav.startTime;
    });
    timings.appReadyMs = appReady;

    const searchStart = Date.now();
    await page.getByTestId("search-input").fill(SEARCH_QUERY, { timeout: 10000 });
    await page.waitForSelector('[data-testid="track-list"] [data-testid="track-row"]', {
      timeout: 15000,
      state: "visible",
    });
    const searchToCards = Date.now() - searchStart;
    timings.searchToCardsMs = searchToCards;

    await page.locator('[data-testid="track-list"] [data-testid="track-row"]').first().click();
    const clickTimePerf = await page.evaluate(() => performance.now());

    await page.waitForFunction(
      () => typeof (window).__audioPlayingAt === "number",
      { timeout: 20000 }
    );
    const audioPlayingAt = await page.evaluate(() => (window).__audioPlayingAt);
    timings.clickToPlayingMs =
      typeof audioPlayingAt === "number" ? Math.max(0, audioPlayingAt - clickTimePerf) : null;
  } catch (err) {
    console.error("E2E perf error:", err.message);
  } finally {
    if (browser) await browser.close();
  }

  console.log("E2E Performance (base: " + BASE + ")\n---");
  if (timings.loadMs != null) console.log("Load (nav to loadEventEnd):", (timings.loadMs / 1000).toFixed(2), "s");
  if (timings.appReadyMs != null) console.log("App ready (mark):", (timings.appReadyMs / 1000).toFixed(2), "s");
  if (timings.searchToCardsMs != null) console.log("Search → cards (visible):", (timings.searchToCardsMs / 1000).toFixed(2), "s");
  if (timings.clickToPlayingMs != null) console.log("Click → audio playing:", (timings.clickToPlayingMs / 1000).toFixed(2), "s");
  console.log("--- done");
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});
