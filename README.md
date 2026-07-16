# 출근 근태 자동 실행 (StartOfWork) v1.2.1

Windows에서 다우오피스 근태를 **자동 출근·퇴근**하는 프로그램입니다.  
잠금 해제뿐 아니라 **부팅/로그인 직후**에도 오늘 미출근이면 1회 출근을 시도합니다.

**버전:** 1.2.1

> **배포 라인:** 1.1.3까지는 수동 배포(old) 라인입니다.  
> **1.2.0부터** GitHub Releases 기반 **자동 업데이트**를 사용합니다.

## 주요 기능

- **자동 출근**
  - **잠금 → 잠금 해제** 전환 시
  - **부팅/로그인 후 프로그램 시작** 시 (프로세스당 1회)
  - **업무시간 시작 시각 진입** 시 (날짜당 1회, 예: 08:20 로그인 → 08:30에 출근)
  - 조건: 설정한 업무시간 · 평일 · 오늘 아직 미출근
- **하루 1회 출근/퇴근**: `check_in_state.json`에 기록해 중복 방지
- **자동 퇴근**: GUI에서 활성화하면 지정 시각 이후 headless Chrome으로 **퇴근하기** 클릭 (잠금 화면에서도 동작)
- **업무시간 설정**: `config.json` / GUI에서 시작·종료 시각 변경 가능 (기본 `08:30`~`18:00`)
- **공휴일 판별**: [한국천문연구원 특일정보 Open API](https://www.data.go.kr/data/15012690/openapi.do) (매일 확인, 내용 변경 시에만 캐시 갱신)
- **주말/공휴일 제외**: 토·일·공휴일에는 출근·퇴근 자동화 생략
- **최초 설정**: 설치 후 첫 실행 시 **① 근태 페이지 URL → ② 아이디/비밀번호 → 로그인·근태 페이지 검증 후 저장**
- **자동 업데이트 (1.2.0+)**: GitHub Releases에서 `StartOfWorkSetup-x.y.z.exe` 확인·다운로드·무인 설치
- **시스템 트레이**: 시작 시 트레이로 최소화, 툴팁에 출근체크 상태 표시
- **완료 알림**: 출근/퇴근 처리 성공 시 시스템 트레이 알림 표시
- **중복 실행 방지**: 이미 실행 중이면 경고 후 추가 인스턴스를 시작하지 않음
- **GUI 상태**: 잠금 상태, 공휴일, 출근/퇴근 결과, 업무시간·자동 퇴근 설정

## 요구 사항

- Windows 10/11
- Google Chrome
- Python 3.13+ (소스 실행 시)
- [uv](https://github.com/astral-sh/uv) 권장

## 설치 (권장: 설치 파일)

```powershell
uv sync
.\build.ps1 -Installer
```

결과물: `dist\StartOfWorkSetup-1.2.1.exe`

| 항목 | 내용 |
|------|------|
| 설치 경로 | `%LOCALAPPDATA%\StartOfWork` (관리자 권한 불필요) |
| 시작프로그램 | 설치 시 **항상** 등록 (`HKCU\Software\Microsoft\Windows\CurrentVersion\Run`) |
| 사용 설명서 | `README.md`를 설치 폴더에 포함 (시작 메뉴에도 바로가기) |
| 설정 파일 | 최초 설치 시 빈 `config.json` 생성. **재설치 시 기존 설정은 덮어쓰지 않음** |
| 계정·URL 입력 | **설치 과정에서는 생략**. 첫 실행 시 GUI에서 근태 URL → 아이디/비밀번호 입력·검증 |
| 설치 후 실행 | 설치 마법사가 앱을 자동 실행하지 않음(Smart App Control 차단 방지). **시작 메뉴** 또는 설치 폴더의 `StartOfWork.exe`를 직접 실행 |
| 제거 | Windows 설정 → 앱 → StartOfWork (시작프로그램 등록도 함께 해제) |

설치 직후 실행하면, 설정이 비어 있을 때 GUI에서 근태 주소와 로그인 정보를 순서대로 받습니다.

## 실행 파일만 빌드

```powershell
.\build.ps1
```

결과물: `dist\StartOfWork.exe`, `dist\StartOfWork-1.2.1.exe` (약 29MB).  
`config.json`, `StartOfWork.ico`를 exe와 **같은 폴더**에 두면 됩니다.

포터블로 시작프로그램만 등록하려면:

```powershell
.\register_startup.ps1
# 해제: .\register_startup.ps1 -Remove
```

## 소스에서 실행

```powershell
cd d:\py_workspace\StartOfWork
uv sync
copy config.example.json config.json
# 또는 실행 후 GUI에서 계정 입력
uv run python main.py
```

## 설정 파일 (`config.json`)

실행 파일(또는 소스)과 **같은 폴더**의 `config.json`을 사용합니다.

```json
{
  "attendance_url": "",
  "username": "",
  "password": "",
  "active_start_time": "08:30",
  "active_end_time": "18:00",
  "auto_checkout_enabled": false,
  "auto_checkout_time": "18:00",
  "update_check_enabled": true
}
```

| 항목 | 설명 |
|------|------|
| `attendance_url` | 다우오피스 근태 페이지 전체 URL (`https://…/my-attendance-status`) |
| `username` | 다우오피스 로그인 아이디 |
| `password` | 다우오피스 로그인 비밀번호 |
| `active_start_time` | 출근 자동화 시작 시각 (`HH:MM`, 기본 `08:30`) |
| `active_end_time` | 출근 자동화 종료 시각 (`HH:MM`, 기본 `18:00`) |
| `auto_checkout_enabled` | 자동 퇴근 사용 여부 (`true` / `false`) |
| `auto_checkout_time` | 자동 퇴근 시각 (`HH:MM`, 기본 `18:00`) |
| `update_check_enabled` | GitHub Releases 업데이트 확인 (`true` / `false`, 기본 `true`) |

- GUI의 **업무시간**, **자동 퇴근 활성화**, **퇴근 시각**은 변경 즉시 저장됩니다.
- 구버전(1.1.3 이하) config에 `attendance_url`이 없고 계정만 있으면, 기존 기본 URL로 자동 보강됩니다.
- 구버전 config에 업무시간 키가 없으면 `08:30`/`18:00`으로 자동 보강됩니다.
- 업무시간 시작 > 종료처럼 잘못된 값이면 기본값으로 되돌립니다.

### 최초 설정 순서

1. **근태 URL** 입력 후 다음
2. **아이디/비밀번호** 입력
3. **확인 및 저장** → 해당 URL로 로그인·근태 버튼 검증 후 `config.json`에 저장

### 자동 업데이트 (1.2.0+)

- 시작 약 5초 후 GitHub Releases **latest**를 1회 확인 (`update_check_enabled`가 `true`일 때)
- **매일 새벽 01:00**에 정기 확인 1회 (앱이 실행 중일 때)
- 새 버전이 있으면 트레이 알림 표시
- 트레이 메뉴 **업데이트 확인** → 결과를 **트레이 알림**으로 표시 (새 버전이면 설치 대화상자)
- 설치 파일: `StartOfWorkSetup-{version}.exe` (Release에 첨부)
- Release 본문에 SHA256이 있으면 다운로드 후 검증
- `config.json`, 출근 상태, `chrome_profile/` 등 사용자 데이터는 설치 시 유지

### 로그인 성공/실패 판정 (GUI 검증 시)

1. 로그인 버튼 클릭 후 URL에 `/login`이 없고, 비밀번호 입력란이 사라지면 **로그인 성공**으로 간주
2. 이어서 근태 페이지에 **출근하기** 또는 **퇴근하기** 버튼이 보이면 최종 성공 → `config.json` 저장  
에러 문구를 파싱하지 않으며, 제한 시간 내 페이지 전환이 안 되면 실패로 처리합니다.

## 동작 요약

### 출근

다음 중 하나에서 조건을 검사합니다.

| 트리거 | 설명 |
|--------|------|
| 부팅/로그인 | 프로그램 시작 약 2초 후, 세션이 잠금 해제이면 **프로세스당 1회** |
| 업무시간 시작 | 업무시간 밖 → 안 으로 바뀌는 순간, 잠금 해제·미출근이면 **당일 1회** |
| 잠금 해제 | 잠금 → 해제 전환 시마다 (당일 미출근일 때) |

공통 조건:

1. 설정한 **업무시간** 안
2. **평일** (토·일·공휴일 아님)
3. 오늘 **아직 출근 처리되지 않음**
4. `username` / `password` 설정됨

충족 시 headless Chrome으로 로그인 후 **출근하기**를 클릭하고 브라우저를 종료합니다.

> 예: 08:20에 로그인해 업무시간(08:30) 전이면 시작 출근은 생략되고,  
> **08:30이 되는 순간** 업무시간 시작 출근이 1회 시도됩니다.

### 퇴근

1. **자동 퇴근 활성화**가 켜져 있어야 합니다.
2. 지정 시각 이후이며, 평일 · 오늘 출근 완료 · 오늘 퇴근 미완료일 때 시도합니다.
3. 잠금 여부와 관계없이 headless로 처리합니다. PC가 절전이면 타이머가 멈출 수 있습니다.

## 생성·참고 파일

| 파일 | 설명 |
|------|------|
| `config.json` | 계정·업무시간·자동 퇴근 설정 |
| `config.example.json` | 설정 예시 |
| `check_in_state.json` | 출근/퇴근 처리 기록 |
| `holiday_cache.json` | 당월 공휴일 캐시 |
| `chrome_profile/` | headless Chrome 세션(쿠키) 디렉터리 — 재로그인 생략용 |
| `lock_state_monitor.log` | 실행 로그 (자정 로테이션, 약 7일 유지) |
| `StartOfWork.ico` | 앱/트레이/설치 아이콘 |

## 소스 구조

```text
main.py                     # 실행 진입점
startofwork/
  app.py                    # mainloop
  gui.py                    # GUI · 트레이 · 부팅 출근
  browser.py                # Selenium 로그인·출근·퇴근
  config.py                 # config.json (계정·업무시간·퇴근)
  holidays.py               # 공휴일 API/캐시
  attendance_state.py       # 출근/퇴근 상태 파일
  rules.py                  # 출근/퇴근 실행 조건
  lock_state.py             # Windows 세션 잠금 상태
  single_instance.py        # 중복 실행 방지 (Named Mutex)
  notifications.py          # 출근/퇴근 완료 트레이 알림
  paths.py / constants.py
tests/test_core.py          # 단위·스모크 테스트
build.ps1 / installer.iss   # exe · 설치 파일 빌드
register_startup.ps1        # 포터블용 시작프로그램 등록
```

## 테스트

```powershell
.\.venv\Scripts\python.exe -m unittest tests.test_core tests.test_updater -v
```

## 의존성

```text
pillow, pystray, requests, selenium
dev: pyinstaller
```

Inno Setup 6+ 가 있으면 `.\build.ps1 -Installer`로 설치 파일까지 생성합니다.

```powershell
winget install JRSoftware.InnoSetup
```

## 패치 노트

### v1.2.1

- 트레이 **업데이트 확인** 결과를 GUI 대화상자 대신 **트레이 알림**으로 표시
- 새 버전이 있을 때만 설치 대화상자 표시
- 산출물: `StartOfWork-1.2.1.exe`, `StartOfWorkSetup-1.2.1.exe`

### v1.2.0

- GitHub Releases 기반 자동 업데이트 (방안 A)
- 시작 시 업데이트 1회 확인 + **매일 새벽 01:00** 정기 확인 + 트레이 **업데이트 확인** 메뉴
- `StartOfWorkSetup-x.y.z.exe` 다운로드 → SHA256 검증(있을 때) → 무인 설치 → 재시작
- `config.json`에 `update_check_enabled` 추가 (기본 `true`)
- 산출물: `StartOfWork-1.2.0.exe`, `StartOfWorkSetup-1.2.0.exe`

### v1.1.4

- 다우오피스 근태 페이지 URL을 `config.json`의 `attendance_url`로 분리
- 설치 후 최초 실행: **URL → 아이디/비밀번호 → 검증·저장** 2단계 설정
- 1.1.3 이하 config(계정만 있음)는 기존 기본 URL로 자동 마이그레이션
- 산출물: `StartOfWork-1.1.4.exe`, `StartOfWorkSetup-1.1.4.exe`

### v1.1.3

- GUI 정리: 상단 날짜·시간 표시·「현재 세션 상태」캡션 제거, 「오늘 공휴일」→「공휴일 유무」
- 창 높이를 내용에 맞게 축소해 하단 여백 제거
- 트레이에 있을 때 잠금 상태가 바뀌어도, 창 복원 시 「마지막 상태 변경」시각이 맞게 갱신
- 자동 퇴근 설정 저장이 반복되던 루프 수정 (값 미변경 시 저장·로그 생략)
- 퇴근 시각 전 「출근 기록 없음 — 퇴근 생략」매초 INFO 로그 제거 (조용히 스킵)
- `lock_state_monitor.log` 자정 로테이션 + 약 7일만 유지
- 설치 완료 화면의 README 열기 체크 **기본 해제**, 설치 후 README 강제 표시 페이지 제거
- 산출물: `StartOfWork-1.1.3.exe`, `StartOfWorkSetup-1.1.3.exe`

### v1.1.2

- GUI 닫기(X) 시 **트레이로 이동 / 종료 / 취소** 확인 대화상자 추가
- 트레이 메뉴 「종료」·로그인 설정 취소는 바로 종료 유지
- 산출물: `StartOfWork-1.1.2.exe`, `StartOfWorkSetup-1.1.2.exe`

### v1.1.1

- 설치 직후 Setup이 `StartOfWork.exe`를 자동 실행하지 않도록 변경  
  (서명 없는 exe를 Setup이 띄울 때 Smart App Control **CreateProcess 4551** 차단 방지)
- 설치 후 **시작 메뉴** 또는 설치 폴더에서 직접 실행
- 산출물: `StartOfWork-1.1.1.exe`, `StartOfWorkSetup-1.1.1.exe`

### v1.1.0

**성능·상주 효율**

- 공휴일 Open API를 백그라운드로 조회하고, 시작/자정에는 캐시로 즉시 표시 (`cache_only`)
- 적응형 폴링: 출근·퇴근 임박 1초 / 대기 5초 / 한산 15초
- 트레이 숨김 시 불필요한 UI 라벨 갱신 생략
- 당일 자동 퇴근 시도 후 재호출 억제 (`_checkout_triggered_date`)
- Chrome `--user-data-dir`( `chrome_profile/` )로 세션 유지 → 재로그인 생략 가능
- Chrome 경로 캐시, 버튼 xpath는 텍스트 기반 우선

**알림**

- 출근/퇴근 성공 시 시스템 트레이 알림 표시  
  (PowerShell 토스트는 Smart App Control에 막혀 트레이 `notify`로 전환)

**빌드**

- 버전을 파일명에 명시: `StartOfWork-1.1.0.exe`, `StartOfWorkSetup-1.1.0.exe`

### v1.0.0

- 최초 공개: 잠금 해제·부팅/로그인·업무시간 시작 출근, 자동 퇴근, 공휴일 API, GUI·트레이, 설치본

## 참고

- 대상 URL: `config.json`의 `attendance_url` (최초 실행 시 입력)
- 공휴일 API Decoding 키는 소스에 포함되어 있으며 `requests`로 호출합니다.
- Chrome은 출근/퇴근 모두 **headless**로 동작합니다 (창이 보이지 않음).
- 코드 서명이 없으면 Windows 11 스마트 앱 컨트롤이 Setup의 자동 실행을 막을 수 있습니다. 설치 폴더의 exe를 직접 실행하세요.
