#!/usr/bin/env python3
"""
Probe artwork coverage across VK and fallback providers.

Usage:
  python3 scripts/artwork_source_probe.py --base https://tgplay.fun --queries "metallica,rammstein,miyagi"
"""
from __future__ import annotations

import argparse
import json
import urllib.parse
import urllib.request


def fetch_json(url: str) -> dict:
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="https://tgplay.fun")
    ap.add_argument("--queries", default="metallica,rammstein,miyagi")
    args = ap.parse_args()

    base = args.base.rstrip("/")
    queries = [q.strip() for q in args.queries.split(",") if q.strip()]
    report = []
    for q in queries:
        u = f"{base}/api/music/search?q={urllib.parse.quote(q)}"
        data = fetch_json(u)
        items = data.get("items") or []
        with_cover = sum(1 for x in items if str(x.get("cover_url") or "").strip())
        report.append(
            {
                "query": q,
                "items": len(items),
                "with_cover": with_cover,
                "coverage": round((with_cover / len(items)) * 100, 2) if items else 0.0,
                "sample": [
                    {
                        "id": t.get("id"),
                        "title": t.get("title"),
                        "artist": t.get("artist"),
                        "cover": bool(str(t.get("cover_url") or "").strip()),
                        "cover_url": (t.get("cover_url") or "")[:140],
                    }
                    for t in items[:5]
                ],
            }
        )
    print(json.dumps({"base": base, "queries": report}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

