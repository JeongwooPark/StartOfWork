param(
    [string]$ExePath = (Join-Path $PSScriptRoot "StartOfWork.exe"),
    [string]$AppName = "StartOfWork",
    [switch]$Remove
)

# 포터블(exe 직접 사용) 시 시작프로그램 수동 등록/해제용
# 설치본은 StartOfWorkSetup.exe 설치 시 자동 등록됩니다.

$ErrorActionPreference = "Stop"

$runKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"

try {
    if ($Remove) {
        if (Get-ItemProperty -Path $runKey -Name $AppName -ErrorAction SilentlyContinue) {
            Remove-ItemProperty -Path $runKey -Name $AppName -Force
            Write-Host "시작프로그램 등록 해제 완료: $AppName"
        }
        else {
            Write-Host "등록된 시작프로그램이 없습니다: $AppName"
        }
        exit 0
    }

    if (-not (Test-Path $ExePath -PathType Leaf)) {
        throw "실행 파일을 찾을 수 없습니다: $ExePath"
    }

    $resolvedExePath = (Resolve-Path $ExePath).Path
    Set-ItemProperty -Path $runKey -Name $AppName -Value "`"$resolvedExePath`""
    Write-Host "시작프로그램 등록 완료"
    Write-Host "실행 파일: $resolvedExePath"
    Write-Host "다음 Windows 로그인부터 자동 실행됩니다"
}
catch {
    Write-Error $_.Exception.Message
    exit 1
}
