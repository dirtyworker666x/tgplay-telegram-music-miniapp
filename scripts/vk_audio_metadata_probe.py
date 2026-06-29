#!/usr/bin/env python3
"""
Полный зонд VK audio.*: сохраняет ВСЕ сырые JSON-ответы на диск + сводку по ключам.

  python3 scripts/vk_audio_metadata_probe.py
  python3 scripts/vk_audio_metadata_probe.py --out-dir ./vk_probe_output
  python3 scripts/vk_audio_metadata_probe.py --max-ids 120 --quiet

Папка по умолчанию: <корень репозитория>/vk_probe_output/ (в .gitignore).
Токен: VK_TOKEN или VK_TOKENS в backend/.env
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from collections import Counter
from pathlib import Path
from typing import Any

API = "https://api.vk.com/method"
V131 = "5.131"
V199 = "5.199"
GETBYID_CHUNK = 60


def load_dotenv(path: Path) -> None:
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if k and k not in os.environ:
            os.environ[k] = v


def get_token() -> str:
    t = (os.environ.get("VK_TOKENS") or os.environ.get("VK_TOKEN") or "").strip()
    if not t:
        return ""
    if "," in t:
        t = t.split(",")[0].strip()
    return t


def user_agent() -> str:
    # Как дефолт в backend/server_lite.py (Kate); для токена официального клиента задайте VK_USER_AGENT.
    return (
        os.getenv("VK_USER_AGENT") or "KateMobileAndroid/56 lite-460 (Android 4.4.2; SDK 19; x86; unknown Android SDK built for x86; en)"
    ).strip()


def vk_get(method: str, params: dict, token: str, ver: str) -> dict:
    p = {**params, "access_token": token, "v": ver}
    url = f"{API}/{method}?" + urllib.parse.urlencode(p, doseq=True)
    h = {"User-Agent": user_agent()}
    xvk = (os.getenv("VK_X_VK_ANDROID_CLIENT") or "new").strip()
    if xvk:
        h["X-VK-Android-Client"] = xvk
    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read().decode("utf-8"))


def audio_id(it: dict) -> str:
    return f"{it['owner_id']}_{it['id']}"


def safe_fs(s: str) -> str:
    s = re.sub(r"[^\w.-]+", "_", s, flags=re.UNICODE)
    return (s[:120] or "x").strip("_")


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def flatten_keys(obj: Any, prefix: str = "") -> list[str]:
    out: list[str] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = f"{prefix}.{k}" if prefix else str(k)
            out.append(p)
            out.extend(flatten_keys(v, p))
    elif isinstance(obj, list) and obj:
        out.extend(flatten_keys(obj[0], prefix + "[0]"))
    return out


def collect_keys_from_audios(items: list[dict]) -> set[str]:
    s: set[str] = set()
    for it in items:
        s.update(flatten_keys(it))
    return s


def chunks(xs: list[str], n: int):
    for i in range(0, len(xs), n):
        yield xs[i : i + n]


def default_queries() -> list[str]:
    return [
        "The Beatles Yesterday",
        "Taylor Swift Anti-Hero",
        "Кино Группа крови",
        "Miyagi Эндшпиль",
        "Ed Sheeran Shape of You",
        "Metallica Nothing Else Matters",
        "Billie Eilish bad guy",
        "Frank Sinatra My Way",
        "BTS Dynamite",
        "Shakira Waka Waka",
        "Земфира жить в твоей голове",
        "Любэ Комбат",
        "Eminem Lose Yourself",
    ]


def main() -> int:
    ap = argparse.ArgumentParser(description="Полный дамп ответов VK audio API")
    ap.add_argument(
        "--out-dir",
        type=Path,
        default=None,
        help="Куда писать JSON (по умолчанию <repo>/vk_probe_output)",
    )
    ap.add_argument("--max-ids", type=int, default=300, help="Максимум id для getById (с начала пула)")
    ap.add_argument("--quiet", action="store_true", help="Не дублировать сводку в stdout, только пути к файлам")
    args = ap.parse_args()

    root = Path(__file__).resolve().parents[1]
    out = args.out_dir or (root / "vk_probe_output")
    load_dotenv(root / "backend" / ".env")
    token = get_token()
    if not token:
        print("Нет VK_TOKEN / VK_TOKENS в окружении или backend/.env", file=sys.stderr)
        return 1

    t0 = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    search_dir = out / "audio.search"
    gid131_dir = out / "audio.getById" / "v5.131"
    gid199_dir = out / "audio.getById" / "v5.199"
    lyr_dir = out / "audio.getLyrics"
    art_dir = out / "audio.getArtistById"
    methods_log: list[dict] = []

    def log_call(name: str, extra: dict) -> None:
        methods_log.append({"method": name, "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **extra})

    seen: set[str] = set()
    pool: list[dict] = []
    queries = default_queries()

    for i, q in enumerate(queries, 1):
        raw = vk_get(
            "audio.search",
            {"q": q, "count": 25, "sort": 2, "auto_complete": 0, "search_own": 0},
            token,
            V131,
        )
        fn = search_dir / f"{i:02d}_{safe_fs(q)}.json"
        write_json(fn, {"query": q, "full_response": raw})
        log_call("audio.search", {"file": str(fn.relative_to(out)), "items": len((raw.get("response") or {}).get("items") or [])})
        if not args.quiet:
            print(f"search «{q}» → {fn.relative_to(out)}")
        if "error" in raw:
            continue
        for it in (raw.get("response") or {}).get("items") or []:
            aid = audio_id(it)
            if aid in seen:
                continue
            seen.add(aid)
            pool.append(it)

    ids = [audio_id(it) for it in pool][: max(1, args.max_ids)]

    all_131: list[dict] = []
    all_199: list[dict] = []

    for bi, batch in enumerate(chunks(ids, GETBYID_CHUNK)):
        audios = ",".join(batch)
        for ver, subdir, acc in [(V131, gid131_dir, all_131), (V199, gid199_dir, all_199)]:
            data = vk_get("audio.getById", {"audios": audios}, token, ver)
            path = subdir / f"batch_{bi:03d}.json"
            write_json(path, {"audios_param": audios, "full_response": data})
            log_call(f"audio.getById@{ver}", {"file": str(path.relative_to(out)), "batch": bi})
            if not args.quiet:
                print(f"getById {ver} batch {bi} → {path.relative_to(out)}")
            if "error" in data:
                continue
            items = data.get("response") or []
            if isinstance(items, list):
                acc.extend(items)

    # Все getLyrics: любой lyrics_id из v5.131 объектов (включая 1)
    lyrics_ids_seen: set[int] = set()
    for it in all_131:
        lid = it.get("lyrics_id")
        if isinstance(lid, int):
            lyrics_ids_seen.add(lid)
    for lid in sorted(lyrics_ids_seen):
        lr = vk_get("audio.getLyrics", {"lyrics_id": lid}, token, V131)
        path = lyr_dir / f"lyrics_id_{lid}.json"
        write_json(path, {"lyrics_id": lid, "full_response": lr})
        log_call("audio.getLyrics", {"file": str(path.relative_to(out)), "lyrics_id": lid})
        if not args.quiet:
            print(f"getLyrics id={lid} → {path.relative_to(out)}")

    # Все уникальные main_artists из v5.131
    done_art: set[str] = set()
    for it in all_131:
        for a0 in it.get("main_artists") or []:
            if not isinstance(a0, dict):
                continue
            aid = str(a0.get("id") or "")
            if not aid or aid in done_art:
                continue
            done_art.add(aid)
            ar = vk_get("audio.getArtistById", {"artist_id": aid}, token, V131)
            path = art_dir / f"artist_{safe_fs(aid)}.json"
            write_json(
                path,
                {"artist_id": aid, "name_from_audio": a0.get("name"), "full_response": ar},
            )
            log_call("audio.getArtistById", {"file": str(path.relative_to(out)), "artist_id": aid})
    if not args.quiet:
        print(f"getArtistById ×{len(done_art)} → {art_dir.relative_to(out)}/")

    pop = vk_get("audio.getPopular", {"count": 100}, token, V131)
    pop_path = out / "audio.getPopular.json"
    write_json(pop_path, {"count_requested": 100, "full_response": pop})
    log_call("audio.getPopular", {"file": str(pop_path.relative_to(out))})
    if not args.quiet:
        print(f"getPopular → {pop_path.relative_to(out)}")

    keys_131 = collect_keys_from_audios(all_131)
    keys_199 = collect_keys_from_audios(all_199)
    pop_items = (pop.get("response") or []) if isinstance(pop.get("response"), list) else []
    keys_pop = collect_keys_from_audios(pop_items) if pop_items else set()

    artist_keys: set[str] = set()
    for p in art_dir.glob("*.json"):
        doc = json.loads(p.read_text(encoding="utf-8"))
        body = (doc.get("full_response") or {}).get("response")
        if isinstance(body, dict):
            artist_keys.update(flatten_keys(body))

    lid_search = Counter()
    for it in pool:
        lid = it.get("lyrics_id")
        lid_search[lid if lid is not None else "(нет)"] += 1
    lid_byid = Counter()
    for it in all_131:
        lid = it.get("lyrics_id")
        lid_byid[lid if lid is not None else "(нет)"] += 1

    years = []
    for it in all_131:
        alb = it.get("album") or {}
        if isinstance(alb, dict) and alb.get("year") is not None:
            years.append({"year": alb.get("year"), "track_id": audio_id(it), "artist": it.get("artist"), "title": it.get("title")})

    summary = {
        "generated_at_utc": t0,
        "unique_tracks_in_pool": len(pool),
        "getById_ids_requested": len(ids),
        "objects_returned_v131": len(all_131),
        "objects_returned_v199": len(all_199),
        "lyrics_id_histogram_search": dict(lid_search),
        "lyrics_id_histogram_after_getById_v131": dict(lid_byid),
        "unique_flat_keys_audio_v131": sorted(keys_131),
        "unique_flat_keys_audio_v199": sorted(keys_199),
        "keys_only_in_v199": sorted(keys_199 - keys_131),
        "keys_only_in_v131": sorted(keys_131 - keys_199),
        "unique_flat_keys_audio_getPopular": sorted(keys_pop),
        "unique_flat_keys_getArtistById_response": sorted(artist_keys),
        "album_year_found": years,
        "methods_called": methods_log,
        "note": "Сырые ответы — в подпапках; здесь только агрегаты. Токен в файлы не пишется.",
    }
    summary_path = out / "summary.json"
    write_json(summary_path, summary)

    merged_tracks = out / "merged_all_tracks_getById_v131.json"
    write_json(merged_tracks, all_131)
    merged_199 = out / "merged_all_tracks_getById_v199.json"
    write_json(merged_199, all_199)

    meta_path = out / "meta.json"
    write_json(
        meta_path,
        {
            "created_utc": t0,
            "out_dir": str(out.resolve()),
            "queries": queries,
            "files": {
                "summary": str(summary_path.relative_to(out)),
                "merged_v131": str(merged_tracks.relative_to(out)),
                "merged_v199": str(merged_199.relative_to(out)),
                "getPopular": str(pop_path.relative_to(out)),
            },
        },
    )

    print("\n=== Готово ===")
    print(f"Каталог: {out.resolve()}")
    print(f"Сводка:  {summary_path}")
    print(f"Все треки getById v5.131 одним файлом: {merged_tracks.name} ({len(all_131)} шт.)")
    print(f"Все треки getById v5.199 одним файлом: {merged_199.name} ({len(all_199)} шт.)")
    print(f"Ключей (плоских) v131: {len(keys_131)}, v199: {len(keys_199)}, только в v199: {len(keys_199 - keys_131)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
