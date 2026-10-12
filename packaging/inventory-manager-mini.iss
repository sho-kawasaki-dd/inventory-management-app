; Inventory Manager mini インストーラー(Inno Setup 6)
; 必須の定義: /DAppVersion=<版数> /DOutputName=<出力ファイル名(拡張子なし)> /DBundleDir=<PyInstaller の onedir>
; 出力先は /O で指定する。ビルド手順は packaging/build.ps1 を参照する。

#ifndef AppVersion
  #error "/DAppVersion=<版数> を指定してください。"
#endif
#ifndef OutputName
  #error "/DOutputName=<出力ファイル名> を指定してください。"
#endif
#ifndef BundleDir
  #error "/DBundleDir=<onedir のパス> を指定してください。"
#endif

#define AppName "Inventory Manager mini"
#define AppExeName "inventory-manager-mini.exe"
#define IconFile SourcePath + "\app.ico"

[Setup]
; AppId は更新(上書きインストール)の同一性を決めるため変更しない
AppId={{8B4D1CC3-78CB-452A-8D8F-2D81019811FC}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=IKinoCore
AppCopyright=Copyright (c) 2026 IKinoCore
VersionInfoVersion={#AppVersion}
DefaultDirName={autopf}\{#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=admin
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
LicenseFile={#SourcePath}\DISTRIBUTION_LICENSE.txt
OutputBaseFilename={#OutputName}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\{#AppExeName}
; 起動中の検出は [Code] の Restart Manager で行う。強制終了と自動再起動はしない
CloseApplications=no
RestartApplications=no
#if FileExists(IconFile)
SetupIconFile={#IconFile}
#endif

[Languages]
Name: "japanese"; MessagesFile: "compiler:Languages\Japanese.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "{#BundleDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExeName}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExeName}"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent runasoriginaluser

; ユーザーデータとログを残すため [UninstallDelete] は記述しない

[Code]
// RM-BEGIN
const
  ERROR_SUCCESS = 0;
  ERROR_MORE_DATA = 234;
  RM_SESSION_KEY_LENGTH = 33;
  RM_GET_LIST_RETRIES = 5;
  ATTR_DIRECTORY = $10;
  ATTR_REPARSE_POINT = $400;
  CRLF = #13#10;

type
  TRmUniqueProcess = record
    dwProcessId: Cardinal;
    dwLowDateTime: Cardinal;
    dwHighDateTime: Cardinal;
  end;

  TRmProcessInfo = record
    Process: TRmUniqueProcess;
    strAppName: array [0..255] of Char;
    strServiceShortName: array [0..63] of Char;
    ApplicationType: Integer;
    AppStatus: Cardinal;
    TSSessionId: Cardinal;
    bRestartable: Integer;
  end;

function RmStartSession(var pSessionHandle: Cardinal; dwSessionFlags: Cardinal;
  strSessionKey: String): Cardinal;
  external 'RmStartSession@rstrtmgr.dll stdcall delayload';

function RmRegisterResources(dwSessionHandle: Cardinal; nFiles: Cardinal;
  var rgsFileNames: Integer; nApplications: Cardinal; rgApplications: Integer;
  nServices: Cardinal; rgsServiceNames: Integer): Cardinal;
  external 'RmRegisterResources@rstrtmgr.dll stdcall delayload';

function RmGetList(dwSessionHandle: Cardinal; var pnProcInfoNeeded: Cardinal;
  var pnProcInfo: Cardinal; var rgAffectedApps: TRmProcessInfo;
  var lpdwRebootReasons: Cardinal): Cardinal;
  external 'RmGetList@rstrtmgr.dll stdcall delayload';

function RmEndSession(dwSessionHandle: Cardinal): Cardinal;
  external 'RmEndSession@rstrtmgr.dll stdcall delayload';

// 使用中の判定対象は、アプリ本体の exe と配下の DLL・PYD に限る
procedure CollectTargetFiles(const Dir: String; Files: TStringList);
var
  FindRec: TFindRec;
  Extension: String;
begin
  if FindFirst(Dir + '\*', FindRec) then
  begin
    try
      repeat
        if (FindRec.Name <> '.') and (FindRec.Name <> '..') then
        begin
          if (FindRec.Attributes and ATTR_DIRECTORY) <> 0 then
          begin
            if (FindRec.Attributes and ATTR_REPARSE_POINT) = 0 then
              CollectTargetFiles(Dir + '\' + FindRec.Name, Files);
          end
          else
          begin
            Extension := Lowercase(ExtractFileExt(FindRec.Name));
            if (Extension = '.dll') or (Extension = '.pyd') or
               (Lowercase(FindRec.Name) = '{#AppExeName}') then
              Files.Add(Dir + '\' + FindRec.Name);
          end;
        end;
      until not FindNext(FindRec);
    finally
      FindClose(FindRec);
    end;
  end;
end;

function ProcessDisplayName(const Info: TRmProcessInfo): String;
var
  I: Integer;
begin
  Result := '';
  for I := 0 to 255 do
  begin
    if Info.strAppName[I] = #0 then
      Break;
    Result := Result + Info.strAppName[I];
  end;
  if Result = '' then
    Result := '(名称不明)';
  Result := Result + ' (PID ' + IntToStr(Info.Process.dwProcessId) + ')';
end;

// 成功時は 0 を返し、使用中のプロセスを Found(改行区切り)と Count に設定する。失敗時は 0 以外を返す
function QueryRunningProcesses(const AppDir: String; var Found: String;
  var Count: Integer): Integer;
var
  Files: TStringList;
  Names: array of String;
  Pointers: array of Integer;
  Info: array of TRmProcessInfo;
  Dummy: TRmProcessInfo;
  Session, Needed, Have, Reasons, Res: Cardinal;
  Key: String;
  I, Attempt: Integer;
  SessionStarted: Boolean;
begin
  Found := '';
  Count := 0;
  Result := 0;
  Files := TStringList.Create;
  try
    try
      CollectTargetFiles(AppDir, Files);
      if Files.Count > 0 then
      begin
        SetArrayLength(Names, Files.Count);
        SetArrayLength(Pointers, Files.Count);
        for I := 0 to Files.Count - 1 do
        begin
          Names[I] := Files[I];
          Pointers[I] := CastStringToInteger(Names[I]);
        end;

        SetLength(Key, RM_SESSION_KEY_LENGTH);
        Session := 0;
        SessionStarted := False;
        Res := RmStartSession(Session, 0, Key);
        if Res <> ERROR_SUCCESS then
          Result := Res
        else
          SessionStarted := True;

        if SessionStarted then
        begin
          try
            Res := RmRegisterResources(Session, Files.Count, Pointers[0], 0, 0, 0, 0);
            if Res <> ERROR_SUCCESS then
              Result := Res
            else
            begin
              Needed := 0;
              Have := 0;
              Reasons := 0;
              Res := RmGetList(Session, Needed, Have, Dummy, Reasons);
              Attempt := 0;
              while (Res = ERROR_MORE_DATA) and (Attempt < RM_GET_LIST_RETRIES) do
              begin
                Attempt := Attempt + 1;
                // 取得の間に増えた分の余裕を持たせる
                Have := Needed + 8;
                SetArrayLength(Info, Have);
                Res := RmGetList(Session, Needed, Have, Info[0], Reasons);
              end;
              if Res <> ERROR_SUCCESS then
                Result := Res
              else
              begin
                Count := Have;
                for I := 0 to Count - 1 do
                begin
                  if Found <> '' then
                    Found := Found + CRLF;
                  Found := Found + ProcessDisplayName(Info[I]);
                end;
              end;
            end;
          finally
            RmEndSession(Session);
          end;
        end;
      end;
    except
      Log('Restart Manager の呼び出しで例外が発生しました: ' + GetExceptionMessage);
      Result := -1;
    end;
  finally
    Files.Free;
  end;
end;

// 実行中・確認失敗では処理を開始させない。対話時は終了後の再試行を許可し、サイレント時は即座に失敗とする
function EnsureAppNotRunning(const AppDir, ActionName: String; Silent: Boolean;
  var FailMessage: String): Boolean;
var
  Found, Msg: String;
  Count, Err: Integer;
begin
  Result := False;
  FailMessage := '';
  while True do
  begin
    Err := QueryRunningProcesses(AppDir, Found, Count);
    if (Err = 0) and (Count = 0) then
    begin
      Result := True;
      Exit;
    end;

    if Err <> 0 then
      Msg := Format('{#AppName} が使用中か確認できませんでした(エラー %d)。' + CRLF +
        '安全のため%sを開始しません。', [Err, ActionName])
    else
      Msg := Format('{#AppName} が起動中です。次のプログラムを終了してから[再試行]を押してください。' +
        CRLF + CRLF + '%s', [Found]);
    Log(Msg);

    if Silent then
    begin
      FailMessage := Msg;
      Exit;
    end;
    if MsgBox(Msg, mbError, MB_RETRYCANCEL) = IDCANCEL then
    begin
      FailMessage := ActionName + 'を中止しました。';
      Exit;
    end;
  end;
end;
// RM-END

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  FailMessage: String;
begin
  Result := '';
  if not EnsureAppNotRunning(ExpandConstant('{app}'), '更新', WizardSilent, FailMessage) then
    Result := FailMessage;
end;

function InitializeUninstall(): Boolean;
var
  FailMessage: String;
begin
  Result := EnsureAppNotRunning(ExpandConstant('{app}'), 'アンインストール',
    UninstallSilent, FailMessage);
end;
