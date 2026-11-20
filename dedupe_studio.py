#!/usr/bin/env python3
"""
Dedupe Studio v1.3 — single-file duplicate-cleanup app with a local UI.
Python 3.8+ stdlib only. Runs on Windows, Linux, Termux, macOS.

    python dedupe_studio.py [folder ...] [--port 7788] [--no-browser]

It prints a URL with a one-time key (http://127.0.0.1:7788/?t=...) and opens it.
On Termux: run with --no-browser, then open the printed URL in your Android browser.

GUARANTEES
  - Never deletes. "Trash" MOVES files to <root>/_dedupe_trash/<stamp>/ with
    folder structure preserved; every move is logged to <root>/dedupe_log.tsv
    (same format as dedupe.sh, so either tool can undo the other's run).
    With several roots, each copy goes to the trash on its OWN root/drive.
  - Careful mode: at least one copy of every duplicate set must survive.
  - Destructive mode: lets you select every copy in a set (still trash, not rm).
  - Empty (0-byte) files and Google Drive shortcuts are never shown or touched.
  - Dates before 1980 are flagged as unreliable (epoch garbage).
  - Every API call needs the per-launch key, so other web pages can't drive it.
"""
import argparse
import concurrent.futures as cf
import csv
import fnmatch
import hashlib
import io
import json
import os
import secrets
import shutil
import string
import subprocess
import sys
import threading
import time
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

VERSION = '1.3.6'
SANE_EPOCH = 315532800.0  # 1980-01-01
QH_MIN = 8 * 1024 * 1024  # quick-hash prefilter for files >= 8 MB
GSHORTCUTS = ('.gdoc', '.gsheet', '.gslides', '.gdraw', '.gform', '.gmap', '.gsite', '.glink')
JUNK_NAMES = {'desktop.ini', 'thumbs.db', '.ds_store'}
DEFAULT_EXCLUDES = ['.git', 'node_modules', '$RECYCLE.BIN', 'System Volume Information',
                    '*/AppData/Local/Temp/*', '__pycache__']
# Game installs, saves and mods repeat files on purpose: never dedupe them (v1.3.2)
GAME_EXCLUDES = ['steamapps', 'SteamLibrary', 'Steam', 'Epic Games', 'GOG Games', 'GOG Galaxy',
                 'Origin Games', 'EA Games', 'Ubisoft Game Launcher', 'Battle.net', 'Riot Games',
                 'XboxGames', 'WindowsApps', 'My Games', 'Saved Games', '.minecraft', 'Roblox',
                 'Zomboid', 'ProjectZomboid', 'Project Zomboid']
# Skip presets for the "Skip" dropdown (v1.3.3). Each name matches a folder/file name anywhere.
SKIP_PRESETS = {
    'games': ('Games', GAME_EXCLUDES),
    'system': ('Windows system & app data', ['AppData', 'Application Data', 'Local Settings', 'My Documents',
               'Cookies', 'NetHood', 'PrintHood', 'Recent', 'SendTo', 'Start Menu', 'Templates', 'NTUSER*',
               'IntelGraphicsProfiles', 'Windows', 'Program Files', 'Program Files (x86)', 'ProgramData']),
    'hidden': ('Hidden dot folders & files', ['.*']),
    'cloud': ('Sync caches (plumbing)', ['DriveFS', 'drive_cache', '.dropbox.cache', 'iCloudDrive', 'CloudStore', 'Syncthing', '.stfolder', '.stversions']),
    'onedrive': ('OneDrive & WPS Cloud', ['OneDrive', 'WPS Cloud Files']),
    'code': ('Code & build folders', ['.git', 'node_modules', '.venv', 'venv', '__pycache__', 'build', 'dist', 'target']),
}
PRESETS = {
    'all': None,
    'pictures': {'.jpg', '.jpeg', '.png', '.gif', '.bmp', '.heic', '.heif', '.webp', '.raw',
                 '.cr2', '.nef', '.arw', '.dng', '.tif', '.tiff', '.svg', '.ico'},
    'videos': {'.mp4', '.mkv', '.mov', '.avi', '.wmv', '.flv', '.webm', '.m4v', '.3gp',
               '.mts', '.ts', '.mpg', '.mpeg'},
    'music': {'.mp3', '.flac', '.wav', '.m4a', '.aac', '.ogg', '.opus', '.wma', '.aiff', '.mid'},
    'documents': {'.pdf', '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx', '.txt', '.md',
                  '.odt', '.ods', '.odp', '.rtf', '.csv', '.epub'},
    'archives': {'.zip', '.rar', '.7z', '.tar', '.gz', '.bz2', '.xz', '.iso'},
    'programs': {'.exe', '.dll', '.msi', '.sys', '.apk', '.deb', '.rpm', '.appimage'},
}

TOKEN = secrets.token_hex(16)
LOCK = threading.Lock()
STATE = {
    'phase': 'idle',          # idle | scanning | done | error
    'roots': [], 'stage': '', 'done': 0, 'total': 0,
    'bytes_done': 0, 'bytes_total': 0, 'hash_started': 0.0,
    'scanned': 0, 'empty_skipped': 0, 'errors': 0, 'error_paths': [],
    'groups': [], 'singletons': False, 'message': '', 'cancel': False,
    'algo': 'sha256', 'imported': 0, 'cached': 0,
    'job': {},                # background trash/delete run (v1.3)
}


# ------------------------------------------------------------------ helpers
def hash_file(path, algo, chunk=1 << 20):
    h = hashlib.md5() if algo == 'md5' else hashlib.sha256()
    with open(path, 'rb') as f:
        for blk in iter(lambda: f.read(chunk), b''):
            h.update(blk)
    return h.hexdigest()


def trash_root(root):
    return os.path.join(root, '_dedupe_trash')


def log_path(root):
    return os.path.join(root, 'dedupe_log.tsv')


def cache_path(root, algo):
    # sha256 cache is shared with dedupe.sh; md5 gets its own file
    return os.path.join(trash_root(root), 'hash_cache.tsv' if algo == 'sha256' else 'hash_cache_md5.tsv')


def creation_time(st):
    """Real creation time where the OS has one (Item 18: st_ctime on Windows is
    deprecated as 'creation' from Python 3.12; st_birthtime replaces it)."""
    bt = getattr(st, 'st_birthtime', None)
    if bt:
        return bt
    if os.name == 'nt':
        return st.st_ctime
    return 0.0  # Linux: no reliable creation time


def norm(p):
    return os.path.normcase(os.path.normpath(p))


def why(path, e):
    """Plain-language reason a file/folder couldn't be read."""
    if e is None:
        return ''
    win = getattr(e, 'winerror', None)
    no = getattr(e, 'errno', None)
    stand_in = any(0xF000 <= ord(c) <= 0xF0FF for c in str(path))
    if stand_in:
        return "name contains a stand-in for : | ? * (made on Linux/Android); rename it on the device that owns it"
    if win == 32:
        return 'open in another program'
    if win in (5,) or no == 13:
        return 'access denied (protected by Windows or another user)'
    if win in (362, 388, 389, 390, 391, 1004) or no == 22 and 'cloud' in str(e).lower():
        return 'online-only cloud file'
    if len(str(path)) >= 260:
        return 'path over 260 characters'
    if win in (2, 3) or no == 2:
        return 'not found (moved or deleted during the scan)'
    if win == 123:
        return "name Windows can't open"
    return getattr(e, 'strerror', None) or e.__class__.__name__


def note_error(path, e=None):
    with LOCK:
        STATE['errors'] += 1
        if len(STATE['error_paths']) < 1000:
            r = why(path, e)
            STATE['error_paths'].append('%s\t%s' % (path, r) if r else path)


def excluded(path, name, patterns):
    """A pattern with no slash matches a folder/file NAME anywhere; with a slash it
    matches the full path. Case-insensitive, / and \\ treated alike."""
    lp = path.replace('\\', '/').lower()
    ln = name.lower()
    for pat in patterns:
        p = pat.replace('\\', '/').lower().strip()
        if not p:
            continue
        if '/' in p:
            if fnmatch.fnmatch(lp, p) or fnmatch.fnmatch(lp + '/', p):
                return True
        elif fnmatch.fnmatch(ln, p):
            return True
    return False


def load_hash_list(path):
    """Item 10: import hashes instead of computing them. Accepts the DLM inventory
    CSV (MD5,Length,CreationTime,LastWriteTime,FullName) or any CSV with
    md5/sha256 + size/length + path/fullname columns. Returns (algo, {(normpath,size): hash})."""
    out = {}
    algo = None
    with open(path, encoding='utf-8-sig', errors='replace', newline='') as f:
        rd = csv.DictReader(f)
        cols = {c.lower(): c for c in (rd.fieldnames or [])}
        hcol = cols.get('md5') or cols.get('sha256') or cols.get('hash')
        algo = 'sha256' if 'sha256' in cols and 'md5' not in cols else 'md5'
        scol = cols.get('length') or cols.get('size')
        pcol = cols.get('fullname') or cols.get('path')
        if not (hcol and scol and pcol):
            raise ValueError('hash list needs hash, size/length and path/fullname columns')
        for r in rd:
            try:
                out[(norm(r[pcol]), int(r[scol]))] = r[hcol].strip().lower()
            except (ValueError, KeyError, TypeError):
                continue
    return algo, out


# ------------------------------------------------------------------ scanning
def scan_worker(roots, opts):
    try:
        exts = opts.get('exts')
        min_size = max(1, int(opts.get('min_size') or 1))
        match = (opts.get('match') or '').strip().lower()
        include_singletons = bool(opts.get('include_singletons')) and bool(match)
        skip_junk = bool(opts.get('skip_junk')) and not match
        patterns = opts.get('excludes') or []
        algo = opts.get('algo') or 'sha256'
        threads = max(1, min(16, int(opts.get('threads') or 4)))
        imported = opts.get('imported') or {}

        trash_dirs = {norm(trash_root(r)) for r in roots}
        logs = {norm(log_path(r)) for r in roots}
        files = []  # (path, size, st, root)
        seen = set()
        with LOCK:
            STATE['stage'] = 'Listing files'
        for root in roots:
            for dirpath, dirnames, filenames in os.walk(root, onerror=lambda e: note_error(getattr(e, 'filename', '?'), e)):
                if STATE['cancel']:
                    raise InterruptedError
                dirnames[:] = [d for d in dirnames
                               if norm(os.path.join(dirpath, d)) not in trash_dirs
                               and not excluded(os.path.join(dirpath, d), d, patterns)]
                for name in filenames:
                    p = os.path.join(dirpath, name)
                    np_ = norm(p)
                    if np_ in logs or np_ in seen:
                        continue  # overlapping roots: count each file once
                    low = name.lower()
                    if low.endswith(GSHORTCUTS):
                        continue
                    if skip_junk and low in JUNK_NAMES:
                        continue
                    if patterns and excluded(p, name, patterns):
                        continue
                    try:
                        st = os.stat(p)
                    except OSError as e:
                        note_error(p, e)
                        continue
                    with LOCK:
                        STATE['scanned'] += 1
                    if st.st_size == 0:
                        with LOCK:
                            STATE['empty_skipped'] += 1
                        continue
                    if st.st_size < min_size:
                        continue
                    if exts is not None and os.path.splitext(low)[1] not in exts:
                        continue
                    if match and not fnmatch.fnmatch(low, match):
                        continue
                    seen.add(np_)
                    files.append((p, st.st_size, st, root))

        # size prefilter
        by_size = {}
        for rec in files:
            by_size.setdefault(rec[1], []).append(rec)
        cands = [r for recs in by_size.values() if len(recs) > 1 or include_singletons for r in recs]

        # caches (per root) + imported list
        cache = {}
        for root in roots:
            try:
                with open(cache_path(root, algo), encoding='utf-8') as cfh:
                    for line in cfh:
                        parts = line.rstrip('\n').split('\t')
                        if len(parts) == 4:
                            cache['|'.join(parts[:3])] = parts[3]
            except OSError:
                pass

        def known(p, size, st):
            h = cache.get('%s|%d|%d' % (p, size, int(st.st_mtime)))
            if h:
                return h, 'cache'
            h = imported.get((norm(p), size))
            if h:
                return h, 'import'
            return None, None

        # quick-hash prefilter: unique first 64 KB within a size group => not a dup
        with LOCK:
            STATE['stage'] = 'Quick-checking large files'
        qh_groups, kept = {}, []
        for rec in cands:
            p, size, st, root = rec
            if size < QH_MIN or known(p, size, st)[0]:
                kept.append(rec)
                continue
            if STATE['cancel']:
                raise InterruptedError
            try:
                with open(p, 'rb') as f:
                    qh = hashlib.sha256(f.read(65536)).hexdigest()[:16]
                qh_groups.setdefault((size, qh), []).append(rec)
            except OSError as e:
                note_error(p, e)
        for recs in qh_groups.values():
            if len(recs) > 1 or include_singletons:
                kept.extend(recs)
        cands = kept

        with LOCK:
            STATE.update(stage='Hashing', total=len(cands), done=0, bytes_done=0,
                         bytes_total=sum(r[1] for r in cands), hash_started=time.time())

        results = [None] * len(cands)
        new_lines = {}  # root -> lines
        todo = []
        n_cached = n_imported = 0
        for i, (p, size, st, root) in enumerate(cands):
            h, src = known(p, size, st)
            if h:
                results[i] = h
                if src == 'cache':
                    n_cached += 1
                else:
                    n_imported += 1
                with LOCK:
                    STATE['done'] += 1
                    STATE['bytes_done'] += size
            else:
                todo.append(i)
        with LOCK:
            STATE['cached'], STATE['imported'] = n_cached, n_imported

        def work(i):
            if STATE['cancel']:
                return i, None
            p, size, st, root = cands[i]
            try:
                return i, hash_file(p, algo)
            except OSError as e:
                note_error(p, e)
                return i, None
            finally:
                with LOCK:
                    STATE['done'] += 1
                    STATE['bytes_done'] += size

        # Item 9: parallel hashing (set threads=1 for a single spinning disk)
        with cf.ThreadPoolExecutor(max_workers=threads) as ex:
            for i, h in ex.map(work, todo):
                if h:
                    results[i] = h
                    p, size, st, root = cands[i]
                    if '\t' not in p and '\n' not in p:
                        new_lines.setdefault(root, []).append(
                            '%s\t%d\t%d\t%s\n' % (p, size, int(st.st_mtime), h))
        # save cache even after cancel: hashing done so far is never wasted
        for root, lines in new_lines.items():
            try:
                os.makedirs(trash_root(root), exist_ok=True)
                with open(cache_path(root, algo), 'a', encoding='utf-8') as cfh:
                    cfh.writelines(lines)
            except OSError:
                pass
        if STATE['cancel']:
            raise InterruptedError

        by_hash = {}
        for i, (p, size, st, root) in enumerate(cands):
            h = results[i]
            if not h:
                continue
            mt = st.st_mtime
            by_hash.setdefault((size, h), []).append({
                'path': p, 'root': root, 'rel': os.path.relpath(p, root),
                'name': os.path.basename(p), 'size': size,
                'mtime': mt, 'ctime': creation_time(st),
                'suspect': mt < SANE_EPOCH,
            })
        groups = []
        for (size, h), members in by_hash.items():
            if len(members) < 2 and not include_singletons:
                continue
            members.sort(key=lambda m: (m['suspect'], m['mtime'], m['path']))
            groups.append({'hash': h, 'size': size, 'count': len(members),
                           'wasted': size * (len(members) - 1), 'files': members})
        groups.sort(key=lambda g: (-g['wasted'], g['hash']))
        for i, g in enumerate(groups):
            g['id'] = i + 1

        with LOCK:
            STATE.update(phase='done', groups=groups, stage='', singletons=include_singletons,
                         message='Scan complete.')
    except InterruptedError:
        with LOCK:
            STATE.update(phase='idle', groups=[], stage='',
                         message='Scan cancelled. Hashes computed so far were saved to the cache.')
    except Exception as e:  # noqa: BLE001
        with LOCK:
            STATE.update(phase='error', stage='', message='Scan failed: %s' % e)


# ------------------------------------------------------------------ moves
def lp(p):
    """Windows long paths: the trash adds about 30 characters, which pushed deep files
    (browser profiles, app backups) past 260 and failed with WinError 3. The \\?\
    prefix lifts that limit without any registry change. No-op elsewhere."""
    if os.name != 'nt' or p.startswith('\\\\?\\') or len(p) < 240:
        return p
    p = os.path.abspath(p)
    if p.startswith('\\\\'):          # network share \\server\share
        return '\\\\?\\UNC\\' + p[2:]
    return '\\\\?\\' + p


def move_file(src, dest):
    src, dest = lp(src), lp(dest)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    try:
        os.rename(src, dest)
    except OSError:
        shutil.move(src, dest)  # cross-volume fallback (copy2 keeps mtime)


def open_log(root):
    lp = log_path(root)
    new = not os.path.exists(lp)
    lf = open(lp, 'a', encoding='utf-8', newline='')
    if new:
        lf.write('time\thash\toriginal\tkept\ttrashed_to\n')
    return lf


HISTORY = os.path.join(os.path.expanduser('~'), 'DedupeStudio', 'history.tsv')


def history_write(rows):
    """Master log across every root and run: time, action, hash, original, kept, destination."""
    try:
        os.makedirs(os.path.dirname(HISTORY), exist_ok=True)
        new = not os.path.exists(HISTORY)
        with open(HISTORY, 'a', encoding='utf-8', newline='') as hf:
            if new:
                hf.write('time\taction\thash\toriginal\tkept\tdestination\n')
            hf.writelines(rows)
    except OSError:
        pass


def start_trash(paths, mode, action):
    """Validate on the request thread, then move/delete in the background so the UI can
    show progress (v1.2 ran it inside one HTTP request: on slow drives nothing visible
    happened for minutes)."""
    with LOCK:
        if STATE['job'].get('running'):
            return {'ok': False, 'error': 'A trash/delete run is already in progress'}
    chk = do_trash(paths, mode, action, check_only=True)
    if not chk.get('ok'):
        return chk
    with LOCK:
        STATE['cancel'] = False
        STATE['job'] = {'running': True, 'action': action, 'done': 0, 'total': len(set(paths)),
                        'moved': 0, 'bytes': 0, 'errors': [], 'result': None}
    threading.Thread(target=_trash_worker, args=(paths, mode, action), daemon=True).start()
    return {'ok': True, 'started': True}


def _trash_worker(paths, mode, action):
    try:
        res = do_trash(paths, mode, action)
    except Exception as e:  # noqa: BLE001
        res = {'ok': False, 'error': str(e)}
    with LOCK:
        STATE['job']['running'] = False
        STATE['job']['result'] = res


def do_trash(paths, mode, action='trash', check_only=False):
    sel = set(paths)
    with LOCK:
        groups = STATE['groups']
        singles_ok = STATE['singletons']
    by_path = {}
    for g in groups:
        for f in g['files']:
            by_path[f['path']] = (g, f)
    unknown = [p for p in sel if p not in by_path]
    if unknown:
        return {'ok': False, 'error': 'Not in current scan: %s' % unknown[0]}
    if mode != 'destructive' and not singles_ok:
        for g in groups:
            gp = {f['path'] for f in g['files']}
            if gp and gp <= sel:
                return {'ok': False, 'error': 'Careful mode: set %s would lose ALL copies. '
                        'Deselect one, or switch to Destructive mode.' % g['hash'][:12]}
    if check_only:
        return {'ok': True}
    delete = action == 'delete'
    hist = []
    stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    logs, run_dirs, done = {}, set(), set()
    moved, bytes_moved, errors = 0, 0, []
    try:
        for p in sorted(sel):
            if STATE['cancel']:
                errors.append('Cancelled - the remaining files were left untouched')
                break
            g, f = by_path[p]
            root = f['root']
            kept = next((x['path'] for x in g['files'] if x['path'] not in sel), '-')
            run_dir = os.path.join(trash_root(root), stamp)
            if delete:
                dest = 'DELETED'
            else:
                dest = os.path.join(run_dir, f['rel'])
                if os.path.exists(dest):
                    dest = dest + '.' + str(time.time_ns())
            try:
                if delete:
                    os.remove(lp(p))
                else:
                    move_file(p, dest)
            except Exception as e:  # noqa: BLE001
                errors.append('%s: %s' % (p, e))
                with LOCK:
                    STATE['job']['done'] += 1
                    STATE['job']['errors'] = errors[-200:]
                continue
            hist.append('%s\t%s\t%s\t%s\t%s\t%s\n' % (stamp, action, g['hash'], p, kept, dest))
            if root not in logs:
                logs[root] = open_log(root)
            logs[root].write('%s\t%s\t%s\t%s\t%s\n' % (stamp, g['hash'], p, kept, dest))
            logs[root].flush()  # a crash can never lose an undo row
            if not delete:
                run_dirs.add(run_dir)
            done.add(p)
            moved += 1
            bytes_moved += g['size']
            with LOCK:
                STATE['job'].update(done=STATE['job'].get('done', 0) + 1, moved=moved, bytes=bytes_moved)
    finally:
        for lf in logs.values():
            lf.close()
        history_write(hist)
    with LOCK:
        minc = 1 if STATE['singletons'] else 2
        left = []
        for g in STATE['groups']:
            g['files'] = [x for x in g['files'] if x['path'] not in done]
            g['count'] = len(g['files'])
            g['wasted'] = g['size'] * max(0, g['count'] - 1)
            if g['count'] >= minc:
                left.append(g)
        STATE['groups'] = left
    return {'ok': True, 'moved': moved, 'bytes': bytes_moved, 'errors': errors, 'action': action,
            'trash': ' + '.join(sorted(run_dirs)) or ('permanently deleted' if delete else '-'),
            'history': HISTORY, 'groups': left}


def read_log(root):
    rows = []
    try:
        with open(log_path(root), encoding='utf-8') as lf:
            for line in lf:
                r = line.rstrip('\r\n').split('\t')
                if len(r) == 5 and r[0] != 'time':
                    rows.append(r)
    except OSError:
        pass
    return rows


def do_runs(roots):
    runs = {}
    for root in roots:
        for stamp, _, _, _, dest in read_log(root):
            r = runs.setdefault(stamp, {'run': stamp, 'count': 0, 'restorable': 0})
            r['count'] += 1
            if os.path.exists(dest):
                r['restorable'] += 1
    return {'ok': True, 'runs': [runs[k] for k in sorted(runs)]}


def do_undo(roots, run=''):
    restored = skipped = 0
    for root in roots:
        for stamp, _, orig, _, dest in reversed(read_log(root)):
            if run and stamp != run:
                continue
            if not os.path.exists(dest):
                continue
            if os.path.exists(orig):
                skipped += 1
                continue
            try:
                move_file(dest, orig)
                restored += 1
            except Exception:  # noqa: BLE001
                skipped += 1
    return {'ok': True, 'restored': restored, 'skipped': skipped}


def empty_dirs(roots, junk=False):
    out = []
    for root in roots:
        tdir = norm(trash_root(root))
        for dirpath, dirnames, filenames in os.walk(root, topdown=False):
            if norm(dirpath) == norm(root) or norm(dirpath).startswith(tdir):
                continue
            try:
                ents = os.listdir(dirpath)
            except OSError:
                continue
            if all(junk and e.lower() in JUNK_NAMES and os.path.isfile(os.path.join(dirpath, e))
                   for e in ents):
                out.append(dirpath)
    return out


def prune_empty(roots, junk=False):
    removed = failed = junk_moved = 0
    stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    root_of = lambda d: next((r for r in roots if norm(d).startswith(norm(r))), roots[0])
    logs = {}
    try:
        for _ in range(64):
            batch = empty_dirs(roots, junk)
            got = 0
            failed = 0
            for d in batch:
                if junk:
                    root = root_of(d)
                    for e in os.listdir(d):
                        src = os.path.join(d, e)
                        dest = os.path.join(trash_root(root), stamp, os.path.relpath(src, root))
                        try:
                            move_file(src, dest)
                        except Exception:  # noqa: BLE001
                            continue
                        if root not in logs:
                            logs[root] = open_log(root)
                        logs[root].write('%s\t-\t%s\t-\t%s\n' % (stamp, src, dest))
                        logs[root].flush()
                        junk_moved += 1
                try:
                    os.rmdir(d)
                    removed += 1
                    got += 1
                except OSError:
                    failed += 1  # locked / Android-style refusals: skip, not fatal
            if got == 0:
                break
    finally:
        for lf in logs.values():
            lf.close()
    return {'ok': True, 'removed': removed, 'failed': failed, 'junk': junk_moved}


def export_csv():
    """Item 12: every duplicate set as CSV rows (one row per copy)."""
    with LOCK:
        groups = STATE['groups']
        algo = STATE['algo']
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(['set_id', 'copies', algo, 'size', 'created', 'modified', 'path'])
    iso = lambda t: datetime.fromtimestamp(t).strftime('%Y-%m-%d %H:%M:%S') if t else ''
    for g in groups:
        for f in g['files']:
            w.writerow([g['id'], g['count'], g['hash'], g['size'], iso(f['ctime']), iso(f['mtime']), f['path']])
    return buf.getvalue()


# ------------------------------------------------------------------ folder picking
def pick_folder(start):
    """Native folder dialog.
    Desktop app (pywebview loaded in this process): use the window's own dialog.
    Plain script: tkinter in a child Python process. Never launch sys.executable when
    frozen - in a PyInstaller build that is this app, not Python (v1.2 bug: every
    "Add folder" opened a second copy of the app)."""
    wv = sys.modules.get('webview')
    if wv is not None and getattr(wv, 'windows', None):
        try:
            kind = getattr(getattr(wv, 'FileDialog', None), 'FOLDER', None)
            if kind is None:
                kind = wv.FOLDER_DIALOG
            res = wv.windows[0].create_file_dialog(kind, directory=start if os.path.isdir(start or '') else '')
        except Exception as e:  # noqa: BLE001
            return {'ok': False, 'error': 'folder dialog failed: %s' % e}
        if not res:
            return {'ok': False, 'cancelled': True}
        path = res[0] if isinstance(res, (list, tuple)) else res
        return {'ok': True, 'path': os.path.normpath(path)}
    if getattr(sys, 'frozen', False):
        return {'ok': False, 'error': 'no native folder picker available'}
    code = ('import tkinter as t, tkinter.filedialog as f, sys\n'
            'r=t.Tk(); r.withdraw(); r.attributes("-topmost", True)\n'
            'p=f.askdirectory(initialdir=sys.argv[1] or None, title="Pick a folder to scan")\n'
            'sys.stdout.write(p or "::CANCEL::")\n')
    try:
        out = subprocess.run([sys.executable, '-c', code, start or ''], capture_output=True,
                             text=True, timeout=300).stdout.strip()
    except Exception as e:  # noqa: BLE001
        return {'ok': False, 'error': str(e)}
    if out == '::CANCEL::':
        return {'ok': False, 'cancelled': True}
    if out and os.path.isdir(out):
        return {'ok': True, 'path': os.path.normpath(out)}
    return {'ok': False, 'error': 'no native folder picker available'}


def list_dir(path):
    if not path:
        if os.name == 'nt':
            drives = ['%s:\\' % d for d in string.ascii_uppercase if os.path.exists('%s:\\' % d)]
            return {'ok': True, 'path': '', 'parent': None, 'dirs': drives}
        path = '/'
    path = os.path.abspath(os.path.expanduser(path))
    if not os.path.isdir(path):
        return {'ok': False, 'error': 'Not a folder: %s' % path}
    try:
        entries = sorted((e for e in os.scandir(path) if e.is_dir(follow_symlinks=False)),
                         key=lambda e: e.name.lower())
    except OSError as e:
        return {'ok': False, 'error': str(e)}
    parent = os.path.dirname(path)
    if parent == path:
        parent = '' if os.name == 'nt' else None
    return {'ok': True, 'path': path, 'parent': parent,
            'dirs': [e.path for e in entries if not e.name.startswith('$')]}


# ------------------------------------------------------------------ HTTP
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, body, ctype, code=200, extra=None):
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(json.dumps(obj).encode('utf-8'), 'application/json', code)

    def _host_ok(self):
        port = self.server.server_address[1]
        return self.headers.get('Host') in ('127.0.0.1:%d' % port, 'localhost:%d' % port)

    def _authed(self):
        # Item 17: per-launch key in a custom header. Browsers can't add custom
        # headers to cross-site requests without a CORS preflight we never answer.
        return self._host_ok() and secrets.compare_digest(self.headers.get('X-Token', ''), TOKEN)

    def do_GET(self):
        url = urlparse(self.path)
        if not self._host_ok():
            return self._send(b'forbidden', 'text/plain', 403)
        if url.path == '/':
            return self._send(INDEX_HTML.replace('%%VERSION%%', VERSION).encode('utf-8'), 'text/html; charset=utf-8')
        if url.path == '/api/export':  # a download link can't send headers: key in the query
            if parse_qs(url.query).get('t', [''])[0] != TOKEN:
                return self._send(b'forbidden', 'text/plain', 403)
            name = 'dedupe_sets_%s.csv' % datetime.now().strftime('%Y%m%d_%H%M%S')
            return self._send(export_csv().encode('utf-8-sig'), 'text/csv; charset=utf-8', 200,
                              {'Content-Disposition': 'attachment; filename="%s"' % name})
        if not self._authed():
            return self._send(b'forbidden', 'text/plain', 403)
        if url.path == '/api/state':
            with LOCK:
                s = {k: STATE[k] for k in ('phase', 'roots', 'stage', 'done', 'total', 'scanned',
                                           'empty_skipped', 'errors', 'message', 'singletons',
                                           'bytes_done', 'bytes_total', 'algo', 'cached', 'imported')}
                s['elapsed'] = time.time() - STATE['hash_started'] if STATE['hash_started'] else 0
            s['version'] = VERSION
            s['default_excludes'] = DEFAULT_EXCLUDES
            return self._json(s)
        if url.path == '/api/groups':
            with LOCK:
                body = json.dumps({'groups': STATE['groups'], 'singletons': STATE['singletons']})
            return self._send(body.encode('utf-8'), 'application/json')
        if url.path == '/api/job':
            with LOCK:
                return self._json(dict(STATE['job']))
        if url.path == '/api/errors':
            with LOCK:
                return self._json({'ok': True, 'count': STATE['errors'], 'paths': list(STATE['error_paths'])})
        return self._json({'ok': False, 'error': 'not found'}, 404)

    def do_POST(self):
        if not self._authed():
            return self._send(b'forbidden', 'text/plain', 403)
        try:
            n = int(self.headers.get('Content-Length') or 0)
            req = json.loads(self.rfile.read(n) or b'{}')
        except Exception:  # noqa: BLE001
            return self._json({'ok': False, 'error': 'bad json'}, 400)
        route = urlparse(self.path).path
        with LOCK:
            roots = list(STATE['roots'])
        if route == '/api/browse':
            return self._json(list_dir(req.get('path', '')))
        if route == '/api/pick':
            return self._json(pick_folder(req.get('start', '')))
        if route == '/api/scan':
            # Item 7: several roots in one scan, separated by ; or new lines
            raw = req.get('roots') or req.get('root') or ''
            parts = [x.strip().strip('"') for x in raw.replace('\n', ';').split(';') if x.strip()]
            new_roots = []
            for x in parts:
                r = os.path.abspath(os.path.expanduser(x))
                if not os.path.isdir(r):
                    return self._json({'ok': False, 'error': 'Not a folder: %s' % r})
                if norm(r) not in {norm(y) for y in new_roots}:
                    new_roots.append(r)
            if not new_roots:
                return self._json({'ok': False, 'error': 'Type or browse a folder first'})
            imported, algo = {}, req.get('algo') if req.get('algo') in ('md5', 'sha256') else 'sha256'
            hl = (req.get('hash_list') or '').strip().strip('"')
            if hl:
                try:
                    list_algo, imported = load_hash_list(hl)
                except Exception as e:  # noqa: BLE001
                    return self._json({'ok': False, 'error': 'Hash list: %s' % e})
                algo = list_algo  # the list decides the algorithm, so hashes are comparable
            preset = req.get('type', 'all')
            exts = PRESETS.get(preset)
            custom = (req.get('exts') or '').strip()
            if preset == 'custom' and custom:
                exts = {('.' + e.strip().lstrip('.').lower()) for e in custom.split(',') if e.strip()}
            excludes = [x.strip() for x in (req.get('excludes') or '').replace('\n', ';').split(';') if x.strip()]
            skips = req.get('skip_presets')
            if skips is None:  # older callers: games on unless turned off
                skips = ['games'] if req.get('skip_games', True) else []
            for k in skips:
                if k in SKIP_PRESETS:
                    excludes += SKIP_PRESETS[k][1]
            with LOCK:
                if STATE['phase'] == 'scanning':
                    return self._json({'ok': False, 'error': 'Scan already running'})
                STATE.update(phase='scanning', roots=new_roots, stage='Starting', done=0, total=0,
                             bytes_done=0, bytes_total=0, hash_started=0.0, scanned=0,
                             empty_skipped=0, errors=0, error_paths=[], groups=[], message='',
                             cancel=False, algo=algo, cached=0, imported=0)
            opts = {'exts': exts, 'min_size': req.get('min_size') or 1, 'match': req.get('match') or '',
                    'include_singletons': req.get('include_singletons'), 'excludes': excludes,
                    'skip_junk': req.get('skip_junk', True), 'algo': algo,
                    'threads': req.get('threads') or 4, 'imported': imported}
            threading.Thread(target=scan_worker, args=(new_roots, opts), daemon=True).start()
            return self._json({'ok': True, 'roots': new_roots, 'algo': algo})
        if route == '/api/cancel':
            with LOCK:
                STATE['cancel'] = True
            return self._json({'ok': True})
        if route == '/api/trash':
            with LOCK:
                if STATE['phase'] != 'done':
                    return self._json({'ok': False, 'error': 'Scan first'})
            action = 'delete' if req.get('action') == 'delete' else 'trash'
            return self._json(start_trash(req.get('paths') or [], req.get('mode', 'careful'), action))
        if not roots:
            return self._json({'ok': False, 'error': 'Pick a folder and scan first'})
        if route == '/api/runs':
            return self._json(do_runs(roots))
        if route == '/api/undo':
            return self._json(do_undo(roots, req.get('run', '')))
        if route == '/api/empty':
            return self._json({'ok': True, 'dirs': empty_dirs(roots, bool(req.get('junk')))})
        if route == '/api/prune':
            return self._json(prune_empty(roots, bool(req.get('junk'))))
        return self._json({'ok': False, 'error': 'not found'}, 404)


# ------------------------------------------------------------------ UI
INDEX_HTML = r"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Dedupe Studio</title>
<style>
:root{--bg:#121214;--panel:#1c1c20;--panel2:#242429;--line:#34343c;--tx:#e8e8ec;
--dim:#9a9aa6;--acc:#d9a7f0;--acc2:#8e5bd4;--danger:#e26d6d;--ok:#6dc78f;--warn:#e2b96d}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--tx);
font:14px/1.45 "Segoe UI",system-ui,sans-serif}
header{background:var(--panel);border-bottom:1px solid var(--line);position:sticky;top:0;z-index:5}
.hrow{display:flex;flex-wrap:wrap;gap:8px;align-items:center;padding:8px 14px}
h1{font-size:16px;margin:0 10px 0 0;color:var(--acc)}
h1 small{color:var(--dim);font-weight:400;font-size:11px;margin-left:4px}
input,select,button,textarea{background:var(--panel2);color:var(--tx);border:1px solid var(--line);
border-radius:6px;padding:6px 9px;font:inherit}
input:focus,select:focus,textarea:focus{outline:1px solid var(--acc2)}
button{cursor:pointer}button:hover{border-color:var(--acc2)}
button.primary{background:var(--acc);color:#1b0f24;font-weight:600;border:none}
button.danger{background:var(--danger);color:#fff;border:none;font-weight:600}
button:disabled{opacity:.45;cursor:default}
#path{min-width:320px;flex:1}
#opts{display:none;border-top:1px solid var(--line)}
#opts label{color:var(--dim);font-size:12px;display:flex;align-items:center;gap:6px}
#toolbar{display:none;flex-wrap:wrap;gap:8px;align-items:center;padding:8px 14px;
background:var(--panel);border-bottom:1px solid var(--line);position:sticky;top:0;z-index:4}
#stats{color:var(--dim);margin-left:auto;white-space:nowrap}
#stats b{color:var(--tx)}
.modeWrap{display:flex;border:1px solid var(--line);border-radius:6px;overflow:hidden}
.modeWrap button{border:none;border-radius:0;padding:6px 10px;background:var(--panel2)}
.modeWrap button.on.careful{background:var(--ok);color:#0e2416;font-weight:700}
.modeWrap button.on.destructive{background:var(--danger);color:#fff;font-weight:700}
#progress{display:none;padding:12px 16px}
.bar{height:8px;background:var(--panel2);border-radius:5px;overflow:hidden;margin-top:6px}
.bar>div{height:100%;background:var(--acc);width:0;transition:width .25s}
main{padding:12px 14px;max-width:1600px}
.group{background:var(--panel);border:1px solid var(--line);border-radius:10px;margin-bottom:12px;overflow:hidden}
.ghead{display:flex;flex-wrap:wrap;gap:10px;align-items:center;padding:8px 12px;
background:var(--panel2);border-bottom:1px solid var(--line)}
.ghead .hash{font-family:Consolas,monospace;color:var(--dim);font-size:12px}
.badge{background:var(--acc2);color:#fff;border-radius:10px;padding:1px 9px;font-size:12px}
.gsize{color:var(--dim);font-size:12px}
.gctl{margin-left:auto;display:flex;gap:6px;align-items:center}
.gctl select,.gctl button{padding:3px 8px;font-size:12px}
.row{display:flex;gap:10px;align-items:center;padding:6px 12px;border-bottom:1px solid #26262c;cursor:pointer}
.row:last-child{border-bottom:none}
.row:hover{background:#202026}
.row.sel{background:#2b2234}
.row input{accent-color:var(--acc);pointer-events:none}
.fname{font-weight:600}
.fdir{color:var(--dim);font-size:12px;word-break:break-all}
.fmeta{margin-left:auto;text-align:right;color:var(--dim);font-size:12px;white-space:nowrap}
.fmeta span.lbl{display:inline-block;width:56px;text-align:left;opacity:.7}
.sus{color:var(--warn)}
.tag{font-size:11px;border:1px solid var(--line);border-radius:8px;padding:0 6px;color:var(--dim);margin-left:6px}
#toast{position:fixed;bottom:18px;left:50%;transform:translateX(-50%);
background:var(--panel2);border:1px solid var(--acc2);padding:10px 18px;border-radius:8px;
display:none;max-width:70%;z-index:20}
.modal{position:fixed;inset:0;background:#000a;display:none;z-index:10;align-items:center;justify-content:center}
.modal .box{background:var(--panel);border:1px solid var(--line);border-radius:10px;
width:min(760px,92vw);max-height:80vh;display:flex;flex-direction:column;padding:14px}
.modal .list{overflow:auto;margin:10px 0;border:1px solid var(--line);border-radius:6px}
.modal .list div{padding:6px 10px;border-bottom:1px solid #26262c;word-break:break-all}
.modal .list div.click{cursor:pointer}
.modal .list div.click:hover{background:var(--panel2)}
.runrow{display:flex;align-items:center;gap:10px}
.runrow span{flex:1}
#empty{color:var(--dim);text-align:center;padding:40px}
.hint{color:var(--dim);font-size:12px}
#more{width:100%;padding:10px;margin-bottom:30px}
a{color:var(--acc)}
.skipdd{position:relative}.skipdd summary{cursor:pointer;list-style:none;padding:4px 10px;border:1px solid var(--line);border-radius:6px;font-size:12px;color:var(--dim)}
.skipdd .skipbox{position:absolute;z-index:20;top:110%;left:0;background:var(--panel2);border:1px solid var(--line);border-radius:8px;padding:8px 10px;display:flex;flex-direction:column;gap:6px;min-width:240px}
body.dropping{outline:3px dashed var(--acc);outline-offset:-8px}
</style></head><body>
<header>
 <div class="hrow">
  <h1>Dedupe Studio<small>v%%VERSION%%</small></h1>
  <input id="path" placeholder="Drop folders here, or type them (several: separate with ;) e.g. C:\Users\Admin\Inbox; J:\EverythingFolder">
  <button onclick="openBrowse()" title="Add a folder">Add folder…</button>
  <select id="type">
    <option value="all">All files</option><option value="pictures">Pictures</option>
    <option value="videos">Videos</option><option value="music">Music</option>
    <option value="documents">Documents</option><option value="archives">Archives</option>
    <option value="programs">Programs</option><option value="custom">Custom…</option>
  </select>
  <input id="exts" placeholder=".psd,.kra" style="display:none;width:110px">
  <input id="nmatch" placeholder="name filter e.g. desktop.ini" title="Only scan files whose name matches (wildcards ok)" style="width:170px">
  <button onclick="toggleOpts()">Options ▾</button>
  <button class="primary" id="scanBtn" onclick="startScan()">Scan</button>
 </div>
 <div class="hrow" id="opts">
  <label>Exclude <input id="excl" style="width:340px" title="Folder or file names, or path globs with / . Separate with ;"></label>
  <label>Min size <input id="minsz" type="number" min="0" value="0" style="width:64px">
   <select id="minu"><option value="1">B</option><option value="1024">KB</option><option value="1048576" selected>MB</option></select></label>
  <label>Hash <select id="algo"><option value="sha256">SHA-256</option><option value="md5">MD5 (matches rclone / Drive / DLM CSV)</option></select></label>
  <label>Threads <select id="threads" title="1 is fastest on a single spinning/external disk"><option>1</option><option>2</option><option selected>4</option><option>8</option></select></label>
  <label><input type="checkbox" id="skipjunk" checked> Ignore desktop.ini / Thumbs.db</label>
  <details class="skipdd" id="skipdd"><summary id="skipsum">Skip: Games, Windows, Hidden, Sync, Code</summary><div class="skipbox">
    <label title="Steam, Epic, GOG, Xbox, Minecraft, Roblox, Project Zomboid…: their files repeat on purpose"><input type="checkbox" class="skp" value="games" checked onchange="skipSum()"> Games</label>
    <label title="AppData, Start Menu, NTUSER, Program Files…"><input type="checkbox" class="skp" value="system" checked onchange="skipSum()"> Windows system &amp; app data</label>
    <label title=".claude, .ssh, .bash_history…"><input type="checkbox" class="skp" value="hidden" checked onchange="skipSum()"> Hidden dot folders &amp; files</label>
    <label title="Google Drive cache, Dropbox cache, iCloud, Syncthing: apps manage these files, never touch them"><input type="checkbox" class="skp" value="cloud" checked onchange="skipSum()"> Sync caches (plumbing)</label>
    <label title="OneDrive and WPS Cloud files are your own documents: safe to dedupe"><input type="checkbox" class="skp" value="onedrive" onchange="skipSum()"> OneDrive &amp; WPS Cloud</label>
    <label title=".git, node_modules, .venv, build, dist…"><input type="checkbox" class="skp" value="code" checked onchange="skipSum()"> Code &amp; build folders</label>
  </div></details>
  <label>Hash list <input id="hlist" style="width:260px" placeholder="optional: path to dlm_windows_md5.csv" title="Reuse hashes from a CSV (MD5,Length,…,FullName) instead of hashing"></label>
 </div>
</header>

<div id="toolbar">
  <input id="search" placeholder="Filter sets… (name or path)" style="width:200px" oninput="page=0;render()">
  <select id="strategy" title="Which copies auto-select KEEPS">
    <option value="dlm" selected>Keep oldest (earliest of created/modified)</option>
    <option value="oldest">Keep oldest (modified)</option>
    <option value="oldcreated">Keep oldest (created)</option>
    <option value="newest">Keep newest</option>
    <option value="shortname">Keep shortest name</option><option value="longname">Keep longest name</option>
    <option value="cleanname">Keep cleanest name</option>
  </select>
  <label class="hint">Keep <select id="keepN" title="DLM: same-folder copies → 1, copies in different folders → 2">
    <option value="dlm" selected>DLM (1 same-folder / 2 cross-folder)</option>
    <option>1</option><option>2</option><option>3</option><option value="0">0 (Destructive)</option></select></label>
  <button onclick="autoSelect()">Auto-select shown</button>
  <button onclick="clearSel()">Clear</button>
  <span class="modeWrap">
    <button id="mCare" class="careful on" onclick="setMode('careful')" title="At least one copy of every set survives; files go to a trash folder">Careful</button>
    <button id="mDest" class="destructive" onclick="setMode('destructive')" title="Allows selecting every copy in a set (still moved to trash, never hard-deleted)">Destructive</button>
  </span>
  <button class="danger" id="trashBtn" onclick="trashSel()" disabled>Trash selected</button>
  <label class="hint" title="Power-user option for SSH / network drives where a trash folder is slow or impossible. Permanent: no undo."><input type="checkbox" id="hardDel" onchange="render()"> Delete permanently</label>
  <button onclick="openUndo()">Undo…</button>
  <button onclick="openEmpty()">Empty folders…</button>
  <button onclick="exportCsv()">Export CSV</button>
  <span id="stats"></span>
</div>

<div id="progress"><span id="pstage"></span>
  <button onclick="cancelScan()" style="float:right;padding:2px 10px">Cancel</button>
  <div class="bar"><div id="pbar"></div></div></div>

<div id="guide" style="display:none;margin:10px 16px;padding:12px 14px;border:1px solid var(--line);border-radius:8px;background:var(--panel2);font-size:13px;line-height:1.5">
  <b>First run? Three safety steps.</b><br>
  1. Test on a <b>small folder</b> first (a Downloads subfolder, a copy of some photos).<br>
  2. After <b>Auto-select</b>, <b>scroll through the results</b> and check what is ticked before you press Trash.<br>
  3. Trash <b>moves</b> files to a <code>_dedupe_trash</code> folder inside the scanned folder; nothing is deleted. Keep it about <b>2 weeks</b>; if nothing is missing, delete that folder yourself. <b>Undo…</b> puts files back.<br>
  Game, Windows, hidden, sync-cache and code folders are skipped by default (Options → Skip).
  <div style="margin-top:8px"><button onclick="hideGuide()">Got it</button></div></div>
<main><div id="empty">Pick one or more folders and press <b>Scan</b>.</div>
<div id="groups"></div><button id="more" style="display:none" onclick="page++;render()">Show more sets</button></main>

<div id="toast"></div>
<div class="modal" id="browseM"><div class="box">
  <b>Pick a folder</b><div id="bpath" class="hint"></div>
  <div class="list" id="blist"></div>
  <div style="display:flex;gap:8px;justify-content:flex-end">
    <button onclick="closeM('browseM')">Cancel</button>
    <button class="primary" onclick="chooseBrowse()">Add this folder</button>
  </div></div></div>
<div class="modal" id="emptyM"><div class="box">
  <b>Empty folders</b><div id="ecount" class="hint"></div>
  <label class="hint" style="margin-top:6px"><input type="checkbox" id="ejunk" onchange="openEmpty()">
    Treat folders holding only desktop.ini / Thumbs.db / .DS_Store as empty (those files go to trash, undoable)</label>
  <div class="list" id="elist"></div>
  <div style="display:flex;gap:8px;justify-content:flex-end">
    <button onclick="closeM('emptyM')">Close</button>
    <button class="danger" onclick="prune()">Remove all (error-tolerant)</button>
  </div></div></div>
<div class="modal" id="undoM"><div class="box">
  <b>Undo</b><div class="hint">Each run = one press of “Trash selected” (or one dedupe.sh --apply). Files return to their original place.</div>
  <div class="list" id="ulist"></div>
  <div style="display:flex;gap:8px;justify-content:flex-end">
    <button onclick="closeM('undoM')">Close</button>
    <button class="danger" onclick="undoRun('')">Undo ALL runs</button>
  </div></div></div>
<div class="modal" id="errM"><div class="box">
  <b>Unreadable files</b><div id="errcount" class="hint"></div>
  <div class="list" id="errlist"></div>
  <div style="display:flex;gap:8px;justify-content:flex-end"><button onclick="closeM('errM')">Close</button></div>
</div></div>

<script>
const TOKEN=new URLSearchParams(location.search).get('t')||'';
const SANE=315532800;
let groups=[],sel=new Set(),mode='careful',page=0,PAGE=150,singletons=false,browseDirs=[],bcur='',bparent=null,first=true,defaultsSet=false;
const $=id=>document.getElementById(id);
const fmtB=n=>{if(n>=1073741824)return(n/1073741824).toFixed(2)+' GB';
 if(n>=1048576)return(n/1048576).toFixed(2)+' MB';if(n>=1024)return(n/1024).toFixed(1)+' KB';return n+' B'};
const fmtD=t=>{if(!t)return '—';const d=new Date(t*1000);return d.toLocaleDateString()+' '+d.toLocaleTimeString()};
const fmtT=s=>{s=Math.max(0,Math.round(s));const h=Math.floor(s/3600),m=Math.floor(s%3600/60);return h?`${h}h ${m}m`:m?`${m}m ${s%60}s`:`${s}s`};
const esc=s=>String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
async function api(p,body){const o={headers:{'X-Token':TOKEN}};
 if(body){o.method='POST';o.headers['Content-Type']='application/json';o.body=JSON.stringify(body)}
 const r=await fetch(p,o);if(r.status===403)throw new Error('This page has no valid key. Re-open the link the app printed.');return r.json()}
function toast(msg,ms){const t=$('toast');t.innerHTML=msg;t.style.display='block';clearTimeout(t._h);t._h=setTimeout(()=>t.style.display='none',ms||4200)}
$('type').onchange=()=>{$('exts').style.display=$('type').value==='custom'?'':'none'};
$('path').addEventListener('keydown',e=>{if(e.key==='Enter')startScan()});
function skipSum(){const n=[...document.querySelectorAll('.skp:checked')].map(x=>x.parentElement.textContent.trim().split(' ')[0]);$('skipsum').textContent='Skip: '+(n.length?n.join(', '):'nothing')}
function hideGuide(){$('guide').style.display='none';try{localStorage.setItem('ds_guide_seen','1')}catch(e){}}
function showGuideIfNew(){let seen=null;try{seen=localStorage.getItem('ds_guide_seen')}catch(e){}if(!seen)$('guide').style.display='block'}
function toggleOpts(){const o=$('opts');o.style.display=o.style.display==='flex'?'none':'flex'}

/* ---------- scanning ---------- */
async function startScan(){
 const roots=$('path').value.trim();if(!roots){toast('Type or add a folder first');return}
 const minsz=Math.round((parseFloat($('minsz').value)||0)*parseInt($('minu').value));
 let r;try{r=await api('/api/scan',{roots,type:$('type').value,exts:$('exts').value,min_size:minsz,
  match:$('nmatch').value,excludes:$('excl').value,algo:$('algo').value,threads:parseInt($('threads').value),
  skip_junk:$('skipjunk').checked,skip_presets:[...document.querySelectorAll('.skp:checked')].map(x=>x.value),hash_list:$('hlist').value,
  include_singletons:mode==='destructive'&&!!$('nmatch').value.trim()})}catch(e){toast(esc(e.message));return}
 if(!r.ok){toast(esc(r.error));return}
 if(r.algo!==$('algo').value){$('algo').value=r.algo;toast('Hash set to '+r.algo.toUpperCase()+' to match the hash list')}
 sel.clear();groups=[];page=0;render();poll()}
async function cancelScan(){await api('/api/cancel',{})}
async function poll(){
 let s;try{s=await api('/api/state')}catch(e){toast(esc(e.message),9000);return}
 if(first){first=false;if(!$('path').value&&s.roots&&s.roots.length)$('path').value=s.roots.join('; ');
  if(!defaultsSet){$('excl').value=s.default_excludes.join('; ');defaultsSet=true}
  if(s.phase!=='scanning'&&s.phase!=='done')return}
 if(s.phase==='scanning'){
  $('progress').style.display='block';$('empty').style.display='none';
  let eta='';
  if(s.stage==='Hashing'&&s.bytes_total&&s.elapsed>3&&s.bytes_done>0){
   const rate=s.bytes_done/s.elapsed;eta=` · ${fmtB(rate)}/s · ETA ${fmtT((s.bytes_total-s.bytes_done)/rate)}`}
  $('pstage').innerHTML=`${esc(s.stage)} — checked ${s.scanned}`+
   (s.total?` · hashing ${s.done}/${s.total} (${fmtB(s.bytes_done)} of ${fmtB(s.bytes_total)})`:'')+eta+
   (s.cached||s.imported?` · reused ${s.cached+s.imported}`:'')+
   (s.errors?` · <a href="#" onclick="showErrors();return false">unreadable ${s.errors}</a>`:'');
  $('pbar').style.width=s.bytes_total?(100*s.bytes_done/s.bytes_total)+'%':'8%';
  setTimeout(poll,500);return}
 $('progress').style.display='none';
 if(s.phase==='done'){
  const g=await api('/api/groups');groups=g.groups||[];singletons=g.singletons;
  page=0;$('toolbar').style.display='flex';render();
  toast(`Scan done: ${groups.length} duplicate set(s), ${fmtB(groups.reduce((a,x)=>a+x.wasted,0))} reclaimable`+
   (s.cached||s.imported?` · ${s.cached+s.imported} hashes reused`:'')+
   (s.errors?` · <a href="#" onclick="showErrors();return false">${s.errors} unreadable</a>`:''),7000)}
 else if(s.message)toast(esc(s.message),6000)}
async function showErrors(){const r=await api('/api/errors');$('errM').querySelector('b').textContent='Unreadable files';
 $('errcount').textContent=`${r.count} file(s) or folder(s) couldn't be read`+(r.count>r.paths.length?` · first ${r.paths.length} shown`:'');
 $('errlist').innerHTML=r.paths.map(x=>{const[p,why]=x.split('\t');return `<div>${esc(p)}${why?`<br><span class="hint">↳ ${esc(why)}</span>`:''}</div>`}).join('')||'<div>None</div>';$('errM').style.display='flex'}

/* ---------- rendering ---------- */
function visible(){const q=$('search').value.trim().toLowerCase();
 return q?groups.filter(g=>g.files.some(f=>f.path.toLowerCase().includes(q))):groups}
function render(){
 const vis=visible(),slice=vis.slice(0,(page+1)*PAGE),box=$('groups');
 $('empty').style.display=groups.length?'none':'block';
 const multi=new Set(groups.flatMap(g=>g.files.map(f=>f.root))).size>1;
 box.innerHTML=slice.map(g=>{
  const rows=g.files.map((f,i)=>{const on=sel.has(f.path);
   return `<div class="row${on?' sel':''}" onclick="tick(${g.id},${i})"><input type="checkbox" ${on?'checked':''}>
    <div><div class="fname">${esc(f.name)}${loc(f)===0?'<span class="tag">DCIM</span>':loc(f)===2?'<span class="tag">unsorted</span>':''}${toolNamed(f.name)?'<span class="tag">tool-named</span>':''}</div>
     <div class="fdir">${esc(multi?f.path:f.rel)}</div></div>
    <div class="fmeta"><span class="lbl">modified</span>${fmtD(f.mtime)}${f.suspect?' <span class="sus" title="pre-1980 date: unreliable">⚠</span>':''}<br>
     <span class="lbl">created</span>${fmtD(f.ctime)}${f.ctime&&f.ctime<SANE?' <span class="sus" title="pre-1980 date: unreliable">⚠</span>':''} · ${fmtB(f.size)}</div></div>`}).join('');
  return `<div class="group"><div class="ghead"><span class="badge">${g.count}×</span>
    <span class="gsize">${fmtB(g.size)} each · ${fmtB(g.wasted)} reclaimable</span><span class="hash">${g.hash.slice(0,16)}</span>
    <span class="gctl"><select onchange="gsort(${g.id},this.value)"><option value="">sort…</option>
      <option value="oldest">oldest modified</option><option value="newest">newest modified</option>
      <option value="created">oldest created</option><option value="name">by name</option><option value="path">by path</option></select>
     <button onclick="gAuto(${g.id})">auto-select</button>
     <button onclick="gAll(${g.id})" ${mode==='destructive'?'':'disabled title="Destructive mode only"'}>select all</button>
     <button onclick="gInv(${g.id})">invert</button><button onclick="gNone(${g.id})">none</button></span></div>${rows}</div>`}).join('');
 $('more').style.display=vis.length>slice.length?'block':'none';
 $('more').textContent=`Show more sets (${vis.length-slice.length} hidden)`;
 updateStats()}
function updateStats(){let n=0,b=0;for(const g of groups)for(const f of g.files)if(sel.has(f.path)){n++;b+=f.size}
 $('stats').innerHTML=`<b>${n}</b> selected · <b>${fmtB(b)}</b>`;$('trashBtn').disabled=!n;$('trashBtn').textContent=$('hardDel').checked?'Delete selected':'Trash selected'}

/* ---------- DLM rules ---------- */
// Item 4: tool-made names lose to human ones; Android collision copies lose
function toolNamed(n){return /^ilovepdf_|^smallpdf_|^pdf24_|^img-\d{8}-wa\d+|^screenshot_\d{8}|~\d{8}-\d{6}|^\$R[A-Z0-9]{6}/i.test(n)}
function nameScore(n){let s=0;if(/copy of /i.test(n))s+=10;if(/-\s*copy/i.test(n))s+=10;
 s+=5*((n.match(/\(\d{1,3}\)/g)||[]).length);if(toolNamed(n))s+=8;if(/\.tmp$|\.tmp\.|\.0\./i.test(n))s+=8;return s}
// Item 5: DCIM (0) > sorted area (1) > unsorted (2)
function loc(f){const p=f.path.replace(/\\/g,'/').toLowerCase();
 if(/\/dcim\//.test(p))return 0;
 if(/\/(00_inbox|inbox|99_move_to|move_?to|downloads?|_review|folder_unsort|unsorted|desktop)\//.test(p))return 2;return 1}
const sane=t=>t&&t>=SANE?t:Infinity;
const earliest=f=>Math.min(sane(f.ctime),sane(f.mtime));
function keyFn(st){return{
 dlm:f=>[loc(f),earliest(f)===Infinity?1:0,earliest(f),nameScore(f.name),f.name.length],
 oldest:f=>[f.suspect?1:0,f.mtime,nameScore(f.name),f.name.length],
 oldcreated:f=>[sane(f.ctime)===Infinity?1:0,f.ctime,f.suspect?1:0,f.mtime],
 newest:f=>[f.suspect?1:0,-f.mtime,nameScore(f.name)],
 shortname:f=>[f.name.length,f.mtime],longname:f=>[-f.name.length,f.mtime],
 cleanname:f=>[nameScore(f.name)*100+f.name.length,f.suspect?1:0,f.mtime]}[st]}
function cmp(a,b){for(let i=0;i<a.length;i++){if(a[i]<b[i])return -1;if(a[i]>b[i])return 1}return 0}
function dirOf(p){return p.replace(/[\\/][^\\/]*$/,'').toLowerCase()}
// Item 3: per-set keep count. DLM: all copies in one folder -> keep 1; spread over folders -> keep 2 (one per folder)
function autoPick(g,keepSel){
 const k=keyFn($('strategy').value);
 const ord=[...g.files].sort((x,y)=>cmp(k(x),k(y))||(x.path<y.path?-1:1));
 let keepers;
 if(keepSel==='dlm'){keepers=[];const dirs=new Set();
  for(const f of ord){const d=dirOf(f.path);if(!dirs.has(d)&&keepers.length<2){dirs.add(d);keepers.push(f)}}}
 else{let n=parseInt(keepSel)||0;if(n===0&&careful()){n=1}keepers=ord.slice(0,n)}
 const keep=new Set(keepers.map(f=>f.path));
 return ord.filter(f=>!keep.has(f.path)).map(f=>f.path)}
function careful(){return mode!=='destructive'&&!singletons}

/* ---------- selection ---------- */
function grp(id){return groups.find(g=>g.id===id)}
function tick(id,i){const g=grp(id),path=g.files[i].path;
 if(!sel.has(path)){if(careful()&&g.files.filter(f=>!sel.has(f.path)&&f.path!==path).length===0){
   toast('Careful mode: one copy must survive. Switch to Destructive to select all.');return}sel.add(path)}
 else sel.delete(path);render()}
function gAuto(id){const g=grp(id);for(const f of g.files)sel.delete(f.path);autoPick(g,$('keepN').value).forEach(p=>sel.add(p));render()}
function gAll(id){grp(id).files.forEach(f=>sel.add(f.path));render()}
function gInv(id){const g=grp(id);g.files.forEach(f=>{if(sel.has(f.path))sel.delete(f.path);else sel.add(f.path)});
 if(careful()&&g.files.every(f=>sel.has(f.path))){sel.delete(g.files[0].path);toast('Careful mode: keeping one copy')}render()}
function gNone(id){grp(id).files.forEach(f=>sel.delete(f.path));render()}
function gsort(id,how){if(!how)return;const g=grp(id),m={oldest:(a,b)=>a.mtime-b.mtime,newest:(a,b)=>b.mtime-a.mtime,
 created:(a,b)=>a.ctime-b.ctime,name:(a,b)=>a.name.localeCompare(b.name),path:(a,b)=>a.path.localeCompare(b.path)};g.files.sort(m[how]);render()}
function autoSelect(){if($('keepN').value==='0'&&careful())toast('Careful mode: keeping 1 per set (Destructive allows 0)');
 for(const g of visible()){for(const f of g.files)sel.delete(f.path);autoPick(g,$('keepN').value).forEach(p=>sel.add(p))}render()}
function clearSel(){sel.clear();render()}
function setMode(m){mode=m;$('mCare').classList.toggle('on',m==='careful');$('mDest').classList.toggle('on',m==='destructive');
 if(careful()){for(const g of groups){if(g.files.length&&g.files.every(f=>sel.has(f.path)))sel.delete(g.files[0].path)}}render()}

/* ---------- actions ---------- */
async function trashSel(){const n=sel.size;if(!n)return;const del=$('hardDel').checked;
 if(del){const a=prompt(`PERMANENTLY delete ${n} file(s)? There is no undo.\nType DELETE to confirm.`);if(a!=='DELETE')return}
 else if(!confirm(`Move ${n} file(s) to the trash folder?\n(Nothing is deleted. Undo… restores them.)`))return;
 const r=await api('/api/trash',{paths:[...sel],mode,action:del?'delete':'trash'});if(!r.ok){toast(esc(r.error));return}
 $('progress').style.display='block';jobPoll()}
async function jobPoll(){let j;try{j=await api('/api/job')}catch(e){toast(esc(e.message),9000);return}
 const verb=j.action==='delete'?'Deleting':'Moving to trash';
 if(j.running){$('pstage').innerHTML=`${verb} — ${j.done}/${j.total} · ${fmtB(j.bytes||0)}`+(j.errors&&j.errors.length?` · ${j.errors.length} failed`:'');
  $('pbar').style.width=j.total?(100*j.done/j.total)+'%':'8%';setTimeout(jobPoll,500);return}
 $('progress').style.display='none';const r=j.result||{};
 if(!r.ok){showResult('Run failed',esc(r.error||'unknown error'),[]);return}
 sel.clear();groups=r.groups||[];page=0;render();
 showResult(`${r.action==='delete'?'Deleted':'Moved to trash'}: ${r.moved} file(s) · ${fmtB(r.bytes)}`,
  `${r.action==='delete'?'':'Trash: <code>'+esc(r.trash)+'</code><br>'}History: <code>${esc(r.history)}</code>`,r.errors||[])}
function showResult(title,html,errs){$('errcount').innerHTML=`<b>${title}</b><br>${html}`+(errs.length?`<br>${errs.length} problem(s):`:'');
 $('errlist').innerHTML=errs.map(x=>`<div>${esc(x)}</div>`).join('');$('errM').querySelector('b').textContent='Result';$('errM').style.display='flex'}
function exportCsv(){location.href='/api/export?t='+encodeURIComponent(TOKEN)}
async function openUndo(){const r=await api('/api/runs',{});if(!r.ok){toast(esc(r.error));return}
 $('ulist').innerHTML=(r.runs||[]).slice().reverse().map(x=>`<div class="runrow"><span>${esc(x.run)} — ${x.count} file(s), <b>${x.restorable}</b> still in trash</span>
   <button onclick="undoRun('${esc(x.run)}')" ${x.restorable?'':'disabled'}>Undo this run</button></div>`).join('')||'<div>No runs logged yet for these folders.</div>';
 $('undoM').style.display='flex'}
async function undoRun(run){if(!confirm(run?`Restore every file from run ${run}?`:'Restore EVERY file in the log?'))return;
 const r=await api('/api/undo',{run});closeM('undoM');
 toast(r.ok?`Restored ${r.restored} file(s)${r.skipped?`, ${r.skipped} skipped (a file already exists there)`:''}. Re-scan to refresh.`:esc(r.error),6000)}
async function openEmpty(){const r=await api('/api/empty',{junk:$('ejunk').checked});if(!r.ok){toast(esc(r.error));return}
 const d=r.dirs||[];$('ecount').textContent=d.length+' empty folder(s)';
 $('elist').innerHTML=d.map(x=>`<div>${esc(x)}</div>`).join('')||'<div>None found</div>';$('emptyM').style.display='flex'}
async function prune(){const r=await api('/api/prune',{junk:$('ejunk').checked});
 toast(r.ok?`Removed ${r.removed} folder(s)`+(r.junk?` · ${r.junk} junk file(s) to trash`:'')+(r.failed?` · ${r.failed} locked/system skipped (not an error)`:''):esc(r.error),6000);closeM('emptyM')}

/* ---------- folder picking ---------- */
function addRoot(p){const cur=$('path').value.split(';').map(s=>s.trim()).filter(Boolean);if(!cur.includes(p))cur.push(p);$('path').value=cur.join('; ')}
async function openBrowse(){toast('Opening folder picker…',2500);let r={};
 try{r=await api('/api/pick',{start:$('path').value.split(';').pop().trim()})}catch(e){}
 $('toast').style.display='none';if(r.ok){addRoot(r.path);return}if(r.cancelled)return;
 await bload('');$('browseM').style.display='flex'}
async function bload(p){const r=await api('/api/browse',{path:p});if(!r.ok){toast(esc(r.error));return}
 bcur=r.path;bparent=r.parent;browseDirs=r.dirs||[];$('bpath').textContent=bcur||'(drives)';
 let h='';if(bparent!==null)h+=`<div class="click" onclick="bload(bparent)">⬆ ..</div>`;
 h+=browseDirs.map((d,i)=>`<div class="click" onclick="bload(browseDirs[${i}])">📁 ${esc(d)}</div>`).join('');$('blist').innerHTML=h}
function chooseBrowse(){if(bcur)addRoot(bcur);closeM('browseM')}
function closeM(id){$(id).style.display='none'}
poll();
showGuideIfNew();
/* drag-and-drop: the desktop app adds the real paths; a plain browser can't see paths */
let dragN=0;
document.addEventListener('dragenter',e=>{e.preventDefault();dragN++;document.body.classList.add('dropping')});
document.addEventListener('dragleave',e=>{if(--dragN<=0){dragN=0;document.body.classList.remove('dropping')}});
document.addEventListener('dragover',e=>e.preventDefault());
document.addEventListener('drop',e=>{e.preventDefault();dragN=0;document.body.classList.remove('dropping');
 if(window.pywebview)toast('Folder added — press Scan',2500);
 else toast('Drag-and-drop works in the desktop app. Here, type or paste the folder path.',5000)});
</script></body></html>
"""


# ------------------------------------------------------------------ main
def main():
    ap = argparse.ArgumentParser(description='Dedupe Studio - duplicate cleanup with a local UI')
    ap.add_argument('folders', nargs='*', help='folder(s) to pre-fill')
    ap.add_argument('--port', type=int, default=7788)
    ap.add_argument('--no-browser', action='store_true')
    args = ap.parse_args()
    for _ in range(20):
        try:
            srv = ThreadingHTTPServer(('127.0.0.1', args.port), Handler)
            break
        except OSError:
            args.port += 1
    else:
        sys.exit('No free port found.')
    pre = [os.path.abspath(os.path.expanduser(f)) for f in args.folders if os.path.isdir(os.path.expanduser(f))]
    with LOCK:
        STATE['roots'] = pre
    url = 'http://127.0.0.1:%d/?t=%s' % (args.port, TOKEN)
    print('Dedupe Studio %s running at:\n  %s\n(Ctrl+C to stop. The ?t= key is required; it changes every launch.)' % (VERSION, url))
    if not args.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print('\nBye.')


if __name__ == '__main__':
    main()
