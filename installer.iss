; Instalátor File2TV (Inno Setup 6). Sestavuje ho build.ps1:
;   ISCC /DAppVersion=1.0.0 /DSourceDir=dist\File2TV /DIconFile=...\file2tv.ico installer.iss
; Instaluje se jen pro aktuálního uživatele (bez práv správce) do %LOCALAPPDATA%\Programs\File2TV.
; Pravidlo firewallu je volitelná úloha – jediný dotaz na oprávnění správce (UAC) během instalace.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef SourceDir
  #define SourceDir "dist\File2TV"
#endif

[Setup]
AppId={{23595DBD-7778-4B21-9B21-685A4369A150}
AppName=File2TV
AppVersion={#AppVersion}
AppVerName=File2TV {#AppVersion}
AppPublisher=Tomáš Holčík - czmanix
AppPublisherURL=https://github.com/czmanix/File2TV
AppSupportURL=https://github.com/czmanix/File2TV/issues
PrivilegesRequired=lowest
DefaultDirName={autopf}\File2TV
DisableProgramGroupPage=yes
DisableDirPage=auto
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
WizardStyle=modern
OutputDir=dist
OutputBaseFilename=File2TV-{#AppVersion}-setup
Compression=lzma2/max
SolidCompression=yes
#ifdef IconFile
SetupIconFile={#IconFile}
#endif
UninstallDisplayIcon={app}\File2TV.exe
UninstallDisplayName=File2TV
; Běžící File2TV se před instalací/odinstalací zavře (Restart Manager).
CloseApplications=force
ShowLanguageDialog=auto

[Languages]
Name: "en"; MessagesFile: "compiler:Default.isl"
Name: "cs"; MessagesFile: "compiler:Languages\Czech.isl"
Name: "sk"; MessagesFile: "compiler:Languages\Slovak.isl"
Name: "de"; MessagesFile: "compiler:Languages\German.isl"
Name: "pl"; MessagesFile: "compiler:Languages\Polish.isl"
Name: "fr"; MessagesFile: "compiler:Languages\French.isl"
Name: "es"; MessagesFile: "compiler:Languages\Spanish.isl"
Name: "it"; MessagesFile: "compiler:Languages\Italian.isl"

[CustomMessages]
en.FirewallTask=Allow File2TV in Windows Firewall for private networks (the TV downloads the video from this PC; asks for administrator permission)
cs.FirewallTask=Povolit File2TV ve Windows firewallu pro soukromé sítě (TV si video stahuje z tohoto PC; zeptá se na oprávnění správce)
sk.FirewallTask=Povoliť File2TV v bráne firewall systému Windows pre súkromné siete (televízor si video sťahuje z tohto PC; spýta sa na oprávnenie správcu)
de.FirewallTask=File2TV in der Windows-Firewall für private Netzwerke zulassen (der Fernseher lädt das Video von diesem PC; fragt nach Administratorrechten)
pl.FirewallTask=Zezwól na File2TV w Zaporze systemu Windows dla sieci prywatnych (telewizor pobiera wideo z tego komputera; poprosi o uprawnienia administratora)
fr.FirewallTask=Autoriser File2TV dans le pare-feu Windows pour les réseaux privés (le téléviseur télécharge la vidéo depuis ce PC ; demande les droits d'administrateur)
es.FirewallTask=Permitir File2TV en el Firewall de Windows para redes privadas (el televisor descarga el vídeo desde este PC; pide permisos de administrador)
it.FirewallTask=Consenti File2TV in Windows Firewall per le reti private (il televisore scarica il video da questo PC; chiede i diritti di amministratore)

[Tasks]
Name: "firewall"; Description: "{cm:FirewallTask}"
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\File2TV"; Filename: "{app}\File2TV.exe"
Name: "{autodesktop}\File2TV"; Filename: "{app}\File2TV.exe"; Tasks: desktopicon

[Registry]
; Značka, že pravidlo firewallu vytvořil instalátor – odinstalace ho pak smaže.
Root: HKCU; Subkey: "Software\File2TV"; ValueType: dword; ValueName: "FirewallRule"; ValueData: 1; Tasks: firewall; Flags: uninsdeletekey

[Run]
; Smazat a znovu přidat (při přeinstalaci by jinak vznikla dvojí pravidla) – jeden dotaz UAC.
Filename: "{cmd}"; Parameters: "/c netsh advfirewall firewall delete rule name=""File2TV"" & netsh advfirewall firewall add rule name=""File2TV"" dir=in action=allow program=""{app}\File2TV.exe"" enable=yes profile=private,domain remoteip=localsubnet"; Verb: runas; Flags: shellexec runhidden waituntilterminated; Tasks: firewall
Filename: "{app}\File2TV.exe"; Description: "{cm:LaunchProgram,File2TV}"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "{sys}\taskkill.exe"; Parameters: "/IM File2TV.exe /F"; Flags: runhidden; RunOnceId: "StopApp"
Filename: "{cmd}"; Parameters: "/c netsh advfirewall firewall delete rule name=""File2TV"""; Verb: runas; Flags: shellexec runhidden waituntilterminated; RunOnceId: "DeleteFirewallRule"; Check: FirewallRuleCreated

[UninstallDelete]
; „Odeslat do → File2TV“ vytváří aplikace sama při prvním spuštění.
Type: files; Name: "{userappdata}\Microsoft\Windows\SendTo\File2TV.lnk"

[Code]
function FirewallRuleCreated: Boolean;
begin
  Result := RegValueExists(HKCU, 'Software\File2TV', 'FirewallRule');
end;
