# 코드 서명 인증서

이 폴더의 **PFX/비밀번호는 git에 올리지 않습니다.**

| 파일 | 설명 | Git |
|------|------|-----|
| `StartOfWorkCodeSign.pfx` | 개인키 포함 인증서 | ignore |
| `pfx.password` | PFX 암호 | ignore |
| `StartOfWorkCodeSign.cer` | 공개 인증서 (신뢰 설치용) | 커밋 가능 |

## 생성

```powershell
.\scripts\New-CodeSigningCert.ps1
```

`.\build.ps1` 실행 시 PFX가 없으면 위 스크립트를 자동 호출합니다.

## 다른 PC에서 신뢰

관리자 PowerShell:

```powershell
Import-Certificate -FilePath .\certs\StartOfWorkCodeSign.cer -CertStoreLocation Cert:\LocalMachine\TrustedPublisher
Import-Certificate -FilePath .\certs\StartOfWorkCodeSign.cer -CertStoreLocation Cert:\LocalMachine\Root
```

## 한계

자체 서명은 **상용 CA 코드 서명 인증서**가 아닙니다.  
Smart App Control이 “강제” 모드이면 여전히 차단될 수 있습니다. 로컬 Trusted Publisher/Root에 CER을 설치한 환경에서는 경고·차단이 줄어듭니다.
