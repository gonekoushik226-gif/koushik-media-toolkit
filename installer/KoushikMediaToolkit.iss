; Inno Setup script for Koushik Media Toolkit.
; Normally compiled by build.py, which passes AppVersion / SourceDir / OutputDir.
; Manual compile:  ISCC.exe /DAppVersion=1.0.0 installer\KoushikMediaToolkit.iss

#ifndef AppVersion
  #define AppVersion "1.0.0"
#endif
#ifndef SourceDir
  #define SourceDir "..\dist\KoushikMediaToolkit"
#endif
#ifndef OutputDir
  #define OutputDir "..\release"
#endif
#define AppName "Koushik Media Toolkit"
#define AppPublisher "Koushik"
#define AppExe "KoushikMediaToolkit.exe"

[Setup]
; Never change AppId: Windows uses it to recognise upgrades and the uninstall entry.
AppId={{8F2C6E3A-5B1D-4C7E-9A42-3D6B1E7F0C21}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
VersionInfoVersion={#AppVersion}
VersionInfoProductName={#AppName}
VersionInfoDescription={#AppName} Setup
; Installs for the current user without administrator rights by default;
; the first wizard page lets the user choose "install for all users" instead.
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog commandline
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
OutputDir={#OutputDir}
OutputBaseFilename=KoushikMediaToolkit-{#AppVersion}-Setup
SetupIconFile=..\assets\app.ico
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName}
Compression=lzma2/ultra64
SolidCompression=yes
LZMAUseSeparateProcess=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
WizardStyle=modern
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[InstallDelete]
; Remove the previous version's runtime files before an upgrade (settings are kept in AppData).
Type: filesandordirs; Name: "{app}\_internal"

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"; Comment: "Video, audio, image and PDF tools"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExe}"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent
