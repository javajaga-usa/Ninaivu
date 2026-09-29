; pynsist's own template, with two additions: NINAIVU_HOME for the tray, and
; a "start at sign-in" box on a components page. Everything else is pynsist's;
; see https://github.com/takluyver/pynsist/blob/master/nsist/pyapp.nsi
[% extends "pyapp.nsi" %]

[% block ui_pages %]
  ; A components page ahead of pynsist's own pages, so "Start Ninaivu at
  ; sign-in" is a box the person can untick rather than always happening.
  !insertmacro MUI_PAGE_COMPONENTS
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

[% block uninstall_files %]
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
