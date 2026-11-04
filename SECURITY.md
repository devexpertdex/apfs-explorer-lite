# Security policy

## Reporting a vulnerability

Please **don't open a public issue** for security problems. Use GitHub's private
reporting instead: **Security** tab → **Report a vulnerability**. Include:
- the app version;
- your Windows version;
- steps to reproduce;
- what you expected to happen and what happened.

Only the latest release is supported.

## What counts as a security issue here

APFS Explorer Lite's main promise is that it **never writes to the source APFS drive**,
and only writes inside the destination folder you choose. These count as security issues:
- Any way to make the app write to, format, repartition or otherwise modify the
  source disk.
- Any way to make it write outside the chosen destination folder: path traversal,
  following links or junctions, device paths.
- Overwriting an existing file without your explicit "Replace".
- File names, file contents or other private data appearing in the app's log
  (`%LOCALAPPDATA%\APFS Explorer Lite\logs`).
- Code execution through a crafted APFS volume, for example a malicious file name or
  damaged metadata. APFS parsing is done by libfsapfs
  (https://github.com/libyal/libfsapfs); issues inside libfsapfs itself should also be
  reported upstream.

## Things to know

- The app needs administrator rights to read a whole disk. It starts without them and
  asks only when you open a drive; the disk is opened read-only.
- The app makes no network connections and downloads nothing at runtime.
- Release files are not digitally signed. Compare them with the SHA-256 hashes in
  `BUILD-MANIFEST.txt` from the same release.
