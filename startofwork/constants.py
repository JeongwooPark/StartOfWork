"""앱 전역 상수."""

from datetime import time as dt_time

APP_VERSION = "1.3.4"
APP_TITLE = "출근 근태 자동 실행"
# GitHub Releases 업데이트 (1.2.0+)
GITHUB_REPO_OWNER = "JeongwooPark"
GITHUB_REPO_NAME = "StartOfWork"
GITHUB_RELEASES_LATEST_URL = (
    f"https://api.github.com/repos/{GITHUB_REPO_OWNER}/{GITHUB_REPO_NAME}/releases/latest"
)
UPDATE_SETUP_NAME_TEMPLATE = "StartOfWorkSetup-{version}.exe"
UPDATE_USER_AGENT = f"StartOfWork/{APP_VERSION}"
# 매일 정기 업데이트 확인 시각 (로컬 시각)
UPDATE_CHECK_TIME = dt_time(1, 0)
# 폴링: 활성(출근/퇴근 임박)·대기·한산 구간
CHECK_INTERVAL_MS = 1000
CHECK_INTERVAL_IDLE_MS = 5000
CHECK_INTERVAL_QUIET_MS = 15000
POLL_BOUNDARY_WINDOW_SEC = 120

# 1.1.3 이하·구 config 마이그레이션용 기본 근태 URL (신규 설치는 빈 값으로 최초 입력)
DEFAULT_ATTENDANCE_URL = (
    "https://smartplanning.daouoffice.com/ehr/app/attend/my-attendance-status"
)
CHECK_IN_BUTTON_XPATH = (
    "/html/body/div[3]/div/main/div[1]/div[1]/div[3]/div[3]/button[1]/span"
)
CHECK_OUT_BUTTON_XPATH = (
    "/html/body/div[3]/div/main/div[1]/div[1]/div[3]/div[3]/button[2]/span"
)
ATTENDANCE_PAGE_WAIT_SEC = 60
CHECK_IN_RENDER_WAIT_SEC = 45
# driver.get() 상한 — eager 전략과 함께 리소스 행을 끊고 폼 대기로 넘긴다
PAGE_LOAD_TIMEOUT_SEC = 45
LOGIN_FORM_WAIT_SEC = 45
# config.json 기본값 (실제 값은 config에서 로드)
DEFAULT_ACTIVE_START_TIME = dt_time(8, 30)
DEFAULT_ACTIVE_END_TIME = dt_time(18, 0)
DEFAULT_AUTO_CHECKOUT_TIME = dt_time(18, 0)

HOLIDAY_API_URL = (
    "https://apis.data.go.kr/B090041/openapi/service/SpcdeInfoService/getRestDeInfo"
)
HOLIDAY_SERVICE_KEY = (
    "znknClAY/dhPMDrO40Yk0kPs8GxiPP8kiTO0YiybQPYkJa7+"
    "Tyl+KkvE07Mw6MNhsFyqz10LBN8vs3WIkZ6asQ=="
)
# 새벽 API 장애(점검 등) 시 재시도 시각
HOLIDAY_API_RETRY_TIME = dt_time(8, 0)

GWL_STYLE = -16
WS_MAXIMIZEBOX = 0x00010000
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOZORDER = 0x0004
SWP_FRAMECHANGED = 0x0020
