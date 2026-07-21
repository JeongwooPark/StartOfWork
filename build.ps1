# StartOfWork 빌드 스크립트
# 사용법:
#   .\build.ps1              # onedir 폴더만 빌드
#   .\build.ps1 -Installer   # onedir + Windows 설치 파일(StartOfWorkSetup-<version>.exe)

param(
    [switch]$Installer
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$AppVersion = "1.2.6"
$AppDirName = "StartOfWork"
$VersionedZipName = "StartOfWork-$AppVersion.zip"
$VersionedSetupName = "StartOfWorkSetup-$AppVersion.exe"
$DistAppDir = Join-Path ".\dist" $AppDirName

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
# dist 전체 삭제는 기존 config.json 보존을 위해 앱 폴더·zip·setup만 제거
Remove-Item -Recurse -Force $DistAppDir -ErrorAction SilentlyContinue
Remove-Item -Force `
    .\dist\StartOfWork.exe, `
    .\dist\$VersionedZipName, `
    .\dist\StartOfWorkSetup.exe, `
    .\dist\$VersionedSetupName `
    -ErrorAction SilentlyContinue

Write-Host "==> PyInstaller 빌드 (onedir, console off)"
.\.venv\Scripts\pyinstaller.exe `
    --noconfirm `
    --clean `
    .\startofwork.spec

$builtExe = Join-Path $DistAppDir "StartOfWork.exe"
if (-not (Test-Path $builtExe)) {
    throw "빌드 실패: $builtExe 없음"
}

# 설치본·포터블과 함께 쓸 아이콘을 앱 폴더 루트에 복사
Copy-Item -Force .\StartOfWork.ico (Join-Path $DistAppDir "StartOfWork.ico") -ErrorAction SilentlyContinue

# 앱 폴더에 config 예시가 없으면 example 복사 (이미 있으면 유지)
$distConfig = Join-Path $DistAppDir "config.json"
if (-not (Test-Path $distConfig)) {
    Copy-Item .\config.example.json $distConfig
    Write-Host "==> dist\StartOfWork\config.json 생성 (예시). 아이디/비밀번호를 수정하세요."
}

# 포터블 zip (폴더 내용)
$zipPath = Join-Path ".\dist" $VersionedZipName
if (Test-Path $zipPath) {
    Remove-Item -Force $zipPath
}
Compress-Archive -Path (Join-Path $DistAppDir "*") -DestinationPath $zipPath -CompressionLevel Optimal

$exeSize = (Get-Item $builtExe).Length
$zipSize = (Get-Item $zipPath).Length
Write-Host ("==> 완료: dist\StartOfWork\StartOfWork.exe ({0:N1} MB)" -f ($exeSize / 1MB))
Write-Host ("==> 완료: dist\{0} ({1:N1} MB)" -f $VersionedZipName, ($zipSize / 1MB))

if (-not $Installer) {
    Write-Host "설치 파일도 만들려면: .\build.ps1 -Installer"
    exit 0
}

$iscc = Find-Iscc
if (-not $iscc) {
    throw "Inno Setup(ISCC.exe)을 찾을 수 없습니다. winget install JRSoftware.InnoSetup 후 다시 실행하세요."
}

# 설치 파일이 개발용 config.json을 덮어쓰지 않도록 제거 (Inno가 example로 onlyifdoesntexist 생성)
if (Test-Path $distConfig) {
    Remove-Item -Force $distConfig
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
