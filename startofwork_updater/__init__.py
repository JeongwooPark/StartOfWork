"""독립형 StartOfWork 업데이터."""

from __future__ import annotations

from typing import Optional

__all__ = ["run"]


def run(argv: Optional[list[str]] = None) -> int:
    from startofwork_updater.app import main

    return main(argv)
