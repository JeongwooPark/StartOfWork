"""JSON 파일 원자적 저장·손상 백업."""

from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Optional


def atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    """같은 디렉터리 임시 파일에 쓴 뒤 os.replace로 교체한다."""
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent),
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def backup_corrupt_file(path: Path) -> Optional[Path]:
    """손상·읽기 실패 파일을 timestamp 백업으로 옮긴다."""
    if not path.is_file():
        return None
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = path.with_name(f"{path.name}.corrupt.{stamp}")
    try:
        os.replace(path, backup)
        logging.warning("손상된 파일 백업: %s → %s", path.name, backup.name)
        return backup
    except OSError:
        logging.exception("손상 파일 백업 실패: %s", path)
        return None
