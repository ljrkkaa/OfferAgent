#ifndef ReleaseRoot
  #error ReleaseRoot is required
#endif
#ifndef RuntimeVersion
  #error RuntimeVersion is required
#endif
#ifndef PluginVersion
  #define PluginVersion RuntimeVersion
#endif
#ifndef RuntimeArchitecture
  #error RuntimeArchitecture is required
#endif
#if RuntimeArchitecture == "x64"
  #define InstallerArchitecturesAllowed "x64os"
  #define InstallerArchitecturesInstallMode "x64os"
#elif RuntimeArchitecture == "arm64"
  #define InstallerArchitecturesAllowed "arm64"
  #define InstallerArchitecturesInstallMode "arm64"
#else
  #error RuntimeArchitecture must be x64 or arm64
#endif

[Setup]
AppId={{A8EE410F-7D39-4D88-B06B-C0C4B375A8D8}
AppName=OfferAgent for Obsidian
AppVersion={#RuntimeVersion}
ArchitecturesAllowed={#InstallerArchitecturesAllowed}
ArchitecturesInstallIn64BitMode={#InstallerArchitecturesInstallMode}
DefaultDirName={localappdata}\OfferAgent\setup-payload
PrivilegesRequired=lowest
Compression=lzma2/max
SolidCompression=yes
OutputBaseFilename=OfferAgent-for-Obsidian-Setup-{#RuntimeArchitecture}
OutputDir={#ReleaseRoot}
Uninstallable=yes

[Files]
Source: "{#ReleaseRoot}\setup-payload\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion
Source: "{#ReleaseRoot}\setup-payload\offeragent-obsidian-plugin\*"; DestDir: "{code:GetPluginDir}"; Flags: recursesubdirs createallsubdirs ignoreversion

[UninstallDelete]
; Runs only after InitializeUninstall returns True (explicit all-Vault scope).
; Known ledger metadata is removed after the coordinator ACK. Unknown siblings
; are never matched and deliberately keep the parent directories non-empty.
Type: files; Name: "{localappdata}\OfferAgent\installer\vault-installations.json"
Type: files; Name: "{localappdata}\OfferAgent\installer\selected-installation.txt"
Type: files; Name: "{localappdata}\OfferAgent\installer\uninstall-journal.json"
Type: dirifempty; Name: "{localappdata}\OfferAgent\installer\requests"
Type: dirifempty; Name: "{localappdata}\OfferAgent\installer"

[Code]
const
  InstallerRegistryKey = 'Software\OfferAgent\Installer';
  PurgeConfirmation = 'DELETE OFFERAGENT LOCAL DATA';

var
  VaultPage: TInputDirWizardPage;

function CoCreateGuid(var Guid: TGUID): HResult;
  external 'CoCreateGuid@ole32.dll stdcall';

procedure InitializeWizard;
begin
  VaultPage := CreateInputDirPage(wpSelectDir,
    '选择 Obsidian Vault',
    '选择要安装 OfferAgent 的真实 Vault 根目录',
    '安装器只把插件完整包复制到该 Vault 的 .obsidian\plugins 目录；Vault 笔记不会被修改或删除。',
    False, '');
  VaultPage.Add('');
end;

function NextButtonClick(CurPageID: Integer): Boolean;
var
  VaultRoot: String;
begin
  Result := True;
  if CurPageID = VaultPage.ID then
  begin
    VaultRoot := RemoveBackslashUnlessRoot(VaultPage.Values[0]);
    if (VaultRoot = '') or (not DirExists(VaultRoot)) or
      (not DirExists(VaultRoot + '\.obsidian')) then
    begin
      MsgBox('请选择包含 .obsidian 目录的本地 Vault。', mbError, MB_OK);
      Result := False;
    end;
  end;
end;

function GetPluginDir(Param: String): String;
begin
  Result := RemoveBackslashUnlessRoot(VaultPage.Values[0]) +
    '\.obsidian\plugins\offeragent-obsidian-plugin';
end;

function GetVaultRoot: String;
begin
  Result := RemoveBackslashUnlessRoot(VaultPage.Values[0]);
end;

function IsLowerHex(Character: Char): Boolean;
begin
  Result := ((Character >= '0') and (Character <= '9')) or
    ((Character >= 'a') and (Character <= 'f'));
end;

function IsLowerHexString(const Value: String; ExpectedLength: Integer): Boolean;
var
  Index: Integer;
begin
  Result := False;
  if Length(Value) <> ExpectedLength then
    Exit;
  for Index := 1 to ExpectedLength do
    if not IsLowerHex(Value[Index]) then
      Exit;
  Result := True;
end;

function IsOperationId(const Value: String): Boolean;
begin
  Result := IsLowerHexString(Value, 64);
end;

function IsInstallationId(const Value: String): Boolean;
begin
  Result := (Length(Value) = 40) and (Copy(Value, 1, 8) = 'install_') and
    IsLowerHexString(Copy(Value, 9, 32), 32);
end;

function NewOperationId(var OperationId: String): Boolean;
var
  Guid: TGUID;
  Material: String;
begin
  Result := False;
  OperationId := '';
  if CoCreateGuid(Guid) <> 0 then
    Exit;
  Material := Format('%x|%x|%x|%x|%x|%x|%x|%x|%x|%x|%x',
    [Guid.D1, Guid.D2, Guid.D3, Guid.D4[0], Guid.D4[1], Guid.D4[2],
     Guid.D4[3], Guid.D4[4], Guid.D4[5], Guid.D4[6], Guid.D4[7]]);
  OperationId := Lowercase(GetSHA256OfString(Utf8Encode(Material)));
  Result := IsOperationId(OperationId);
end;

function JsonEscape(const Value: String; var Escaped: String): Boolean;
var
  Index: Integer;
  Character: Char;
begin
  Escaped := '';
  Result := False;
  for Index := 1 to Length(Value) do
  begin
    Character := Value[Index];
    if Ord(Character) < 32 then
      Exit;
    if Character = '\' then
      Escaped := Escaped + '\\'
    else if Character = '"' then
      Escaped := Escaped + '\"'
    else
      Escaped := Escaped + Character;
  end;
  Result := True;
end;

function RunBootstrap(const Parameters: String): Boolean;
var
  Bootstrap: String;
  ResultCode: Integer;
begin
  Bootstrap := ExpandConstant('{app}\offeragent-bootstrap.exe');
  Result := FileExists(Bootstrap) and
    Exec(Bootstrap, Parameters, '', SW_HIDE, ewWaitUntilTerminated, ResultCode) and
    (ResultCode = 0);
end;

function RegisterVault(const VaultRoot: String; const Source: String): Boolean;
var
  RequestId: String;
  RequestPath: String;
  EscapedRoot: String;
  Payload: String;
begin
  Result := False;
  if not JsonEscape(VaultRoot, EscapedRoot) then
    Exit;
  if not NewOperationId(RequestId) then
    Exit;
  if not RunBootstrap('inno-ledger prepare-registration --request-id "' + RequestId + '"') then
    Exit;
  RequestPath := ExpandConstant('{localappdata}\OfferAgent\installer\requests\') +
    RequestId + '.json';
  Payload := '{"pluginVersion":"{#PluginVersion}","schemaVersion":1,"source":"' +
    Source + '","vaultRoot":"' + EscapedRoot + '"}' + #10;
  if not SaveStringToFile(RequestPath, Utf8Encode(Payload), False) then
    Exit;
  Result := RunBootstrap('inno-ledger register --request-id "' + RequestId + '"');
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep <> ssPostInstall then
    Exit;

  { A newly copied plugin may not have run yet.  The protected ledger therefore
    records a pending installation and first bootstrap later binds its owner. }
  if not RegisterVault(GetVaultRoot, 'setup') then
    RaiseException('OfferAgent 无法建立受保护的多 Vault 安装记录；安装已中止。');
end;

function LoadSelectedInstallation(var InstallationId: String): Boolean;
var
  SelectionPath: String;
  Payload: AnsiString;
begin
  SelectionPath := ExpandConstant(
    '{localappdata}\OfferAgent\installer\selected-installation.txt');
  Result := LoadStringFromFile(SelectionPath, Payload);
  if Result then
  begin
    InstallationId := Trim(Payload);
    Result := IsInstallationId(InstallationId);
  end;
end;

function LoadPendingOperation(var Found: Boolean; var OperationId: String;
  var Scope: String; var Mode: String; var InstallationId: String): Boolean;
begin
  Found := RegQueryStringValue(HKCU, InstallerRegistryKey,
    'PendingOperationId', OperationId);
  if not Found then
  begin
    Result := True;
    Exit;
  end;
  Result := IsOperationId(OperationId) and
    RegQueryStringValue(HKCU, InstallerRegistryKey, 'PendingScope', Scope) and
    RegQueryStringValue(HKCU, InstallerRegistryKey, 'PendingMode', Mode) and
    RegQueryStringValue(HKCU, InstallerRegistryKey,
      'PendingInstallationId', InstallationId) and
    (((Scope = 'selected') and (Mode = 'preserve-data') and
      IsInstallationId(InstallationId)) or
     ((Scope = 'all') and ((Mode = 'preserve-data') or
       (Mode = 'purge-data')) and (InstallationId = '')));
end;

function StorePendingOperation(const OperationId: String; const Scope: String;
  const Mode: String; const InstallationId: String): Boolean;
begin
  Result := RegWriteStringValue(HKCU, InstallerRegistryKey,
    'PendingOperationId', OperationId) and
    RegWriteStringValue(HKCU, InstallerRegistryKey, 'PendingScope', Scope) and
    RegWriteStringValue(HKCU, InstallerRegistryKey, 'PendingMode', Mode) and
    RegWriteStringValue(HKCU, InstallerRegistryKey,
      'PendingInstallationId', InstallationId);
end;

procedure ClearPendingOperation;
begin
  RegDeleteValue(HKCU, InstallerRegistryKey, 'PendingOperationId');
  RegDeleteValue(HKCU, InstallerRegistryKey, 'PendingScope');
  RegDeleteValue(HKCU, InstallerRegistryKey, 'PendingMode');
  RegDeleteValue(HKCU, InstallerRegistryKey, 'PendingInstallationId');
end;

function ChooseUninstall(var Scope: String; var Mode: String;
  var InstallationId: String): Boolean;
var
  Choice: Integer;
  Confirmation: String;
begin
  Result := False;
  Mode := 'preserve-data';
  InstallationId := '';
  if UninstallSilent then
  begin
    Scope := 'all';
    Result := True;
    Exit;
  end;

  if LoadSelectedInstallation(InstallationId) then
  begin
    Choice := MsgBox(
      '请选择精确卸载范围：' + #13#10 + #13#10 +
      '“是”：仅删除最近一次由 Setup 选择或插件 bootstrap 明确绑定的 Vault 安装' + #13#10 +
      'installationId = ' + InstallationId + #13#10 + #13#10 +
      '“否”：删除账本中所有 Vault 的 OfferAgent 插件，并继续卸载全局安装器。' +
      #13#10 + 'Apps & Features 不代表某个“当前 Vault”；若不能确认上述 ID，' +
      '请取消或选择全部，安装器绝不根据路径猜测。',
      mbConfirmation, MB_YESNOCANCEL);
    if Choice = IDCANCEL then
      Exit;
    if Choice = IDYES then
    begin
      Confirmation := '';
      if (not InputQuery('确认单 Vault installationId',
        '请完整输入上方 installationId：', Confirmation)) or
        (Confirmation <> InstallationId) then
      begin
        MsgBox('installationId 不匹配；单 Vault 卸载已中止。', mbError, MB_OK);
        Exit;
      end;
      if not RunBootstrap('inno-ledger validate-uninstall --scope selected ' +
        '--installation-id "' + InstallationId + '" --mode preserve-data') then
      begin
        MsgBox('该 installationId 已失效或存在待恢复卸载；未持久化新的删除操作。',
          mbError, MB_OK);
        Exit;
      end;
      Scope := 'selected';
      Result := True;
      Exit;
    end;
  end
  else
  begin
    Choice := MsgBox(
      '最近一次明确绑定上下文的受保护 installationId 无法验证，因此不能安全执行单 Vault 卸载。' +
      #13#10 + '是否改为删除账本中所有 Vault 的 OfferAgent 插件？',
      mbConfirmation, MB_YESNO);
    if Choice <> IDYES then
      Exit;
  end;

  Scope := 'all';
  InstallationId := '';
  if MsgBox(
    '默认会保留本机 Session、索引和设置。是否彻底清除全部 OfferAgent 本机数据？' +
    #13#10 + 'Vault 笔记在任何模式下都不会被删除。',
    mbConfirmation, MB_YESNO) = IDYES then
  begin
    Confirmation := '';
    if (not InputQuery('彻底清除 OfferAgent 本机数据',
      '请输入 ' + PurgeConfirmation + ' 以再次确认：', Confirmation)) or
      (Confirmation <> PurgeConfirmation) then
    begin
      MsgBox('确认短语不匹配；卸载已中止，未删除任何数据。', mbError, MB_OK);
      Exit;
    end;
    if not RunBootstrap('inno-ledger validate-uninstall --scope all --mode purge-data ' +
      '--confirmation "' + PurgeConfirmation + '"') then
    begin
      MsgBox('无法证明至少一个已绑定 Runtime owner；pending-only 安装不能执行全局清除。' +
        #13#10 + '未持久化卸载操作。请重新选择“保留本机数据”。', mbError, MB_OK);
      Exit;
    end;
    Mode := 'purge-data';
  end;
  Result := True;
end;

function InitializeUninstall(): Boolean;
var
  FoundPending: Boolean;
  OperationId: String;
  InstallationId: String;
  Scope: String;
  Mode: String;
  Parameters: String;
begin
  Result := False;
  if not FileExists(ExpandConstant('{app}\offeragent-bootstrap.exe')) then
  begin
    MsgBox('签名 Runtime bootstrap 缺失；卸载已安全中止。', mbError, MB_OK);
    Exit;
  end;

  if not LoadPendingOperation(FoundPending, OperationId, Scope, Mode,
    InstallationId) then
  begin
    MsgBox('上次卸载操作记录无效；为避免误删其他 Vault，卸载已安全中止。',
      mbError, MB_OK);
    Exit;
  end;
  if not FoundPending then
  begin
    if not ChooseUninstall(Scope, Mode, InstallationId) then
      Exit;
    if (not NewOperationId(OperationId)) or
      (not StorePendingOperation(OperationId, Scope, Mode, InstallationId)) then
    begin
      MsgBox('无法持久化幂等卸载操作；卸载已安全中止。', mbError, MB_OK);
      Exit;
    end;
  end
  else if not UninstallSilent then
    MsgBox('将恢复上次未确认完成的同一卸载操作。', mbInformation, MB_OK);

  if Scope = 'selected' then
    Parameters := 'inno-ledger uninstall --operation-id "' + OperationId +
      '" --scope selected --installation-id "' + InstallationId +
      '" --mode preserve-data'
  else
  begin
    Parameters := 'inno-ledger uninstall --operation-id "' + OperationId +
      '" --scope all --mode ' + Mode;
    if Mode = 'purge-data' then
      Parameters := Parameters + ' --confirmation "' + PurgeConfirmation + '"';
  end;

  if not RunBootstrap(Parameters) then
  begin
    MsgBox('OfferAgent 未能确认 Runtime 已安全停止并完成精确卸载；' +
      '文件删除已中止，下次将恢复同一操作。', mbError, MB_OK);
    Exit;
  end;
  ClearPendingOperation;

  if Scope = 'selected' then
  begin
    MsgBox('已确认的 installationId 对应 OfferAgent 插件已安全删除。' + #13#10 +
      '全局安装器和其他 Vault 保持不变。', mbInformation, MB_OK);
    { Abort Inno's global AppId uninstall: the signed ledger coordinator has
      already removed only the exact selected plugin directory. }
    Result := False;
  end
  else
    { The ledger has removed every registered plugin.  Inno may now delete only
      its global application payload; no Vault ownership depends on its uninstall log. }
    Result := True;
end;
