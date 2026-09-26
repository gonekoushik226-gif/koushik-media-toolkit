; Inno Setup script for Media Toolkit.
; Normally compiled by build.py, which passes AppVersion / SourceDir / OutputDir.
; Manual compile:  ISCC.exe /DAppVersion=1.2.0 installer\MediaToolkit.iss

#ifndef AppVersion
  #define AppVersion "1.2.0"
#endif
#ifndef SourceDir
  #define SourceDir "..\dist\MediaToolkit"
#endif
#ifndef OutputDir
  #define OutputDir "..\release"
#endif
#define AppName "Media Toolkit"
#define AppPublisher "Media Toolkit"
#define AppExe "MediaToolkit.exe"

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
; A new version always replaces the installed one - never a second copy: reuse the previous
; install mode (just me / all users), folder and choices of an earlier version.
UsePreviousPrivileges=yes
UsePreviousAppDir=yes
UsePreviousTasks=yes
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
OutputDir={#OutputDir}
OutputBaseFilename=MediaToolkit-{#AppVersion}-Setup
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
; Version 1.0.0 used a longer product name. When upgrading, remove its executable and shortcuts
; (matched by the common "MediaToolkit" / " Media Toolkit" ending).
Type: files; Name: "{app}\*MediaToolkit.exe"
Type: files; Name: "{autoprograms}\* Media Toolkit.lnk"
Type: files; Name: "{autodesktop}\* Media Toolkit.lnk"

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"; Comment: "Video, audio, image, PDF and AI translation tools"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExe}"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent
