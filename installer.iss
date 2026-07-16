; StartOfWork Windows 설치 스크립트 (Inno Setup 6+)
; 빌드: iscc installer.iss
; 또는: .\build.ps1 -Installer

#define MyAppName "StartOfWork"
#define MyAppVersion "1.1.3"
#define MyAppPublisher "StartOfWork"
#define MyAppExeName "StartOfWork.exe"

[Setup]
AppId={{A7E3C91B-4D2F-4B8A-9C1E-8F2A6B0D4E71}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={localappdata}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=dist
OutputBaseFilename=StartOfWorkSetup-1.1.3
SetupIconFile=StartOfWork.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=yes

[Languages]
Name: "korean"; MessagesFile: "compiler:Languages\Korean.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "바탕화면 바로가기 만들기"; GroupDescription: "추가 아이콘:"; Flags: unchecked

[Files]
Source: "dist\{#MyAppExeName}"; DestDir: "{app}"; Flags: ignoreversion
Source: "dist\StartOfWork.ico"; DestDir: "{app}"; Flags: ignoreversion skipifsourcedoesntexist
Source: "README.md"; DestDir: "{app}"; Flags: ignoreversion isreadme
; 최초 설치에만 빈 계정 config.json 생성 (기존 설정 덮어쓰지 않음)
; 아이디/비밀번호는 첫 실행 시 프로그램 GUI에서 입력
Source: "config.example.json"; DestDir: "{app}"; DestName: "config.json"; Flags: onlyifdoesntexist
Source: "config.example.json"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"
Name: "{group}\사용 설명서 (README)"; Filename: "{app}\README.md"
Name: "{group}\{#MyAppName} 제거"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"; Tasks: desktopicon

[Registry]
; 시작프로그램 항상 등록 (HKCU Run) — 제거 시 함께 삭제
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; \
    ValueType: string; ValueName: "{#MyAppName}"; ValueData: """{app}\{#MyAppExeName}"""; \
    Flags: uninsdeletevalue

; [Run] 설치 직후 자동 실행은 넣지 않음.
; 서명 없는 exe를 Setup이 CreateProcess로 띄우면 Smart App Control이
; 코드 4551로 차단하는 경우가 있음. 사용자는 설치 폴더/시작 메뉴에서 직접 실행.

[UninstallDelete]
Type: files; Name: "{app}\lock_state_monitor.log"

[Code]
procedure CurPageChanged(CurPageID: Integer);
var
  I: Integer;
begin
  { 완료 화면의 README 열기 체크박스를 기본 해제 }
  if CurPageID = wpFinished then
  begin
    for I := 0 to WizardForm.RunList.Items.Count - 1 do
      WizardForm.RunList.Checked[I] := False;
  end;
end;
