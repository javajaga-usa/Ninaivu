; pynsist's own template, with these additions: NINAIVU_HOME for the tray, a
; "start at sign-in" box on a components page, the product's name, version and
; copyright in the installer's properties, and a tidy stop before an upgrade or
; an uninstall, with the old program removed before an upgrade (the last two
; from Ninaivu Lite). Everything else is pynsist's; see
; https://github.com/takluyver/pynsist/blob/master/nsist/pyapp.nsi
; Keep this file plain ASCII: pynsist writes it in the Windows code page.
[% extends "pyapp.nsi" %]

[% block ui_pages %]
  ; A running Ninaivu, or an open Control Panel or tray, holds pythonw.exe
  ; (and python.exe, and the DLLs they loaded) open, and Windows will not let
  ; the installer write over a file in use: an upgrade stopped with a write
  ; error half-way. So, before any file is written: ask the server to stop,
  ; the way the Control Panel does (never by force), then, while either
  ; program is still in use, ask the person to stop it and close the panel,
  ; with Retry.
  !macro WaitUntilNotInUse
    ; One id for this insertion's labels (the macro is used twice).
    !define U ${__COUNTER__}
    IfFileExists "$INSTDIR\Python\pythonw.exe" 0 not_in_use_${U}
    ExecWait '"$INSTDIR\Python\pythonw.exe" -m ninaivu.desktop.control --stop'
    check_${U}:
      ClearErrors
      FileOpen $0 "$INSTDIR\Python\pythonw.exe" a
      IfErrors in_use_${U}
      FileClose $0
      IfFileExists "$INSTDIR\Python\python.exe" 0 not_in_use_${U}
      FileOpen $0 "$INSTDIR\Python\python.exe" a
      IfErrors in_use_${U}
      FileClose $0
      Goto not_in_use_${U}
    in_use_${U}:
      MessageBox MB_RETRYCANCEL|MB_ICONEXCLAMATION \
        "Ninaivu is still running, so its files cannot be replaced.$\r$\n$\r$\nIn the Ninaivu Control Panel press Stop (or close the window Ninaivu was started from), then close the Control Panel and the Ninaivu tray, and press Retry." \
        IDRETRY check_${U}
      Abort "Ninaivu is still running. Stop it and close the Control Panel, then run this installer again."
    not_in_use_${U}:
    !undef U
  !macroend

  ; What Windows shows under Properties, Details.
  VIProductVersion "[[ ib.version ]].0"
  VIAddVersionKey "ProductName" "Ninaivu"
  VIAddVersionKey "ProductVersion" "[[ ib.version ]]"
  VIAddVersionKey "FileVersion" "[[ ib.version ]]"
  VIAddVersionKey "FileDescription" "Ninaivu installer"
  VIAddVersionKey "CompanyName" "Jagadeesh Rajendran"
  VIAddVersionKey "LegalCopyright" "(c) 2026 Jagadeesh Rajendran. MIT licence."
  ; A components page ahead of pynsist's own pages, so "Start Ninaivu at
  ; sign-in" is a box the person can untick rather than always happening.
  !insertmacro MUI_PAGE_COMPONENTS
  [[ super() ]]
[% endblock %]

[% block install_pkgs %]
  ; An upgrade: nothing is written while the old one is in use, then the old
  ; program is removed whole, so no file of an earlier version is left among
  ; the new ones (a module deleted upstream would otherwise still import).
  ; Only the installed program goes: the private Python, the packages, the
  ; commands and the files beside them. The household's data (the index,
  ; settings, thumbnails, people) lives in its own folder and the Control
  ; Panel's run-time files in .ninaivu-control; neither is touched, and the
  ; photographs never are. Nothing happens on a first install, and only
  ; these named folders are removed, never the whole install folder, in case
  ; it was chosen to be a shared one.
  !insertmacro WaitUntilNotInUse
  DetailPrint "Removing the previous Ninaivu program files..."
  RMDir /r "$INSTDIR\Python"
  RMDir /r "$INSTDIR\pkgs"
  RMDir /r "$INSTDIR\bin"
  Delete "$INSTDIR\README.md"
  Delete "$INSTDIR\LICENSE"
  Delete "$INSTDIR\_system_path.py"
  [[ super() ]]
[% endblock %]

[% block install_files %]
  !include "WinMessages.nsh"     ; ${HWND_BROADCAST}, ${WM_WININICHANGE} below
  [[ super() ]]
  ; The tray keeps its run-time folder and the server log beside the
  ; installation rather than in site-packages (desktop/control.py reads this).
  WriteRegStr HKCU "Environment" "NINAIVU_HOME" "$INSTDIR"
  ; Tell running programs the environment changed, so a tray started from the
  ; Start menu right after installing sees it.
  SendMessage ${HWND_BROADCAST} ${WM_WININICHANGE} 0 "STR:Environment" /TIMEOUT=2000
[% endblock %]

[% block install_shortcuts %]
  [[ super() ]]
  ; The Control Panel on the Desktop too: it is the one thing the person who
  ; runs the house opens, to start and stop Ninaivu and see how it is doing.
  [% for scname, sc in ib.shortcuts.items() %][% if scname == 'Ninaivu' %]
  CreateShortCut "$DESKTOP\Ninaivu Control Panel.lnk" "[[sc['target'] ]]" \
    '[[ sc['parameters'] ]]' "$INSTDIR\[[ sc['icon'] ]]"
  [% endif %][% endfor %]
[% endblock %]

[% block uninstall_shortcuts %]
  [[ super() ]]
  Delete "$DESKTOP\Ninaivu Control Panel.lnk"
[% endblock %]

[% block uninstall_files %]
  ; Stop it and stop starting it while the program is still there to ask,
  ; and wait until nothing of it is in use, as an upgrade does. The data
  ; folder is left alone, and so, always, are the photographs.
  ExecWait '"$INSTDIR\Python\pythonw.exe" -m ninaivu.desktop.control --stop'
  ExecWait '"$INSTDIR\Python\pythonw.exe" -m ninaivu.desktop.autostart --disable'
  !insertmacro WaitUntilNotInUse
  [[ super() ]]
  DeleteRegValue HKCU "Environment" "NINAIVU_HOME"
  DeleteRegValue HKCU "Software\Microsoft\Windows\CurrentVersion\Run" "Ninaivu"
[% endblock %]

[% block sections %]
  [[ super() ]]
  Section "Start Ninaivu at sign-in" sec_autostart
    ; The same value desktop/autostart.py writes from the tray's menu.
    WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Run" "Ninaivu" \
      '"$INSTDIR\Python\pythonw.exe" -m ninaivu.desktop.autostart --start'
  SectionEnd
[% endblock %]
