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

[Messages]
ConfirmUninstall=Удалить %1?%n%nHachimi и русский перевод тоже будут убраны из папки игры, игра вернётся к исходному виду. Сама игра и ваш прогресс в ней не пострадают.

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
; Настройки, журнал и кэш скачанного перевода (сотни МБ) из umaru_core.app_home().
Type: filesandordirs; Name: "{localappdata}\UmaRU"

[Code]
const
  { То же имя, что INSTANCE_MUTEX в player/umaru_app.py: мьютекс держит открытое окно Uma RU Player. }
  PlayerMutex = 'UmaRUPlayerSingleInstance';

var
  VariantPage: TInputOptionWizardPage;
  PreviousChannel: Boolean;

function InitializeUninstall: Boolean;
var
  I: Integer;
begin
  Result := True;
  { После «Удалить всё» окно закрывается само, но может не успеть: даём ему три секунды. }
  for I := 1 to 15 do
  begin
    if not CheckForMutexes(PlayerMutex) then Exit;
    Sleep(200);
  end;
  { Открытое окно держит UmaRU.exe и свои библиотеки: удалить их не получится. }
  while CheckForMutexes(PlayerMutex) do
  begin
    if UninstallSilent or (MsgBox('Закройте окно Uma RU Player и нажмите «ОК».', mbError, MB_OKCANCEL) <> IDOK) then
    begin
      Result := False;
      Exit;
    end;
  end;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  Player: String;
  ResultCode: Integer;
begin
  if CurUninstallStep <> usUninstall then Exit;
  Player := ExpandConstant('{app}\UmaRU.exe');
  if not FileExists(Player) then Exit;
  { Hachimi и перевод лежат в папке игры, а не в папке программы: убираем их, пока программа ещё на месте.
    --keep-program: саму программу удалит этот деинсталлятор. }
  if Exec(Player, '--uninstall-all --keep-program', ExpandConstant('{app}'), SW_HIDE, ewWaitUntilTerminated, ResultCode)
     and (ResultCode = 0) then
    Exit;
  if UninstallSilent then Exit;
  if MsgBox('Не удалось убрать Hachimi и перевод из папки игры. Обычно так бывает, если игра запущена.' + #13#10#13#10 +
            '«Да» — всё равно удалить программу (Hachimi и перевод останутся в игре).' + #13#10 +
            '«Нет» — отменить удаление: закройте игру и запустите удаление снова.',
            mbConfirmation, MB_YESNO) = IDNO then
    Abort;
end;

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
