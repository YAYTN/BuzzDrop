# Changelog

## 1.0.0

First release of BuzzDrop: drop a file, get a link; click a link, get the file.

### Highlights
- **Runs everywhere:** Windows, macOS and Linux, plus a `buzz` command line that also works over SSH.
- **Tiny and dependency-free:** one ~11 MB file, pure Python standard library, nothing else to install.
- **One window:** Send / Receive / History tabs, drag & drop, paste-to-send, live speed and time left, light/dark/system themes, notifications, keyboard shortcuts, high-DPI.
- **Smarter receiving:** resumes after dropped connections, offers to download links you copy, keeps a searchable history, global hotkey on Windows.
- **Quiet one-click updates (Windows):** download in the background, verified by SHA-256 and test-started before anything is replaced, installed only when you restart or close the app, with the previous version kept as a backup until the new one has run fine. `buzz update` does the same from a terminal.
- **Both kinds of link, always:** every finished upload (and every History entry) has a button for the `BZ://` link and one for the plain web link, so you can share with people who don't have BuzzDrop without touching a setting.
- **Opt-in privacy:** *Forget my history automatically* (on quit, or after 1 / 7 / 30 days). Only the list is cleared, never files or links. Off by default.
- **Reliable sending:** a dropped upload restarts automatically (up to 3 times). Optional split uploads send huge files in separately retried parts; every part is a normal BuzzHeavier file with its own link.

### Safe by design
- BZ:// links are passed as `--protocol "<link>"` and only link codes are read after it, so a crafted link can never inject `--send <file>`.
- Hotkeys must include Ctrl, Alt or Win (or be an F-key), so Shift+letter can't hijack capital letters.
- ZIPs are extracted safely: no zip-slip, never overwrites, cleaned up if extraction fails; only real `.zip` files are extracted (not `.docx`, `.jar`, `.apk` …).
- Downloads go to `.part` files and are renamed only when complete, so an interrupted download never looks finished.
- Disk problems (full, no permission, name too long) are reported as such and not retried as network errors; very long names are shortened to fit.
- Temporary ZIPs are always deleted, even after a failed upload on Windows.
- File names keep their punctuation and Unicode; the server's UTF-8 `filename*` is honoured; uploads work for names with spaces, `#`, `?` and `%`.
- The Downloads folder is found even if Windows has moved it.
- BuzzDrop never tries to get around a BuzzHeavier browser check; it offers to open the link in your browser instead.
