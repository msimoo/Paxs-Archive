; Inno Setup 6 Script for PAXS Archive Standalone (Windows 10/11 x64)
; Build via: installer\build_installer.bat (requires ISCC.exe from jrsoftware.org)

#define MyAppName "PAXS Archive"
#define MyAppVersion "1.0.0"
#define MyAppPublisher "PAXS Medical Imaging"
#define MyAppExeName "PAXS-Archive.exe"
#define SourceDir "..\dist\PAXS-Server"

[Setup]
AppId={{8F54D7F1-6C4E-42B1-9B11-7E4B6C24A901}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\PAXS-Archive
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=output
OutputBaseFilename=PAXS-Archive-Setup-{#MyAppVersion}
Compression=lzma2/normal
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=admin
UninstallDisplayIcon={app}\{#MyAppExeName}

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &Desktop shortcut for PAXS Archive"; GroupDescription: "Shortcuts:"
Name: "startmenu"; Description: "Create Start Menu shortcuts (Desktop App, Headless Server, Health Monitor)"; GroupDescription: "Shortcuts:"
Name: "installservice"; Description: "Register PAXS-Server.exe as an auto-start Windows Service (requires NSSM on PATH)"; GroupDescription: "Server Mode:"; Flags: unchecked

[Dirs]
Name: "{app}\storage"; Permissions: users-modify
Name: "{app}\backups"; Permissions: users-modify
Name: "{app}\certs"; Permissions: users-modify
Name: "{app}\logs"; Permissions: users-modify

[Files]
Source: "{#SourceDir}\PAXS-Archive.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#SourceDir}\PAXS-Server.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#SourceDir}\PAXS-Worker.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#SourceDir}\_internal\*"; DestDir: "{app}\_internal"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#SourceDir}\.env.example"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#SourceDir}\health_monitor.bat"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#SourceDir}\install_service.bat"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#SourceDir}\start_paxs_server.bat"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#SourceDir}\PAXS-Archive-fullscreen.bat"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#SourceDir}\dcmtk\*"; DestDir: "{app}\dcmtk"; Flags: ignoreversion recursesubdirs createallsubdirs skipifsourcedoesntexist
Source: "..\scripts\preflight_check.bat"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\PAXS Archive"; Filename: "{app}\PAXS-Archive.exe"; Tasks: startmenu
Name: "{group}\PAXS Archive (Full Screen)"; Filename: "{app}\PAXS-Archive-fullscreen.bat"; Tasks: startmenu
Name: "{group}\PAXS Headless Server"; Filename: "{app}\start_paxs_server.bat"; Tasks: startmenu
Name: "{group}\PAXS Health Monitor"; Filename: "{app}\health_monitor.bat"; Tasks: startmenu
Name: "{group}\PAXS Pre-Flight Diagnostics"; Filename: "{app}\preflight_check.bat"; Tasks: startmenu
Name: "{autodesktop}\PAXS Archive"; Filename: "{app}\PAXS-Archive.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\install_service.bat"; WorkingDir: "{app}"; Flags: runhidden waituntilterminated; Tasks: installservice
Filename: "{app}\preflight_check.bat"; Description: "Run PAXS Pre-Flight Diagnostics"; Flags: postinstall shellexec skipifsilent unchecked
Filename: "{app}\PAXS-Archive.exe"; Description: "Launch PAXS Archive"; Flags: postinstall nowait skipifsilent unchecked

[UninstallDelete]
; Preserve clinical archive data, backups, certificates, and .env on uninstall/upgrade!
Type: filesandordirs; Name: "{app}\_internal"
Type: filesandordirs; Name: "{app}\webview-data"
