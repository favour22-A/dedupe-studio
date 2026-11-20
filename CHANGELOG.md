# Changelog

Every version below is tagged in git (`git checkout v1.3.5` gets that exact code). Built downloads are kept in `..\releases\<version>\`.

## v1.3.8 — 2026-11-20
- Activity log of every stage, folder, file and error (`%USERPROFILE%\DedupeStudio\activity.log`), with a **Log** button.
- Quick-check now shows a counter and a moving bar; a "now:" line shows the exact file being read.
- Linux: Debian/Ubuntu package (`dedupe-studio` window + `dedupe-studio-cli` terminal tool).

## v1.3.7 — 2026-11-20
- **Copy all** and **Save as .txt** on the unreadable list and on run results.
- Downloads (Export CSV, Save as .txt) allowed inside the app window.

## v1.3.6 — 2026-11-20
- Long paths: trash, delete and undo work past 260 characters (fixed WinError 3). Backwards compatible with older logs and trash runs.

## v1.3.5 — 2026-11-20
- Drag and drop folders (or a file, which adds its folder) onto the window.

## v1.3.4 — 2026-11-20
- Safe skips on by default: Games, Windows system & app data, hidden dot folders, sync caches, code folders. OneDrive & WPS Cloud split out and off.
- First-run safety guide.
- README and MIT license.

## v1.3.3 — 2026-11-20
- **Skip** dropdown with presets.

## v1.3.2 — 2026-11-20
- Skip game folders (Steam, Epic, GOG, Xbox, Minecraft, Roblox, Project Zomboid…), on by default.

## v1.3.1 — 2026-11-20
- Fix for other PCs: removes Windows' "downloaded from the internet" mark before loading .NET (the "Failed to resolve Python.Runtime.Loader.Initialize" crash).
- If the app window can't start, it opens in the browser instead of crashing.
- Title shows the real version.

## v1.3 — 2026-11-19
- Fixed: **Add folder** opened a second copy of the app instead of a folder picker.
- Trash and delete run in the background with progress, Cancel and a result panel.
- Optional **Delete permanently** (type DELETE to confirm).
- Master history of every trash/delete run (`%USERPROFILE%\DedupeStudio\history.tsv`).
- Reasons shown for every unreadable file.

## v1.2 — 2026-11-18
- First Windows app: the Python studio wrapped as a desktop window (pywebview + PyInstaller). Last build before the fixes above.
