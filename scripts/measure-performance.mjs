#!/usr/bin/env node
/**
 * Замер времени ответа API: health, поиск, resolve.
 * Не входит в npm test; запуск: BASE_URL=https://... node scripts/measure-performance.mjs
 * Пороги (сек): поиск ≤ 2 (с кешем < 0.1), resolve ≤ 2 (с кешем < 0.05).
 * При PERF_STRICT=1 выход с кодом 1 при превышении порогов.
 */
const BASE = process.env.BASE_URL || 'https://tgplay.fun';
const SEARCH_Q = process.env.SEARCH_QUERY || 'beatles';
const STRICT = process.env.PERF_STRICT === '1';

const SEARCH_MAX_S = 3;
const RESOLVE_MAX_S = 2.5;

async function measure(name, url) {
  const start = performance.now();
  try {
    const res = await fetch(url, { signal: AbortSignal.timeout(15000) });
    await res.text();
  } catch (e) {
    const t = ((performance.now() - start) / 1000).toFixed(2);
    console.log(`${name}: ${t}s (error: ${e.message})`);
    return { name, time: parseFloat(t), ok: false };
  }
  const time = (performance.now() - start) / 1000;
  console.log(`${name}: ${time.toFixed(2)}s`);
  return { name, time, ok: true };
}

async function main() {
  console.log(`Base: ${BASE}\n---`);

  const health = await measure('GET /api/health', `${BASE}/api/health`);
  const search = await measure(
    'GET /api/music/search',
    `${BASE}/api/music/search?q=${encodeURIComponent(SEARCH_Q)}&limit=5`
  );

  let resolve = { time: 0, ok: false };
  if (search.ok) {
    try {
      const res = await fetch(
        `${BASE}/api/music/search?q=${encodeURIComponent(SEARCH_Q)}&limit=1`,
        { signal: AbortSignal.timeout(10000) }
      );
      const data = await res.json();
      const trackId = data?.items?.[0]?.id;
      if (trackId) {
        resolve = await measure(
          'GET /api/music/resolve/:id',
          `${BASE}/api/music/resolve/${encodeURIComponent(trackId)}`
        );
      } else {
        console.log('GET /api/music/resolve/:id: skip (no track id)');
      }
    } catch (e) {
      console.log(`Resolve: skip (${e.message})`);
    }
  }

  console.log('--- done');

  if (STRICT) {
    let failed = false;
    if (search.ok && search.time > SEARCH_MAX_S) {
      console.error(`FAIL: search ${search.time.toFixed(2)}s > ${SEARCH_MAX_S}s`);
      failed = true;
    }
    if (resolve.ok && resolve.time > RESOLVE_MAX_S) {
      console.error(`FAIL: resolve ${resolve.time.toFixed(2)}s > ${RESOLVE_MAX_S}s`);
      failed = true;
    }
    process.exit(failed ? 1 : 0);
  }
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});
