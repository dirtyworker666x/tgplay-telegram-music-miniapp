#!/usr/bin/env node
/**
 * Нагрузочный тест ёмкости: симуляция N одновременных пользователей.
 * Каждый "пользователь": поиск (cache miss) + resolve первого трека.
 * Цель: проверить реальный пик по VK API и limit_concurrency=200.
 *
 * Использование:
 *   BASE_URL=https://tgplay.fun CONCURRENT=80 DURATION=60 node scripts/load-test-capacity.mjs
 *   CONCURRENT=120 DURATION=90 node scripts/load-test-capacity.mjs  # против прод-сервера
 *
 * Переменные:
 *   BASE_URL     — базовый URL API (default: https://tgplay.fun)
 *   CONCURRENT   — число одновременных "пользователей" (default: 50)
 *   DURATION     — длительность фазы в секундах (default: 60)
 *   CACHE_BUST   — 1 = уникальный запрос каждый раз (жёсткий cache miss), 0 = общий пул запросов (default: 1)
 *   LOAD_TEST_SECRET — если задан и на сервере в .env тот же LOAD_TEST_SECRET, запросы поиска идут с заголовком X-Load-Test (обход rate limit для теста)
 */

const BASE = process.env.BASE_URL || "https://tgplay.fun";
const CONCURRENT = Math.max(1, parseInt(process.env.CONCURRENT || "50", 10));
const DURATION_SEC = Math.max(10, parseInt(process.env.DURATION || "60", 10));
const CACHE_BUST = process.env.CACHE_BUST !== "0";
const LOAD_TEST_SECRET = (process.env.LOAD_TEST_SECRET || "").trim();
const LOAD_TEST_HEADERS = LOAD_TEST_SECRET ? { "X-Load-Test": LOAD_TEST_SECRET } : {};

// Запросы, по которым VK точно отдаёт результаты (для cache bust добавляем суффикс)
const SEARCH_QUERIES = [
  "beatles", "muse", "radiohead", "ac dc", "queen", "nirvana", "coldplay", "oasis",
  "linkin park", "metallica", "rammstein", "red hot", "pink floyd", "doors", "led zeppelin",
  "depeche mode", "kino", "ddt", "bi-2", "mumiy troll", "spleen", "agatha christie",
  "noize mc", "basta", "scriptonite", "elka", "max korzh", "matrang", "miyagi",
  "rihanna", "taylor swift", "adele", "beyonce", "eminem", "dr dre", "snoop",
  "billie eilish", "dua lipa", "the weeknd", "ed sheeran", "bruno mars", "imagine dragons",
];

function pickQuery(seed) {
  const q = SEARCH_QUERIES[seed % SEARCH_QUERIES.length];
  return CACHE_BUST ? `${q} ${seed}` : q;
}

function percentile(sortedArr, p) {
  if (sortedArr.length === 0) return 0;
  const idx = Math.ceil((p / 100) * sortedArr.length) - 1;
  return sortedArr[Math.max(0, idx)];
}

async function oneUser(userIndex, startTs, endTs, results) {
  const searchLatencies = [];
  const resolveLatencies = [];
  let searchOk = 0, searchFail = 0, resolveOk = 0, resolveFail = 0;
  let iteration = 0;

  while (Date.now() < endTs) {
    const query = pickQuery(userIndex * 10000 + iteration);
    iteration++;

    // Search
    const searchStart = performance.now();
    let searchResp;
    try {
      searchResp = await fetch(
        `${BASE}/api/music/search?q=${encodeURIComponent(query)}&limit=5`,
        { signal: AbortSignal.timeout(30000), headers: LOAD_TEST_HEADERS }
      );
    } catch (e) {
      searchFail++;
      results.searchFail++;
      continue;
    }
    const searchLatency = (performance.now() - searchStart) / 1000;
    searchLatencies.push(searchLatency);
    if (searchResp.ok) {
      searchOk++;
      results.searchOk++;
    } else {
      searchFail++;
      results.searchFail++;
      if (results.statusCodeCounts) results.statusCodeCounts[searchResp.status] = (results.statusCodeCounts[searchResp.status] || 0) + 1;
      continue;
    }

    let data;
    try {
      data = await searchResp.json();
    } catch {
      continue;
    }
    const items = data?.items || [];
    const trackId = items[0]?.id;
    if (!trackId) continue;

    // Resolve
    const resolveStart = performance.now();
    let resolveResp;
    try {
      resolveResp = await fetch(
        `${BASE}/api/music/resolve/${encodeURIComponent(trackId)}`,
        { signal: AbortSignal.timeout(15000) }
      );
    } catch (e) {
      resolveFail++;
      results.resolveFail++;
      continue;
    }
    const resolveLatency = (performance.now() - resolveStart) / 1000;
    resolveLatencies.push(resolveLatency);
    if (resolveResp.ok) {
      resolveOk++;
      results.resolveOk++;
    } else {
      resolveFail++;
      results.resolveFail++;
    }
  }

  return {
    searchLatencies,
    resolveLatencies,
    searchOk,
    searchFail,
    resolveOk,
    resolveFail,
    iterations: iteration,
  };
}

function report(name, latencies, ok, fail) {
  const total = ok + fail;
  const sorted = [...latencies].sort((a, b) => a - b);
  const p50 = percentile(sorted, 50);
  const p95 = percentile(sorted, 95);
  const p99 = percentile(sorted, 99);
  const errPct = total ? (fail / total * 100).toFixed(1) : "0";
  console.log(
    `  ${name}: ok=${ok} fail=${fail} err%=${errPct} req/s=${(total / DURATION_SEC).toFixed(1)} ` +
    `p50=${p50.toFixed(2)}s p95=${p95.toFixed(2)}s p99=${p99.toFixed(2)}s`
  );
}

async function main() {
  console.log("Load test: capacity (concurrent users)\n");
  console.log(`  BASE_URL=${BASE}`);
  console.log(`  CONCURRENT=${CONCURRENT} simulated users`);
  console.log(`  DURATION=${DURATION_SEC}s`);
  console.log(`  CACHE_BUST=${CACHE_BUST} (unique query per request)\n`);

  const startTs = Date.now();
  const endTs = startTs + DURATION_SEC * 1000;

  const results = {
    searchOk: 0,
    searchFail: 0,
    resolveOk: 0,
    resolveFail: 0,
    statusCodeCounts: {}, // for first-phase failures
  };

  const workers = [];
  for (let i = 0; i < CONCURRENT; i++) {
    workers.push(oneUser(i, startTs, endTs, results));
  }

  const perUser = await Promise.all(workers);
  const elapsed = (Date.now() - startTs) / 1000;

  const allSearchLat = perUser.flatMap((u) => u.searchLatencies);
  const allResolveLat = perUser.flatMap((u) => u.resolveLatencies);
  const totalSearch = results.searchOk + results.searchFail;
  const totalResolve = results.resolveOk + results.resolveFail;

  console.log("--- Results ---");
  if (Object.keys(results.statusCodeCounts).length > 0) {
    console.log("  HTTP status of failed search requests:", Object.entries(results.statusCodeCounts).sort((a, b) => b[1] - a[1]).map(([c, n]) => `${c}:${n}`).join(", "));
    if (results.statusCodeCounts[429]) console.log("  (429 = per-IP rate limit on search, 40/60s from one IP)");
  }
  report("search ", allSearchLat, results.searchOk, results.searchFail);
  report("resolve", allResolveLat, results.resolveOk, results.resolveFail);
  console.log(`  elapsed: ${elapsed.toFixed(1)}s`);
  console.log(`  total search requests:  ${totalSearch}`);
  console.log(`  total resolve requests: ${totalResolve}`);
  const totalErr = results.searchFail + results.resolveFail;
  const totalReq = totalSearch + totalResolve;
  const errPct = totalReq ? (totalErr / totalReq * 100).toFixed(1) : "0";
  console.log(`  overall error %: ${errPct}`);

  const searchReqPerSec = totalSearch / elapsed;
  const resolveReqPerSec = totalResolve / elapsed;
  console.log("\n--- Throughput ---");
  console.log(`  search  ≈ ${searchReqPerSec.toFixed(1)} req/s (hits VK on cache miss)`);
  console.log(`  resolve ≈ ${resolveReqPerSec.toFixed(1)} req/s (hits VK on cache miss)`);
  console.log(`  Combined VK load ≈ ${(searchReqPerSec + resolveReqPerSec).toFixed(1)} req/s`);
  console.log("  (With 7 tokens × 2.5 RPS = 17.5 req/s VK limit; above that → queue/503)\n");

  if (parseFloat(errPct) > 5) {
    console.log(`⚠️  At CONCURRENT=${CONCURRENT} error rate ${errPct}% — above 5%. Real peak is likely below ${CONCURRENT} users.`);
  } else if (parseFloat(errPct) > 0) {
    console.log(`  At CONCURRENT=${CONCURRENT} errors ${errPct}% — near limit.`);
  } else {
    console.log(`  At CONCURRENT=${CONCURRENT} no errors — can try higher CONCURRENT to find peak.`);
  }
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});
