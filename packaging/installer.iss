; Inno Setup 6 script for APFS Explorer Lite. Built by scripts\build_release.py, which passes:
;   /DAppVersion=<version>  /DSourceDir=<dist\APFS Explorer Lite>  /DOutputDir=<dist>
; Installs for the current user by default (no administrator rights needed); the
; setup offers "install for all users" (needs administrator rights) as an option.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef SourceDir
  #define SourceDir "..\dist\APFS Explorer Lite"
#endif
#ifndef OutputDir
  #define OutputDir "..\dist"
#endif
#define AppName "APFS Explorer Lite"
#define AppExe "APFS Explorer Lite.exe"
#define ToolsExe "APFS Explorer Lite Tools.exe"

[Setup]
AppId={{6F0C2B5E-8C3A-4F7B-9B8E-2D7A4E1C9A51}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=APFS Explorer Lite project
VersionInfoVersion={#AppVersion}
VersionInfoProductName={#AppName}
VersionInfoDescription={#AppName} setup
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0.17763
SetupIconFile=..\src\apfs_explorer_lite\resources\app.ico
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName} {#AppVersion}
OutputDir={#OutputDir}
OutputBaseFilename=APFS-Explorer-Lite-{#AppVersion}-setup
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
InfoBeforeFile={#SourceDir}\README.txt

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{group}\Hardware test (read-only)"; Filename: "{app}\Run hardware test.bat"; IconFilename: "{app}\{#ToolsExe}"
Name: "{group}\Diagnose drive (read-only)"; Filename: "{app}\Diagnose drive.bat"; IconFilename: "{app}\{#ToolsExe}"
Name: "{group}\Read me"; Filename: "{app}\README.txt"
Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExe}"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent

; Settings (HKCU\Software\APFS Explorer Lite) and logs (%LOCALAPPDATA%\APFS Explorer Lite)
; are left in place on uninstall; README.txt says how to remove them.
