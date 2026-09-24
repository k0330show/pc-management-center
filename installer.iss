#define SetupVersion "0.11"

; パスはこのファイルの置き場所からの相対パス（ソースのフォルダで ISCC installer.iss を実行する）
[Setup]
AppId=PCManagementCenter
AppName=PC管理センター
AppVersion={#SetupVersion}
VersionInfoVersion=0.11.0.0
DefaultDirName={localappdata}\Programs\PCManagementCenter
DefaultGroupName=PC管理センター
PrivilegesRequired=lowest
OutputDir=..\installer
OutputBaseFilename=PC管理センター_Setup_{#SetupVersion}
Compression=lzma
SolidCompression=yes
WizardStyle=modern

[Files]
Source: "dist\PC管理センター\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\PC管理センター"; Filename: "{app}\PC管理センター.exe"
Name: "{autodesktop}\PC管理センター"; Filename: "{app}\PC管理センター.exe"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "デスクトップにショートカットを作成"; GroupDescription: "追加のショートカット:"

[Run]
Filename: "{app}\PC管理センター.exe"; Description: "PC管理センターを起動"; Flags: nowait postinstall skipifsilent