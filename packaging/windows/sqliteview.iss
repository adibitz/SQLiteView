; Inno Setup script for the Windows installer. Built by scripts/build.py when Inno Setup 6 (iscc) is installed:
;   iscc /DAppVersion=0.1.0 /DSourceDir=..\..\dist\SQliteView /DOutputDir=..\..\dist packaging\windows\sqliteview.iss

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef SourceDir
  #define SourceDir "..\..\dist\SQliteView"
#endif
#ifndef OutputDir
  #define OutputDir "..\..\dist"
#endif

[Setup]
AppId={{6E3B8C51-2F0A-4A57-9C59-3B1E7F2C4D90}
AppName=SQliteView
AppVersion={#AppVersion}
AppVerName=SQliteView {#AppVersion}
AppPublisher=SQliteView
AppPublisherURL=https://github.com/adibitz/SQLiteView
DefaultDirName={autopf}\SQliteView
DefaultGroupName=SQliteView
DisableProgramGroupPage=yes
OutputDir={#OutputDir}
OutputBaseFilename=SQliteView-{#AppVersion}-windows-x64-setup
SetupIconFile=..\..\sqliteview\assets\icons\sqliteview.ico
UninstallDisplayIcon={app}\SQliteView.exe
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequiredOverridesAllowed=dialog
ChangesAssociations=yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked
Name: "associate"; Description: "Open .db, .sqlite, .sqlite3 and .db3 files with SQliteView"; GroupDescription: "File associations:"; Flags: unchecked

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\SQliteView"; Filename: "{app}\SQliteView.exe"
Name: "{group}\Uninstall SQliteView"; Filename: "{uninstallexe}"
Name: "{autodesktop}\SQliteView"; Filename: "{app}\SQliteView.exe"; Tasks: desktopicon

[Registry]
Root: HKA; Subkey: "Software\Classes\SQliteView.Database"; ValueType: string; ValueName: ""; ValueData: "SQLite database"; Flags: uninsdeletekey; Tasks: associate
Root: HKA; Subkey: "Software\Classes\SQliteView.Database\DefaultIcon"; ValueType: string; ValueName: ""; ValueData: "{app}\SQliteView.exe,0"; Tasks: associate
Root: HKA; Subkey: "Software\Classes\SQliteView.Database\shell\open\command"; ValueType: string; ValueName: ""; ValueData: """{app}\SQliteView.exe"" ""%1"""; Tasks: associate
Root: HKA; Subkey: "Software\Classes\.db\OpenWithProgids"; ValueType: string; ValueName: "SQliteView.Database"; ValueData: ""; Flags: uninsdeletevalue; Tasks: associate
Root: HKA; Subkey: "Software\Classes\.sqlite\OpenWithProgids"; ValueType: string; ValueName: "SQliteView.Database"; ValueData: ""; Flags: uninsdeletevalue; Tasks: associate
Root: HKA; Subkey: "Software\Classes\.sqlite3\OpenWithProgids"; ValueType: string; ValueName: "SQliteView.Database"; ValueData: ""; Flags: uninsdeletevalue; Tasks: associate
Root: HKA; Subkey: "Software\Classes\.db3\OpenWithProgids"; ValueType: string; ValueName: "SQliteView.Database"; ValueData: ""; Flags: uninsdeletevalue; Tasks: associate

[Run]
Filename: "{app}\SQliteView.exe"; Description: "{cm:LaunchProgram,SQliteView}"; Flags: nowait postinstall skipifsilent
