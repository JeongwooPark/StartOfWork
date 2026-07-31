; StartOfWork Windows 설치 스크립트 (Inno Setup 6+)
; 빌드: iscc installer.iss
; 또는: .\build.ps1 -Installer

#define MyAppName "StartOfWork"
#define MyAppVersion "1.2.12"
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
OutputBaseFilename=StartOfWorkSetup-1.2.12
SetupIconFile=StartOfWork.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=yes
; Setup.exe 서명은 build.ps1이 빌드 후 signtool로 적용합니다.

[Languages]
Name: "korean"; MessagesFile: "compiler:Languages\Korean.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "바탕화면 바로가기 만들기"; GroupDescription: "추가 아이콘:"; Flags: unchecked

[Files]
; onedir 산출물 — 사용자 데이터(config/상태/캐시/프로필)는 절대 덮어쓰지 않음
Source: "dist\StartOfWork\*"; DestDir: "{app}"; \
    Flags: ignoreversion recursesubdirs createallsubdirs; \
    Excludes: "config.json,check_in_state.json,holiday_cache.json,lock_state_monitor.log,chrome_profile,PendingUpdate,Updater"
; 업데이터는 앱 폴더 밖 (설치 중 잠금·TEMP 복사 회피)
Source: "dist\StartOfWorkUpdater\*"; DestDir: "{localappdata}\StartOfWorkUpdater"; \
    Flags: ignoreversion recursesubdirs createallsubdirs
Source: "README.md"; DestDir: "{app}"; Flags: ignoreversion isreadme
; 최초 설치에만 빈 계정 config.json 생성 (기존 설정 덮어쓰지 않음)
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

[UninstallDelete]
Type: files; Name: "{app}\lock_state_monitor.log"
Type: filesandordirs; Name: "{localappdata}\StartOfWorkUpdater"

[Code]
var
  LaunchAfterInstallCheck: TNewCheckBox;
  UserConfigBackup: String;
  UserStateBackup: String;
  UserHolidayBackup: String;

function IsReadmeRunItem(const Caption: String): Boolean;
var
  UpperCap: String;
begin
  UpperCap := UpperCase(Caption);
  Result := (Pos('README', UpperCap) > 0) or (Pos('설명서', Caption) > 0);
end;

procedure BackupUserDataFiles;
begin
  UserConfigBackup := ExpandConstant('{tmp}\StartOfWork_config.json.bak');
  UserStateBackup := ExpandConstant('{tmp}\StartOfWork_check_in_state.json.bak');
  UserHolidayBackup := ExpandConstant('{tmp}\StartOfWork_holiday_cache.json.bak');

  if FileExists(ExpandConstant('{app}\config.json')) then
    FileCopy(ExpandConstant('{app}\config.json'), UserConfigBackup, False);
  if FileExists(ExpandConstant('{app}\check_in_state.json')) then
    FileCopy(ExpandConstant('{app}\check_in_state.json'), UserStateBackup, False);
  if FileExists(ExpandConstant('{app}\holiday_cache.json')) then
    FileCopy(ExpandConstant('{app}\holiday_cache.json'), UserHolidayBackup, False);
end;

procedure RestoreUserDataFiles;
begin
  { 업그레이드 시 설치본이 사용자 설정을 덮어쓴 경우 복원 }
  if (UserConfigBackup <> '') and FileExists(UserConfigBackup) then
    FileCopy(UserConfigBackup, ExpandConstant('{app}\config.json'), False);
  if (UserStateBackup <> '') and FileExists(UserStateBackup) then
    FileCopy(UserStateBackup, ExpandConstant('{app}\check_in_state.json'), False);
  if (UserHolidayBackup <> '') and FileExists(UserHolidayBackup) then
    FileCopy(UserHolidayBackup, ExpandConstant('{app}\holiday_cache.json'), False);
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssInstall then
    BackupUserDataFiles
  else if CurStep = ssPostInstall then
    RestoreUserDataFiles;
end;

procedure CurPageChanged(CurPageID: Integer);
var
  I: Integer;
  ExePath: String;
begin
  if CurPageID <> wpFinished then
    Exit;

  ExePath := ExpandConstant('{app}\{#MyAppExeName}');

  { README 열기 체크는 기본 해제 }
  for I := 0 to WizardForm.RunList.Items.Count - 1 do
  begin
    if IsReadmeRunItem(WizardForm.RunList.ItemCaption[I]) then
      WizardForm.RunList.Checked[I] := False;
  end;

  WizardForm.FinishedLabel.Caption :=
    '설치가 완료되었습니다.'#13#10#13#10 +
    '아래에서 「StartOfWork 실행」을 선택하면 프로그램을 바로 시작합니다.'#13#10 +
    'Windows(스마트 앱 컨트롤 등)가 자동 실행을 막으면 시작 메뉴 또는'#13#10 +
    '다음 경로에서 직접 실행하세요.'#13#10#13#10 +
    ExePath;

  if LaunchAfterInstallCheck = nil then
  begin
    LaunchAfterInstallCheck := TNewCheckBox.Create(WizardForm);
    LaunchAfterInstallCheck.Parent := WizardForm.FinishedPage;
    LaunchAfterInstallCheck.Left := WizardForm.FinishedLabel.Left;
    LaunchAfterInstallCheck.Top :=
      WizardForm.FinishedLabel.Top + WizardForm.FinishedLabel.Height + ScaleY(12);
    LaunchAfterInstallCheck.Width := WizardForm.FinishedLabel.Width;
    LaunchAfterInstallCheck.Height := ScaleY(22);
    LaunchAfterInstallCheck.Caption := 'StartOfWork 실행';
    LaunchAfterInstallCheck.Checked := True;
  end
  else
  begin
    LaunchAfterInstallCheck.Top :=
      WizardForm.FinishedLabel.Top + WizardForm.FinishedLabel.Height + ScaleY(12);
    LaunchAfterInstallCheck.Visible := True;
  end;
end;

function NextButtonClick(CurPageID: Integer): Boolean;
var
  ResultCode: Integer;
  ExePath: String;
begin
  Result := True;
  if CurPageID <> wpFinished then
    Exit;
  if (LaunchAfterInstallCheck = nil) or (not LaunchAfterInstallCheck.Checked) then
    Exit;

  ExePath := ExpandConstant('{app}\{#MyAppExeName}');
  if not Exec(ExePath, '', ExpandConstant('{app}'), SW_SHOWNORMAL, ewNoWait, ResultCode) then
  begin
    MsgBox(
      'Windows가 프로그램 자동 실행을 차단했을 수 있습니다.'#13#10#13#10 +
      '시작 메뉴의 「StartOfWork」또는 아래 경로에서 직접 실행하세요.'#13#10#13#10 +
      ExePath,
      mbInformation, MB_OK);
  end;
end;
