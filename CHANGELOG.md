# Changelog

## 1.1.2

- **Uninstall can't get stuck any more:** the final "BuzzDrop has been removed" message could open behind other windows. Until someone clicked it, BuzzDrop stayed in the Task Manager and its file could not be deleted, and the cleanup gave up after about 90 seconds. The message is now always on top, closes itself after a minute, the program is forced to end right after, and the cleanup keeps trying for 15 minutes.

## 1.1.1

- **Calmer uninstall (Windows):** no more console window flashing open and closed while BuzzDrop removes itself. The two questions are now one dialog with "Also delete my settings and history" as a tick box, and **Cancel, Esc and the X all really cancel** (before, the second question had no way out).
- **Clean restart after an update (Windows):** when BuzzDrop restarted itself after installing an update, the new copy shared the old copy's temporary folder, so the old copy could not clean up and Windows showed *"Failed to remove temporary directory"* (and left a `_MEIâ€¦` folder in Temp). The restart now starts a fully independent copy. The one-time warning can still appear when updating *from* 1.1.0 or older, because those versions do the restarting; it is harmless, and the leftover `_MEIâ€¦` folder in your Temp folder can be deleted once BuzzDrop is closed.

## 1.1.0

### Easy, complete uninstall
- **Settings â†’ About â†’ Uninstallâ€¦**, or **Settings â†’ Apps â†’ BuzzDrop â†’ Uninstall** in Windows, or `buzz uninstall` in a terminal. It explains what will be removed, asks whether to keep your settings and history, and then removes everything BuzzDrop set up: `BZ://` links, the right-click menu, the Start menu shortcut, start-at-login, the `buzz` command and its PATH entry, the entry in Settings â†’ Apps, Windows' own bookkeeping about the program, any unfinished update files, your settings and history (if you choose), and finally the program file itself.
- **It proves it:** when it finishes, it scans every place BuzzDrop uses and tells you whether anything is left. The result is part of the message you see.
- **Careful by design:** only files BuzzDrop created are deleted (the settings folder is cleaned by file name, never wiped), a `BZ://` handler that now belongs to another program is left alone, a running copy in the tray is asked to quit first, and downloaded files and shared links are never touched.
- BuzzDrop now appears in **Settings â†’ Apps** (packaged Windows app, not in portable mode), so the usual place to look for an uninstaller has one.
- Switching off the `buzz` command now removes its (empty) folder, and on Linux uninstalling also removes the copied icon and the default-app entry for `bz://`.

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
- ZIPs are extracted safely: no zip-slip, never overwrites, cleaned up if extraction fails; only real `.zip` files are extracted (not `.docx`, `.jar`, `.apk` â€¦).
- Downloads go to `.part` files and are renamed only when complete, so an interrupted download never looks finished.
- Disk problems (full, no permission, name too long) are reported as such and not retried as network errors; very long names are shortened to fit.
- Temporary ZIPs are always deleted, even after a failed upload on Windows.
- File names keep their punctuation and Unicode; the server's UTF-8 `filename*` is honoured; uploads work for names with spaces, `#`, `?` and `%`.
- The Downloads folder is found even if Windows has moved it.
- BuzzDrop never tries to get around a BuzzHeavier browser check; it offers to open the link in your browser instead.
