#define AppVersion "0.1.0"

[Setup]
AppId={{7BE4E046-2265-45B6-9E67-AC680C5FAB33}
AppName=PaperDesk
AppVersion={#AppVersion}
AppPublisher=Gabriel Abdelnoor
AppPublisherURL=https://github.com/gjabdelnoor/paperdesk
DefaultDirName={localappdata}\Programs\PaperDesk
DefaultGroupName=PaperDesk
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\dist
OutputBaseFilename=PaperDesk-windows-x86_64-setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\PaperDesk.exe
LicenseFile=..\LICENSE

[Files]
Source: "..\dist\PaperDesk\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\PaperDesk"; Filename: "{app}\PaperDesk.exe"
Name: "{group}\PaperDesk Simulation"; Filename: "{app}\PaperDesk.exe"; Parameters: "--demo"

[Run]
Filename: "{app}\PaperDesk.exe"; Description: "Open PaperDesk"; Flags: nowait postinstall skipifsilent
