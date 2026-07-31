"""StartOfWork — Windows 출근 근태 자동 실행."""

from __future__ import annotations

from typing import Any

__all__ = ["APP_VERSION", "__version__", "main", "run"]


def __getattr__(name: str) -> Any:
    # 패키지 import 시 GUI/selenium을 끌어오지 않도록 지연 로드
    if name in ("APP_VERSION", "__version__"):
        from startofwork.constants import APP_VERSION as version

        return version
    if name == "main":
        from startofwork.app import main as _main

        return _main
    if name == "run":
        from startofwork.app import run as _run

        return _run
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
