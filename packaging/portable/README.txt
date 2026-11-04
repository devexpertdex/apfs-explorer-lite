APFS Explorer Lite {VERSION} (portable)
=====================================

Reads files from an external APFS (Mac-formatted) SSD on Windows 10/11 and copies
the files you choose to a folder on this PC. Nothing on the APFS drive is ever
changed. No internet connection is used or needed.

Start
-----
1. Keep this whole folder together (the .exe needs the "_internal" folder next to it).
   You can put it anywhere, for example in Documents or on a USB stick.
2. Double-click "APFS Explorer Lite.exe".
3. Click "Restart as Administrator" and answer Yes. Windows only lets programs
   running as administrator read a disk directly. You can browse the drive list
   without it, but you can't open the APFS drive.
4. Click "Refresh Drives", select the APFS container, click Open.
5. Select files or folders and click "Copy Selected".

First start: Windows SmartScreen may say "Windows protected your PC", because
this program is not digitally signed. Click "More info", then "Run anyway".

Read-only tools (double-click; they ask for administrator rights)
-----------------------------------------------------------------
  Run hardware test.bat   checks the app on your SSD: finds it, lists its folders,
                          copies samples to a temporary folder, checks every copy
                          and confirms Windows recorded no writes to the SSD.
                          Results: Documents\APFS Explorer Lite hardware test.json
  Diagnose drive.bat      if the drive isn't found: writes a report to
                          Documents\APFS Explorer Lite drive report.txt

"APFS Explorer Lite Tools.exe selftest" checks this copy of the program (no admin needed).

Where things are stored
-----------------------
  Settings:  registry, HKEY_CURRENT_USER\Software\APFS Explorer Lite
  Log:       %LOCALAPPDATA%\APFS Explorer Lite\logs  (no file names or contents)
To remove the portable app, delete this folder (and optionally the two items above).

Licenses: see THIRD-PARTY-NOTICES.txt and the "licenses" folder.
