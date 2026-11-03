#define SetupRoot GetEnv("UMARU_SETUP_ROOT")
#define LauncherVersion GetEnv("UMARU_APP_VERSION")
#if SetupRoot == ""
  #error Set UMARU_SETUP_ROOT to the absolute setup work directory.
#endif
#if LauncherVersion == ""
  #error Set UMARU_APP_VERSION from player/_launcher_version.py.
#endif

[Setup]
AppId={{A54CA8D0-92CF-491B-BDB5-280DBA0E91D4}
AppName=Uma RU Player
AppVersion={#LauncherVersion}
AppVerName=Uma RU Player {#LauncherVersion}
VersionInfoVersion={#LauncherVersion}.0.0
AppPublisher=Uma RU
DefaultDirName={localappdata}\Programs\UmaRU
DefaultGroupName=Uma RU
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
WizardStyle=modern
ShowLanguageDialog=no
LanguageDetectionMethod=none
OutputDir={#SetupRoot}\output
OutputBaseFilename=UmaRU-Setup
SetupIconFile={#GetEnv("UMARU_ICON_FILE")}
Compression=lzma2/ultra64
SolidCompression=yes
UninstallDisplayIcon={app}\UmaRU.exe
ChangesAssociations=no
CloseApplications=yes
RestartApplications=no
DisableProgramGroupPage=yes
UsePreviousAppDir=yes
UsePreviousGroup=yes
DisableReadyMemo=no
WizardImageStretch=yes

[Languages]
Name: "russian"; MessagesFile: "compiler:Languages\Russian.isl"

[Files]
Source: "{#SetupRoot}\stage\UmaRU\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{group}\Uma RU Player"; Filename: "{app}\UmaRU.exe"; IconFilename: "{app}\UmaRU.exe"
Name: "{userdesktop}\Играть на русском (Uma RU)"; Filename: "{app}\UmaRU.exe"; Parameters: "--play"; Tasks: desktopicon; IconFilename: "{app}\UmaRU.exe"

[Tasks]
Name: "desktopicon"; Description: "Создать ярлык «Играть на русском» на рабочем столе"; GroupDescription: "Дополнительные задачи:"

[Run]
Filename: "{app}\UmaRU.exe"; Description: "Открыть Uma RU Player"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
Type: files; Name: "{app}\data\update-channel.json"

[Code]
var
  VariantPage: TInputOptionWizardPage;
  PreviousChannel: Boolean;

function InitializeSetup: Boolean;
begin
  Result := (Pos('/umaruupdate', LowerCase(GetCmdTail)) = 0) or not IsAdmin;
  if not Result then
    MsgBox('Откройте Uma RU Player без прав администратора и повторите обновление.', mbError, MB_OK);
end;

function ChannelPath: String;
begin
  Result := ExpandConstant('{app}\data\update-channel.json');
end;

function ExistingVariant: String;
var
  Raw: AnsiString;
  S: String;
begin
  Result := 'full';
  if LoadStringFromFile(ChannelPath, Raw) then
  begin
    S := String(Raw);
    StringChangeEx(S, ' ', '', True);
    StringChangeEx(S, #13, '', True);
    StringChangeEx(S, #10, '', True);
    if Pos('"variant":"lite"', S) > 0 then
      Result := 'lite';
  end;
end;

procedure InitializeWizard;
var
  Variant: String;
begin
  VariantPage := CreateInputOptionPage(wpSelectDir, 'Вариант перевода',
    'Выберите объём перевода',
    'Пакет перевода загрузится через интернет при первом запуске программы. ' +
    'При обновлении выбор сохраняется.', True, False);
  VariantPage.Add('Полный перевод');
  VariantPage.Add('Облегчённый перевод');
  Variant := 'full';
  if Pos('/variant=lite', LowerCase(GetCmdTail)) > 0 then
    Variant := 'lite';
  VariantPage.Values[0] := Variant <> 'lite';
  VariantPage.Values[1] := Variant = 'lite';
end;

procedure CurPageChanged(CurPageID: Integer);
var
  Variant: String;
begin
  if CurPageID <> VariantPage.ID then Exit;
  PreviousChannel := FileExists(ChannelPath);
  if PreviousChannel then
  begin
    Variant := ExistingVariant;
    VariantPage.Values[0] := Variant <> 'lite';
    VariantPage.Values[1] := Variant = 'lite';
    VariantPage.SubCaptionLabel.Caption := 'Уже установленный вариант перевода сохранится.';
    VariantPage.CheckListBox.Enabled := False;
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  Variant: String;
  Channel: String;
begin
  if CurStep <> ssPostInstall then Exit;
  if FileExists(ChannelPath) then Exit;
  Variant := 'full';
  if VariantPage.Values[1] then Variant := 'lite';
  Channel := '{"schema":1,"url":"https://github.com/kapitanzet3-a11y/UmaRU-Updates/releases/latest/download/channel.json","variant":"' + Variant + '"}' + #13#10;
  ForceDirectories(ExpandConstant('{app}\data'));
  if not SaveStringToFile(ChannelPath, Channel, False) then
    RaiseException('Не удалось сохранить настройки канала обновлений.');
end;
