from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()
