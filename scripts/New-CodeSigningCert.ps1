# 자체 코드 서명 인증서 생성 (Code Signing)
# 사용법: .\scripts\New-CodeSigningCert.ps1
# 산출물:
#   certs\StartOfWorkCodeSign.pfx  (비밀 — gitignore)
#   certs\pfx.password             (비밀 — gitignore)
#   certs\StartOfWorkCodeSign.cer  (공개 — Trusted Publisher 설치용)

param(
    [string]$Subject = "CN=StartOfWork, O=StartOfWork",
    [int]$YearsValid = 5
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$CertDir = Join-Path $Root "certs"
$PfxPath = Join-Path $CertDir "StartOfWorkCodeSign.pfx"
$CerPath = Join-Path $CertDir "StartOfWorkCodeSign.cer"
$PwdPath = Join-Path $CertDir "pfx.password"

New-Item -ItemType Directory -Force -Path $CertDir | Out-Null

if ((Test-Path $PfxPath) -and (Test-Path $PwdPath)) {
    Write-Host "이미 인증서가 있습니다: $PfxPath"
    Write-Host "다시 만들려면 PFX/password를 삭제한 뒤 재실행하세요."
    exit 0
}

# 암호 생성 (로컬 파일에만 저장)
$alphabet = [char[]](65..90) + [char[]](97..122) + [char[]](48..57)
$password = -join (1..32 | ForEach-Object { $alphabet | Get-Random })
$secure = ConvertTo-SecureString -String $password -Force -AsPlainText

Write-Host "==> 코드 서명 인증서 생성 ($Subject, ${YearsValid}년)"
$cert = New-SelfSignedCertificate `
    -Type CodeSigningCert `
    -Subject $Subject `
    -FriendlyName "StartOfWork Code Signing" `
    -NotAfter (Get-Date).AddYears($YearsValid) `
    -CertStoreLocation "Cert:\CurrentUser\My" `
    -KeyExportPolicy Exportable `
    -KeySpec Signature `
    -HashAlgorithm SHA256

Export-PfxCertificate -Cert $cert -FilePath $PfxPath -Password $secure | Out-Null
Export-Certificate -Cert $cert -FilePath $CerPath | Out-Null
Set-Content -Path $PwdPath -Value $password -NoNewline -Encoding ascii

# 현재 사용자 Trusted Publisher에 등록 (로컬 신뢰)
$pubStore = New-Object System.Security.Cryptography.X509Certificates.X509Store(
    "TrustedPublisher",
    "CurrentUser"
)
$pubStore.Open("ReadWrite")
$pubStore.Add($cert)
$pubStore.Close()

Write-Host "==> 완료"
Write-Host "  PFX : $PfxPath"
Write-Host "  CER : $CerPath"
Write-Host "  PWD : $PwdPath"
Write-Host "  Thumbprint: $($cert.Thumbprint)"
Write-Host ""
Write-Host "다른 PC에서 신뢰하려면 관리자 PowerShell에서:"
Write-Host "  Import-Certificate -FilePath '$CerPath' -CertStoreLocation Cert:\LocalMachine\TrustedPublisher"
Write-Host "  Import-Certificate -FilePath '$CerPath' -CertStoreLocation Cert:\LocalMachine\Root"
Write-Host ""
Write-Host "참고: 자체 서명은 Smart App Control(평가/강제)에서 상용 CA 서명만큼 신뢰되지 않을 수 있습니다."
