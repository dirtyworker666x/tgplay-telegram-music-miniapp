from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable


def _candidate_secret_files() -> list[Path]:
    """
    Prefer secrets outside the repo.

    - TGPLAY_SECRETS_FILE: explicit path override
    - ~/.tgplay/secrets.env: per-user secrets
    - /root/.tgplay/secrets.env: server default
    - /etc/tgplay/secrets.env: optional system-wide location
    """
    explicit = (os.getenv("TGPLAY_SECRETS_FILE") or "").strip()
    out: list[Path] = []
    if explicit:
        out.append(Path(explicit))
    home = Path.home()
    out.extend(
        [
            home / ".tgplay" / "secrets.env",
            Path("/root/.tgplay/secrets.env"),
            Path("/etc/tgplay/secrets.env"),
        ]
    )
    # de-dup while preserving order
    seen: set[str] = set()
    deduped: list[Path] = []
    for p in out:
        key = str(p)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(p)
    return deduped


def _parse_env_lines(lines: Iterable[str]) -> dict[str, str]:
    env: dict[str, str] = {}
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        v = v.strip()
        if not k:
            continue
        if (v.startswith('"') and v.endswith('"')) or (v.startswith("'") and v.endswith("'")):
            v = v[1:-1]
        env[k] = v
    return env


def load_secrets_env() -> Path | None:
    """
    Load secrets env file into process environment (idempotent per key).
    Does NOT overwrite existing os.environ keys.

    Returns the Path that was loaded, or None if nothing was loaded.
    """
    for p in _candidate_secret_files():
        try:
            if not p.exists() or not p.is_file():
                continue
            data = _parse_env_lines(p.read_text(errors="ignore").splitlines())
            for k, v in data.items():
                os.environ.setdefault(k, v)
            return p
        except Exception:
            continue
    return None

