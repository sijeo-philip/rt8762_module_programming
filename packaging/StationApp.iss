; Inno Setup script for the Station App installer.
; Built by packaging\build_installer.ps1, which first creates:
;   dist\StationApp\             PyInstaller output (StationApp.exe + _internal)
;   build\fresh-db\station.db    empty database at the current schema
;   build\docs-html\             HTML copies of the guides
;   packaging\redist\            VC_redist.x86.exe / VC_redist.x64.exe
; Compile by hand:  ISCC.exe /DAppVersion=0.2.6 packaging\StationApp.iss

#ifndef AppVersion
  #define AppVersion "0.2.6"
#endif
#define AppName "Station App"
#define AppExe "StationApp.exe"
#define Root ".."
#define MpCliDir "MPCliTool_v1.0.4.25_Windows"
#define MpCliExe "MPCliTool_v1.0.4.25_Windows\mpcli_v1.0.4.25_Windows\mpcli.exe"

[Setup]
; Keep AppId unchanged across versions so a new installer upgrades in place.
AppId={{007F7B2D-7B21-433A-AA20-C212DBF33FE3}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=Unisem India
VersionInfoVersion={#AppVersion}
; Not under Program Files: the app writes its database, logs and .env in
; its own folder, which standard users cannot do in Program Files.
DefaultDirName={sd}\StationApp
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
UsePreviousAppDir=yes
PrivilegesRequired=admin
; /CURRENTUSER on the command line: install without admin rights, for a
; quick test only (shortcuts for this user; VC++ runtimes must be present).
PrivilegesRequiredOverridesAllowed=commandline
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
; Same name as packaging\launcher.py: setup asks to close a running app.
AppMutex=UnisemStationAppRunning
CloseApplications=yes
OutputDir={#Root}\dist\installer
OutputBaseFilename=StationApp-Setup-{#AppVersion}
SetupIconFile=stationapp.ico
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName} {#AppVersion}
InfoAfterFile=INSTALL_NOTES.txt
WizardStyle=modern
Compression=lzma2/max
SolidCompression=yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Dirs]
; Writable by the operator's (non-admin) Windows account; kept on uninstall.
Name: "{app}\data"; Permissions: users-modify; Flags: uninsneveruninstall
Name: "{app}\logs"; Permissions: users-modify; Flags: uninsneveruninstall
Name: "{app}\config"; Permissions: users-modify; Flags: uninsneveruninstall
Name: "{app}\config\certificates"; Permissions: users-modify; Flags: uninsneveruninstall

[InstallDelete]
; Replace the program files completely on upgrade (no stale libraries).
Type: filesandordirs; Name: "{app}\_internal"

[Files]
; Application (Python runtime and all libraries are inside _internal).
Source: "{#Root}\dist\StationApp\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

; Realtek MPCLI programming tool and firmware images.
Source: "{#Root}\{#MpCliDir}\*"; DestDir: "{app}\{#MpCliDir}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#Root}\Firmware\*.bin"; DestDir: "{app}\Firmware"; Flags: ignoreversion

; Station data: never overwritten by an upgrade, never removed by uninstall.
Source: "{#Root}\build\fresh-db\station.db"; DestDir: "{app}\data"; Flags: onlyifdoesntexist uninsneveruninstall
Source: "{#Root}\config\certificates\golden_rig_ca.pem"; DestDir: "{app}\config\certificates"; Flags: onlyifdoesntexist uninsneveruninstall

; Configuration reference and documentation.
Source: "{#Root}\.env.example"; DestDir: "{app}"; Flags: ignoreversion
Source: "INSTALL_NOTES.txt"; DestDir: "{app}"; DestName: "READ ME FIRST.txt"; Flags: ignoreversion
Source: "{#Root}\README.md"; DestDir: "{app}\docs"; Flags: ignoreversion
Source: "{#Root}\docs\*.md"; DestDir: "{app}\docs"; Flags: ignoreversion
Source: "{#Root}\build\docs-html\*.html"; DestDir: "{app}\docs"; Flags: ignoreversion

; Used by [Code] only.
Source: "station.env.template"; Flags: dontcopy

; Microsoft Visual C++ 2015-2022 runtimes (mpcli.exe is a 32-bit program).
Source: "redist\VC_redist.x86.exe"; DestDir: "{tmp}"; Flags: deleteafterinstall
Source: "redist\VC_redist.x64.exe"; DestDir: "{tmp}"; Flags: deleteafterinstall

[Icons]
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; WorkingDir: "{app}"; Comment: "BLE module programming station"
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"; WorkingDir: "{app}"
Name: "{group}\Read me first"; Filename: "{app}\READ ME FIRST.txt"
Name: "{group}\Operator and Supervisor Guide"; Filename: "{app}\docs\operator_supervisor_guide.html"
Name: "{group}\Site Test Plan"; Filename: "{app}\docs\site_test_plan.html"
Name: "{group}\Golden Rig certificates (guide)"; Filename: "{app}\docs\golden_rig_certificates.html"
Name: "{group}\Golden Rig certificate tool"; Filename: "{app}\GoldenRigCertTool.exe"; WorkingDir: "{app}"; Comment: "Create the CA, rig certificate, key and fingerprint"
Name: "{group}\Documentation folder"; Filename: "{app}\docs"
Name: "{group}\Edit configuration (.env)"; Filename: "{sys}\notepad.exe"; Parameters: """{app}\.env"""
Name: "{group}\Logs folder"; Filename: "{app}\logs"
Name: "{group}\Install folder"; Filename: "{app}"
Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}"

[Run]
Filename: "{tmp}\VC_redist.x86.exe"; Parameters: "/install /quiet /norestart"; StatusMsg: "Installing Microsoft Visual C++ runtime (x86) for MPCLI..."; Check: VCRedistNeeded('x86'); Flags: waituntilterminated
Filename: "{tmp}\VC_redist.x64.exe"; Parameters: "/install /quiet /norestart"; StatusMsg: "Installing Microsoft Visual C++ runtime (x64)..."; Check: VCRedistNeeded('x64'); Flags: waituntilterminated
Filename: "{app}\{#AppExe}"; Description: "Start {#AppName} now"; WorkingDir: "{app}"; Flags: nowait postinstall skipifsilent unchecked

[Code]
var
  IdentityPage: TInputQueryWizardPage;
  JigPage: TInputOptionWizardPage;
  ModePage: TInputOptionWizardPage;

const
  MODE_PRODUCTION = 0;
  MODE_HARDWARE_TEST = 1;
  MODE_TRAINING = 2;

function EnvFileIn(Dir: String): String;
begin
  Result := AddBackslash(Dir) + '.env';
end;

function ConfigExists: Boolean;
begin
  Result := FileExists(EnvFileIn(WizardDirValue));
end;

{ VC++ 2015-2022 runtime: install unless 14.30 (VS 2022) or newer is present. }
function VCRedistNeeded(Arch: String): Boolean;
var
  Key: String;
  Installed, Major, Minor: Cardinal;
begin
  Key := 'SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\' + Arch;
  Result := True;
  if RegQueryDWordValue(HKLM32, Key, 'Installed', Installed) and (Installed = 1) and
     RegQueryDWordValue(HKLM32, Key, 'Major', Major) and
     RegQueryDWordValue(HKLM32, Key, 'Minor', Minor) then
    Result := (Major < 14) or ((Major = 14) and (Minor < 30));
end;

procedure InitializeWizard;
begin
  IdentityPage := CreateInputQueryPage(wpSelectDir,
    'Station identity',
    'Identify this station and its UnisemBLE box connection.',
    'STATION_ID and JIG_ID must be unique on the line and stay the same for this PC. ' +
    'The box URL and station key come from the box console (Stations page). ' +
    'They may stay blank for Hardware test or Training mode.');
  IdentityPage.Add('Station ID (STATION_ID):', False);
  IdentityPage.Add('Jig ID (JIG_ID):', False);
  IdentityPage.Add('Box URL (BOX_URL), e.g. http://192.168.0.64:3001:', False);
  IdentityPage.Add('Box station key (BOX_STATION_KEY):', False);
  { Defaults; can be preset on the command line, e.g. for a silent install:
    StationApp-Setup.exe /SILENT /STATIONID=STN-02 /JIGID=JIG-02 /POSITIONS=8
      /MODE=production|hardware-test|training /BOXURL=http://host:3001 /BOXKEY=stn_... }
  IdentityPage.Values[0] := ExpandConstant('{param:STATIONID|STN-01}');
  IdentityPage.Values[1] := ExpandConstant('{param:JIGID|JIG-01}');
  IdentityPage.Values[2] := ExpandConstant('{param:BOXURL|}');
  IdentityPage.Values[3] := ExpandConstant('{param:BOXKEY|}');

  JigPage := CreateInputOptionPage(IdentityPage.ID,
    'Jig', 'Number of module positions on this jig (JIG_POSITIONS).',
    'Select the jig connected to this PC:', True, False);
  JigPage.Add('8 positions');
  JigPage.Add('4 positions');
  if ExpandConstant('{param:POSITIONS|8}') = '4' then
    JigPage.SelectedValueIndex := 1
  else
    JigPage.SelectedValueIndex := 0;

  ModePage := CreateInputOptionPage(JigPage.ID,
    'Station mode', 'How will this PC be used?',
    'Production records are traceability evidence. Hardware test and Training use a ' +
    'built-in demo box and must never be used for real production.',
    True, False);
  ModePage.Add('Production - real jig, logins and MAC blocks from the box');
  ModePage.Add('Hardware test - real jig, built-in demo box (TESTING ONLY)');
  ModePage.Add('Training - simulated jig and demo box, separate training database');
  case Lowercase(ExpandConstant('{param:MODE|production}')) of
    'hardware-test': ModePage.SelectedValueIndex := MODE_HARDWARE_TEST;
    'training': ModePage.SelectedValueIndex := MODE_TRAINING;
  else
    ModePage.SelectedValueIndex := MODE_PRODUCTION;
  end;
end;

function ShouldSkipPage(PageID: Integer): Boolean;
begin
  { An existing .env (upgrade / reinstall) is kept, so its questions are skipped. }
  Result := ((PageID = IdentityPage.ID) or (PageID = JigPage.ID) or (PageID = ModePage.ID))
            and ConfigExists;
end;

function IsValidId(Value: String): Boolean;
var
  I: Integer;
  C: Char;
begin
  Result := Length(Value) > 0;
  for I := 1 to Length(Value) do
  begin
    C := Value[I];
    if not (((C >= 'A') and (C <= 'Z')) or ((C >= 'a') and (C <= 'z')) or
            ((C >= '0') and (C <= '9')) or (C = '-') or (C = '_') or (C = '.')) then
      Result := False;
  end;
end;

function HasNoSpaces(Value: String): Boolean;
begin
  Result := (Pos(' ', Value) = 0) and (Pos('#', Value) = 0) and (Pos('"', Value) = 0);
end;

function NextButtonClick(CurPageID: Integer): Boolean;
var
  Url, Key: String;
begin
  Result := True;
  if CurPageID = IdentityPage.ID then
  begin
    IdentityPage.Values[0] := Trim(IdentityPage.Values[0]);
    IdentityPage.Values[1] := Trim(IdentityPage.Values[1]);
    IdentityPage.Values[2] := Trim(IdentityPage.Values[2]);
    IdentityPage.Values[3] := Trim(IdentityPage.Values[3]);
    Url := IdentityPage.Values[2];
    Key := IdentityPage.Values[3];
    if not IsValidId(IdentityPage.Values[0]) or not IsValidId(IdentityPage.Values[1]) then
    begin
      MsgBox('Station ID and Jig ID are required and may contain only letters, digits, - _ and .', mbError, MB_OK);
      Result := False;
    end
    else if (Url <> '') and (Pos('http://', Lowercase(Url)) <> 1) and (Pos('https://', Lowercase(Url)) <> 1) then
    begin
      MsgBox('Box URL must start with http:// or https://, e.g. http://192.168.0.64:3001', mbError, MB_OK);
      Result := False;
    end
    else if not HasNoSpaces(Url) or not HasNoSpaces(Key) then
    begin
      MsgBox('Box URL and station key must not contain spaces, # or quotes.', mbError, MB_OK);
      Result := False;
    end;
  end
  else if CurPageID = ModePage.ID then
  begin
    if (ModePage.SelectedValueIndex = MODE_PRODUCTION) and
       ((IdentityPage.Values[2] = '') or (IdentityPage.Values[3] = '')) then
    begin
      MsgBox('Production mode needs the Box URL and the box station key.' + #13#10 +
             'Go Back and enter them, or choose Hardware test or Training.', mbError, MB_OK);
      Result := False;
    end;
  end;
end;

procedure WriteStationConfig;
var
  Template: AnsiString;
  Text, AuthMode, Simulation, DatabaseUrl, Positions, Target: String;
  ResultCode: Integer;
begin
  Target := ExpandConstant('{app}\.env');
  if FileExists(Target) then
  begin
    Log('Keeping existing configuration ' + Target);
    Exit;
  end;

  ExtractTemporaryFile('station.env.template');
  if not LoadStringFromFile(ExpandConstant('{tmp}\station.env.template'), Template) then
    RaiseException('Could not read the configuration template.');
  Text := String(Template);

  AuthMode := 'server';
  Simulation := 'false';
  DatabaseUrl := 'sqlite:///./data/station.db';
  case ModePage.SelectedValueIndex of
    MODE_HARDWARE_TEST: AuthMode := 'test';
    MODE_TRAINING:
      begin
        AuthMode := 'test';
        Simulation := 'true';
        DatabaseUrl := 'sqlite:///./data/training.db';
      end;
  end;
  if JigPage.SelectedValueIndex = 1 then Positions := '4' else Positions := '8';

  StringChangeEx(Text, '{{STATION_ID}}', IdentityPage.Values[0], True);
  StringChangeEx(Text, '{{JIG_ID}}', IdentityPage.Values[1], True);
  StringChangeEx(Text, '{{JIG_POSITIONS}}', Positions, True);
  StringChangeEx(Text, '{{BOX_URL}}', IdentityPage.Values[2], True);
  StringChangeEx(Text, '{{BOX_STATION_KEY}}', IdentityPage.Values[3], True);
  StringChangeEx(Text, '{{AUTH_MODE}}', AuthMode, True);
  StringChangeEx(Text, '{{SIMULATION_MODE}}', Simulation, True);
  StringChangeEx(Text, '{{DATABASE_URL}}', DatabaseUrl, True);
  StringChangeEx(Text, '{{MPCLI_PATH}}', ExpandConstant('{app}\{#MpCliExe}'), True);

  if not SaveStringToFile(Target, AnsiString(Text), False) then
    RaiseException('Could not write ' + Target);
  Log('Wrote configuration ' + Target);

  { Let operators' (standard) accounts edit .env: S-1-5-32-545 = Users. }
  Exec(ExpandConstant('{sys}\icacls.exe'), '"' + Target + '" /grant *S-1-5-32-545:M',
       '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
    WriteStationConfig;
end;

function UpdateReadyMemo(Space, NewLine, MemoUserInfoInfo, MemoDirInfo, MemoTypeInfo,
  MemoComponentsInfo, MemoGroupInfo, MemoTasksInfo: String): String;
var
  Mode, Positions: String;
begin
  if JigPage.SelectedValueIndex = 1 then Positions := '4' else Positions := '8';
  Result := MemoDirInfo + NewLine + NewLine;
  if ConfigExists then
    Result := Result + 'Configuration:' + NewLine + Space +
              'Existing .env, data and logs are kept (upgrade).' + NewLine
  else
  begin
    case ModePage.SelectedValueIndex of
      MODE_PRODUCTION: Mode := 'Production';
      MODE_HARDWARE_TEST: Mode := 'Hardware test (demo box)';
    else
      Mode := 'Training (simulation)';
    end;
    Result := Result + 'Station configuration (.env):' + NewLine +
      Space + 'Station ID: ' + IdentityPage.Values[0] + NewLine +
      Space + 'Jig ID: ' + IdentityPage.Values[1] + NewLine +
      Space + 'Jig positions: ' + Positions + NewLine +
      Space + 'Mode: ' + Mode + NewLine +
      Space + 'Box URL: ' + IdentityPage.Values[2] + NewLine;
  end;
  Result := Result + NewLine + 'Shortcuts:' + NewLine + Space +
            'Desktop and Start menu -> {#AppName}' + NewLine;
end;
