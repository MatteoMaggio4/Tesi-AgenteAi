import os
from pathlib import Path
from typing import Optional

from .config import API_KEY_FALLBACK_FILE


def resolve_api_key(repo_root: Optional[Path] = None) -> Optional[str]:
    """
    Cerca la chiave API in tre posti:
    1. variabile GOOGLE_API_KEY;
    2. file .api_key nella directory corrente;
    3. file .api_key nella root del repository.
    """
    key = os.getenv("GOOGLE_API_KEY")
    if key and key.strip():
        return key.strip()

    candidates = [Path.cwd() / API_KEY_FALLBACK_FILE]
    if repo_root:
        repo_key = repo_root / API_KEY_FALLBACK_FILE
        if repo_key not in candidates:
            candidates.append(repo_key)

    for candidate in candidates:
        try:
            if candidate.exists():
                key = candidate.read_text(encoding="utf-8").strip()
                if key:
                    return key
        except Exception:
            continue

    return None
