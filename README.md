# Dedupe Studio

Find duplicate files by their content and clean them up safely. Nothing is ever deleted unless you choose to: duplicates are **moved** to a trash folder, every move is logged, and **Undo** puts them back.

Windows app (portable, no install). Also runs on Linux, macOS and Android (Termux) with Python 3.8+.

## Safety first

- **Trash, not delete.** Selected copies move to `_dedupe_trash\<date-time>\` inside the folder you scanned, keeping their folder structure. Keep it about two weeks; if nothing is missing, delete that folder yourself.
- **Undo.** Every move is written to `dedupe_log.tsv`. *Undo…* restores a whole run.
- **Careful mode (default)** always keeps at least one copy of every file.
- **Safe skips (default):** game folders, Windows system and app data, hidden dot folders, sync caches (Google Drive, Dropbox, iCloud, Syncthing) and code/build folders are never scanned. Game and app files repeat on purpose; removing them breaks things.
- **Delete permanently** exists for power users (for example, network or SSH drives where a trash folder is slow). It asks you to type `DELETE` and cannot be undone.

## Download and run (Windows)

1. Download `DedupeStudio-win64.zip` from [Releases](../../releases).
2. Extract it to a normal folder, for example `C:\DedupeStudio\` (avoid OneDrive folders).
3. Run `DedupeStudio.exe`. Windows SmartScreen may warn about an unknown publisher: click **More info → Run anyway**.

## How to use

1. Drag folders onto the window, or type or add them. Several folders: separate with `;`.
2. **First time: test on a small folder.**
3. Press **Scan**.
4. Press **Auto-select shown**. The default keeps the oldest copy (one per folder, up to two across folders).
5. **Scroll through the results and check what is ticked.** Use *Filter sets…* to search by name or path.
6. Press **Trash selected**.

## Options

| Option | What it does |
|---|---|
| Skip (dropdown) | Preset folders never scanned. OneDrive & WPS Cloud is off by default, so your synced documents are included |
| Exclude | Your own folder or file names to skip, separated by `;` (for example `Zomboid; Backups`) |
| Name filter | Only scan files whose name matches, e.g. `*.mp3` |
| Min size | Ignore files smaller than this |
| Hash | SHA-256 (default) or MD5 (matches rclone and Google Drive exports) |
| Hash list | Reuse hashes from a CSV instead of reading every file |
| Empty folders… | Finds and removes empty folders |
| Export CSV | Saves every duplicate set to a spreadsheet |

A history of every run across all folders is kept in `%USERPROFILE%\DedupeStudio\history.tsv`.

## Run from source (Linux, macOS, Termux)

```bash
python3 dedupe_studio.py              # opens in your browser
python3 dedupe_studio.py --no-browser # Termux: open the printed link in your phone's browser
```

## Troubleshooting

| Problem | Fix |
|---|---|
| "Failed to resolve Python.Runtime.Loader.Initialize" | Windows blocked the downloaded files. v1.3.1+ fixes this itself; otherwise right-click the zip → Properties → Unblock, then extract again |
| The window doesn't open | The app opens in your web browser instead; it works the same |
| A file shows as unreadable | The list shows the reason: open in another program, access denied, path too long, or a name with characters Windows can't open |
| Antivirus removed a file | Restore `_internal\pythonnet\runtime\Python.Runtime.dll` and allow the folder |

## Build the Windows app yourself

```powershell
powershell -ExecutionPolicy Bypass -File build.ps1
```

Output: `dist\DedupeStudio\DedupeStudio.exe` and `dist\DedupeStudio-win64.zip`.

## License

MIT. See [LICENSE](LICENSE).
