"""StartOfWork — Windows 출근 근태 자동 실행."""

from startofwork.constants import APP_VERSION

__all__ = ["APP_VERSION", "__version__", "main", "run"]
__version__ = APP_VERSION


def main() -> None:
    from startofwork.app import main as _main

    _main()


def run() -> None:
    from startofwork.app import run as _run

    _run()
