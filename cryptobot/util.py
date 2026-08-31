from __future__ import annotations

import json
import os
import urllib.request
from pathlib import Path


def read_secret(name: str, getenv=os.getenv) -> str:
    """Значення секрету з ``NAME`` або з файлу, вказаного в ``NAME_FILE``.

    Патерн Docker secrets: ключі кладуться у ``/run/secrets/...``, а в середовищі
    вказується лише шлях. Пряме значення має пріоритет.
    """
    direct = (getenv(name, "") or "").strip()
    if direct:
        return direct
    path = (getenv(f"{name}_FILE", "") or "").strip()
    if path:
        try:
            return Path(path).read_text(encoding="utf-8").strip()
        except OSError:
            return ""
    return ""


def fetch_json(url: str):
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "CryptoBOT-MVP/0.1", "Accept": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=12) as response:
        return json.load(response)


def number(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default
