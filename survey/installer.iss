; Inno Setup script for TrafficLens Survey.
;
; Produces a single TrafficLens-Setup.exe: the surveyor double-clicks it, clicks Next a
; couple of times, and gets a Start Menu entry and a desktop icon. That is what "install
; on Windows" means to somebody who is not a developer, and it is the difference between
; a tool they use and a zip they never unpack.
;
; PrivilegesRequired=lowest is deliberate. Installing per-user into %LOCALAPPDATA% needs
; no administrator, which matters on managed government and contractor laptops where the
; surveyor simply does not have the password. It also keeps the app out of Program Files,
; which is read-only to a normal user.

#define AppName      "TrafficLens Survey"
#define AppShort     "TrafficLens"
#define AppPublisher "TrafficLens"
#ifndef AppVersion
  #define AppVersion "1.0.0"
#endif

[Setup]
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={autopf}\{#AppShort}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
; No admin prompt, and installs somewhere the user can actually write.
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
OutputBaseFilename=TrafficLens-Setup
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; ~2GB of torch and weights: say so before they start, not after.
DiskSpanning=no
; Shut the app down before replacing its files. Without this Inno cannot overwrite a
; running TrafficLens.exe and quietly keeps the old one -- the installer reports success,
; the surveyor reopens the app, and none of the new work is there. That is exactly what a
; wrong-build report looks like, and it is indistinguishable from a failed build unless
; the app can state its own version, which it now does on the first screen.
CloseApplications=yes
CloseApplicationsFilter=*.exe
RestartApplications=no
UninstallDisplayName={#AppName}

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; \
  GroupDescription: "Shortcuts:"

[Files]
; The whole PyInstaller onedir output. recursesubdirs picks up _internal, which holds
; torch, the weights, ffprobe and the UI.
Source: "..\dist\TrafficLens\*"; DestDir: "{app}"; \
  Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\TrafficLens.exe"
Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\TrafficLens.exe"; \
  Tasks: desktopicon

[Run]
Filename: "{app}\TrafficLens.exe"; Description: "Start {#AppName}"; \
  Flags: nowait postinstall skipifsilent

[UninstallDelete]
; The install directory only. A surveyor's counts and verdicts live in
; %LOCALAPPDATA%\TrafficLens and are NEVER removed by uninstalling: reinstalling to fix a
; problem must not be the thing that destroys a week of survey work.
Type: filesandordirs; Name: "{app}\_internal"
