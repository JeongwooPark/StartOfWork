"""StartOfWork — Windows 출근 근태 자동 실행."""

from startofwork.app import main, run
from startofwork.constants import APP_VERSION

__all__ = ["main", "run", "APP_VERSION", "__version__"]
__version__ = APP_VERSION
