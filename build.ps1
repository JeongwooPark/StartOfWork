# StartOfWork 빌드 스크립트
# 사용법:
#   .\build.ps1              # onedir 폴더만 빌드 (+ 코드 서명)
#   .\build.ps1 -Installer   # onedir + Windows 설치 파일(+ 코드 서명)
#   .\build.ps1 -SkipSign    # 서명 생략

param(
    [switch]$Installer,
    [switch]$SkipSign
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$AppVersion = "1.2.10"
$AppDirName = "StartOfWork"
$VersionedZipName = "StartOfWork-$AppVersion.zip"
$VersionedSetupName = "StartOfWorkSetup-$AppVersion.exe"
$DistAppDir = Join-Path ".\dist" $AppDirName
$CertDir = Join-Path $PSScriptRoot "certs"
$PfxPath = Join-Path $CertDir "StartOfWorkCodeSign.pfx"
$PwdPath = Join-Path $CertDir "pfx.password"
$TimestampUrl = "http://timestamp.digicert.com"

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

function Find-SignTool {
    $cmd = Get-Command signtool -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }

    $kits = Get-ChildItem "${env:ProgramFiles(x86)}\Windows Kits\10\bin" -Directory -ErrorAction SilentlyContinue |
        Sort-Object Name -Descending
    foreach ($kit in $kits) {
        $candidate = Join-Path $kit.FullName "x64\signtool.exe"
        if (Test-Path $candidate) { return $candidate }
    }
    $fallback = "${env:ProgramFiles(x86)}\Windows Kits\10\App Certification Kit\signtool.exe"
    if (Test-Path $fallback) { return $fallback }
    return $null
}

function Ensure-CodeSigningCert {
    if ((Test-Path $PfxPath) -and (Test-Path $PwdPath)) {
        return
    }
    Write-Host "==> 코드 서명 인증서 없음 — 생성"
    & (Join-Path $PSScriptRoot "scripts\New-CodeSigningCert.ps1")
    if (-not ((Test-Path $PfxPath) -and (Test-Path $PwdPath))) {
        throw "코드 서명 인증서 생성 실패"
    }
}

function Sign-File(
    [string]$Path,
    [string]$SignTool,
    [string]$Pfx,
    [string]$Password,
    [switch]$Required
) {
    if (-not (Test-Path $Path)) {
        return $false
    }
    $args = @(
        "sign",
        "/f", $Pfx,
        "/p", $Password,
        "/fd", "SHA256",
        "/td", "SHA256",
        "/tr", $TimestampUrl,
        "/v",
        $Path
    )
    & $SignTool @args | Out-Null
    if ($LASTEXITCODE -eq 0) {
        return $true
    }
    # 타임스탬프 서버 실패 시 타임스탬프 없이 재시도
    Write-Host "  타임스탬프 실패 또는 서명 오류 — 재시도: $Path"
    $argsNoTs = @(
        "sign",
        "/f", $Pfx,
        "/p", $Password,
        "/fd", "SHA256",
        "/v",
        $Path
    )
    & $SignTool @argsNoTs | Out-Null
    if ($LASTEXITCODE -eq 0) {
        return $true
    }
    if ($Required) {
        throw "서명 실패(필수): $Path"
    }
    Write-Host "  경고: 서명 생략(지원되지 않는 바이너리일 수 있음): $Path"
    return $false
}

function Sign-AppDirectory([string]$AppDir) {
    Ensure-CodeSigningCert
    $signTool = Find-SignTool
    if (-not $signTool) {
        throw "signtool.exe를 찾을 수 없습니다. Windows SDK를 설치하세요."
    }
    $password = (Get-Content -Path $PwdPath -Raw).Trim()
    Write-Host "==> 코드 서명: $AppDir ($signTool)"

    $mainExe = Join-Path $AppDir "StartOfWork.exe"
    Sign-File -Path $mainExe -SignTool $signTool -Pfx $PfxPath -Password $password -Required | Out-Null

    $targets = Get-ChildItem -Path $AppDir -Recurse -Include *.dll,*.pyd,*.exe -File -ErrorAction SilentlyContinue |
        Where-Object { $_.FullName -ne (Resolve-Path $mainExe).Path } |
        Sort-Object -Property FullName -Unique

    $ok = 1
    $skip = 0
    foreach ($file in $targets) {
        # tcl/tk 일부 DLL은 PE 형식이 아니거나 서명이 거부됨(0x800700C1)
        if ($file.Name -match '^(tcl|tk)\d') {
            Write-Host "  건너뜀(Tcl/Tk): $($file.Name)"
            $skip++
            continue
        }
        if (Sign-File -Path $file.FullName -SignTool $signTool -Pfx $PfxPath -Password $password) {
            $ok++
        } else {
            $skip++
        }
    }
    Write-Host "==> 서명 완료: 성공 $ok / 생략 $skip"
}

function Sign-SingleFile([string]$Path) {
    Ensure-CodeSigningCert
    $signTool = Find-SignTool
    if (-not $signTool) {
        throw "signtool.exe를 찾을 수 없습니다. Windows SDK를 설치하세요."
    }
    $password = (Get-Content -Path $PwdPath -Raw).Trim()
    Write-Host "==> 코드 서명: $Path"
    Sign-File -Path $Path -SignTool $signTool -Pfx $PfxPath -Password $password -Required | Out-Null
}

Write-Host "==> 이전 빌드 산출물 정리 (v$AppVersion)"
Remove-Item -Recurse -Force .\build -ErrorAction SilentlyContinue
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

Copy-Item -Force .\StartOfWork.ico (Join-Path $DistAppDir "StartOfWork.ico") -ErrorAction SilentlyContinue

$distConfig = Join-Path $DistAppDir "config.json"
if (-not (Test-Path $distConfig)) {
    Copy-Item .\config.example.json $distConfig
    Write-Host "==> dist\StartOfWork\config.json 생성 (예시). 아이디/비밀번호를 수정하세요."
}

if (-not $SkipSign) {
    Sign-AppDirectory -AppDir $DistAppDir
} else {
    Write-Host "==> 서명 생략 (-SkipSign)"
}

# 포터블 zip (서명 후)
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

if (Test-Path $distConfig) {
    Remove-Item -Force $distConfig
}
# 설치본에 섞이면 사용자 설정을 덮어쓸 수 있는 파일 제거
@(
    "check_in_state.json",
    "holiday_cache.json",
    "lock_state_monitor.log",
    "config.json"
) | ForEach-Object {
    $p = Join-Path $DistAppDir $_
    if (Test-Path $p) {
        Remove-Item -Force $p
        Write-Host "==> 설치 패키지에서 제외: $_"
    }
}
$chromeProfile = Join-Path $DistAppDir "chrome_profile"
if (Test-Path $chromeProfile) {
    Remove-Item -Recurse -Force $chromeProfile
    Write-Host "==> 설치 패키지에서 제외: chrome_profile"
}

Write-Host "==> Inno Setup 설치 파일 생성 ($VersionedSetupName)"
# SignTool은 빌드 후 Setup exe에 적용 (ISCC SignTool 매크로보다 단순·확실)
& $iscc .\installer.iss
if ($LASTEXITCODE -ne 0) {
    throw "Inno Setup 빌드 실패 (exit $LASTEXITCODE)"
}

if (-not (Test-Path .\dist\$VersionedSetupName)) {
    throw "설치 파일 생성 실패: dist\$VersionedSetupName 없음"
}

if (-not $SkipSign) {
    Sign-SingleFile -Path (Join-Path ".\dist" $VersionedSetupName)
}

$setupSize = (Get-Item .\dist\$VersionedSetupName).Length
Write-Host ("==> 완료: dist\{0} ({1:N1} MB)" -f $VersionedSetupName, ($setupSize / 1MB))
Write-Host "설치 시 시작프로그램 등록 + README.md 포함. 계정 입력은 첫 실행 GUI에서 처리합니다."
