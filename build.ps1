# StartOfWork 빌드 스크립트
# 사용법:
#   .\build.ps1              # exe만 빌드
#   .\build.ps1 -Installer   # exe + Windows 설치 파일(StartOfWorkSetup-<version>.exe)

param(
    [switch]$Installer
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$AppVersion = "1.1.4"
$VersionedExeName = "StartOfWork-$AppVersion.exe"
$VersionedSetupName = "StartOfWorkSetup-$AppVersion.exe"

function Find-Iscc {
    $cmd = Get-Command iscc -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }

    $candidates = @(
        "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
        "${env:LocalAppData}\Programs\Inno Setup 6\ISCC.exe",
        "${env:ProgramFiles}\Inno Setup 6\ISCC.exe",
        "${env:ProgramFiles(x86)}\Inno Setup 7\ISCC.exe",
        "${env:ProgramFiles}\Inno Setup 7\ISCC.exe"
    )
    foreach ($path in $candidates) {
        if (Test-Path $path) { return $path }
    }
    return $null
}

Write-Host "==> 이전 빌드 산출물 정리 (v$AppVersion)"
Remove-Item -Recurse -Force .\build -ErrorAction SilentlyContinue
# dist 전체 삭제는 기존 config.json 보존을 위해 exe/setup만 제거
Remove-Item -Force `
    .\dist\StartOfWork.exe, `
    .\dist\$VersionedExeName, `
    .\dist\StartOfWorkSetup.exe, `
    .\dist\$VersionedSetupName `
    -ErrorAction SilentlyContinue

Write-Host "==> PyInstaller 빌드 (onefile, console off)"
.\.venv\Scripts\pyinstaller.exe `
    --noconfirm `
    --clean `
    .\startofwork.spec

if (-not (Test-Path .\dist\StartOfWork.exe)) {
    throw "빌드 실패: dist\StartOfWork.exe 없음"
}

Copy-Item -Force .\dist\StartOfWork.exe .\dist\$VersionedExeName

$exeSize = (Get-Item .\dist\StartOfWork.exe).Length
Write-Host ("==> 완료: dist\StartOfWork.exe ({0:N1} MB)" -f ($exeSize / 1MB))
Write-Host ("==> 완료: dist\{0} ({1:N1} MB)" -f $VersionedExeName, ($exeSize / 1MB))

# 설치본과 함께 쓸 아이콘 복사
Copy-Item -Force .\StartOfWork.ico .\dist\StartOfWork.ico -ErrorAction SilentlyContinue
Copy-Item -Force .\windows_lock_monitor_icon.ico .\dist\windows_lock_monitor_icon.ico -ErrorAction SilentlyContinue

# dist에 config 예시가 없으면 example 복사 (이미 있으면 유지)
if (-not (Test-Path .\dist\config.json)) {
    Copy-Item .\config.example.json .\dist\config.json
    Write-Host "==> dist\config.json 생성 (예시). 아이디/비밀번호를 수정하세요."
}

if (-not $Installer) {
    Write-Host "설치 파일도 만들려면: .\build.ps1 -Installer"
    exit 0
}

$iscc = Find-Iscc
if (-not $iscc) {
    throw "Inno Setup(ISCC.exe)을 찾을 수 없습니다. winget install JRSoftware.InnoSetup 후 다시 실행하세요."
}

Write-Host "==> Inno Setup 설치 파일 생성 ($VersionedSetupName)"
& $iscc .\installer.iss
if ($LASTEXITCODE -ne 0) {
    throw "Inno Setup 빌드 실패 (exit $LASTEXITCODE)"
}

if (-not (Test-Path .\dist\$VersionedSetupName)) {
    throw "설치 파일 생성 실패: dist\$VersionedSetupName 없음"
}

$setupSize = (Get-Item .\dist\$VersionedSetupName).Length
Write-Host ("==> 완료: dist\{0} ({1:N1} MB)" -f $VersionedSetupName, ($setupSize / 1MB))
Write-Host "설치 시 시작프로그램 등록 + README.md 포함. 계정 입력은 첫 실행 GUI에서 처리합니다."
