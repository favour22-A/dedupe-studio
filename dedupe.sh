#!/usr/bin/env bash
# ============================================================================
# dedupe.sh v2.1 — comprehensive SHA-256 duplicate cleanup
# Runs on: Linux, Termux, Git Bash. macOS needs bash 4+ (brew install bash).
#
# GUARANTEE: never deletes, never overwrites. Extra copies are MOVED to a
# trash folder, every move is logged, and --undo restores everything.
#
# USAGE
#   bash dedupe.sh DIR [options]        preview (default - nothing moves)
#   bash dedupe.sh DIR --apply          move extra copies to trash
#   bash dedupe.sh DIR --interactive    choose per set from the terminal
#   bash dedupe.sh DIR --undo           restore EVERYTHING in the log
#   bash dedupe.sh DIR --undo RUN_ID    restore one run only (see --list-runs)
#   bash dedupe.sh DIR --list-runs      list past runs in the log
#
# OPTIONS
#   --keep N          copies to keep per duplicate set (default 3).
#                     0 = PURGE MODE: trash every match, singletons included
#                     (requires --match / --ext / --type, or --force)
#   --strategy S      which copies to KEEP first:
#                     oldest (default) | newest | shortname | longname | cleanname
#   --prefer LIST     colon-separated folders (relative to DIR) whose copies are
#                     kept first and NEVER trashed (Czkawka "reference folders"),
#                     e.g. --prefer 'Me/Documents:02_Resumes'
#   --match GLOB      only files whose NAME matches, case-insensitive
#                     e.g. --match 'desktop.ini'   --match '*.bak'
#   --ext LIST        only these extensions, e.g. --ext .jpg,.png
#   --type T          preset: pictures|videos|music|documents|archives|junk|all
#   --min-size SZ     ignore smaller files (e.g. 1M, 500K; default 1)
#   --max-size SZ     ignore larger files
#   --exclude GLOB    skip paths matching glob (repeatable), e.g. '*/node_modules/*'
#   --trash DIR       put trash OUTSIDE the tree (recommended for Google Drive;
#                     the log and hash cache move there too)
#   --prune-empty     after apply/undo: remove now-empty folders (error-tolerant,
#                     survives Android/data refusals)
#   --list-empty      only list empty folders, then exit
#   --no-cache        ignore and don't write the hash cache
#   --dry-run         alias for the default preview
#   --force           allow --keep 0 with no filter (purge all dupes in tree)
#
# INTERACTIVE MODE (--interactive)
#   Each duplicate set is shown numbered; you type which to KEEP:
#     Enter       keep the first KEEP copies (the suggested ones)
#     1 3         keep copies 1 and 3, trash the rest
#     a           keep all (skip this set)
#     t           trash ALL copies in the set (asks to confirm)
#     q           stop (what you already chose stays done; undo available)
#   Choices apply immediately (moves to trash, logged, undoable).
#
# SPEED: only files sharing a size are hashed; files >= 8 MB are pre-filtered
#   by a 64 KB quick hash first; full hashes are cached in the trash folder
#   (hash_cache.tsv), so re-runs only hash new or changed files.
#
# CONFIG FILE: .deduperc in DIR or $HOME, KEY=VALUE lines:
#   KEEP, STRATEGY, TRASH_DIR, MATCH, EXT, TYPE, MIN_SIZE, MAX_SIZE, PREFER,
#   EXCLUDE (colon-separated globs). Precedence: flags > env > DIR rc > HOME rc.
#
# ALWAYS TRUE
#   - empty (0-byte) files are never touched
#   - Google Drive shortcuts (.gdoc .gsheet .gslides ...) are never touched
#   - modified dates before 1980 are epoch-garbage: those copies only survive
#     when no sane-dated copy exists
#   - ties broken by name cleanliness ("copy", "(2)" lose), then path
#   - on Termux shared storage run via:  bash dedupe.sh ...  (exec bit won't stick)
# ============================================================================
set -uo pipefail

usage() { sed -n '2,/^# =\{20\}/p' "$0"; }

[ $# -ge 1 ] || { usage; exit 1; }

# ---------- defaults ----------
D_KEEP=3 D_STRATEGY=oldest D_TRASH="" D_MATCH="" D_EXT="" D_TYPE=all
D_MIN=1 D_MAX="" D_EXCLUDE="" D_PREFER=""
SANE_EPOCH=315532800   # 1980-01-01; mtimes older than this are unreliable
QH_MIN=$((8*1024*1024))

MODE=preview ROOT="" FORCE=0 PRUNE=0 LIST_EMPTY=0 LIST_RUNS=0
INTERACTIVE=0 USE_CACHE=1 UNDO_RUN=""
F_KEEP="" F_STRATEGY="" F_TRASH="" F_MATCH="" F_EXT="" F_TYPE="" F_MIN="" F_MAX="" F_EXCLUDE="" F_PREFER=""

# ---------- arg parsing ----------
while [ $# -gt 0 ]; do
  case "$1" in
    --apply) MODE=apply ;;
    --undo) MODE=undo
      if [ $# -gt 1 ] && [[ "$2" =~ ^[0-9]{8}_[0-9]{6}$ ]]; then UNDO_RUN="$2"; shift; fi ;;
    --interactive|-i) INTERACTIVE=1 ;;
    --list-runs) LIST_RUNS=1 ;;
    --dry-run|--preview) MODE=preview ;;
    --keep) F_KEEP="${2:?--keep needs a number}"; shift ;;
    --strategy) F_STRATEGY="${2:?}"; shift ;;
    --prefer) F_PREFER="${2:?}"; shift ;;
    --match) F_MATCH="${2:?}"; shift ;;
    --ext) F_EXT="${2:?}"; shift ;;
    --type) F_TYPE="${2:?}"; shift ;;
    --min-size) F_MIN="${2:?}"; shift ;;
    --max-size) F_MAX="${2:?}"; shift ;;
    --exclude) F_EXCLUDE="${F_EXCLUDE:+$F_EXCLUDE:}${2:?}"; shift ;;
    --trash) F_TRASH="${2:?}"; shift ;;
    --prune-empty) PRUNE=1 ;;
    --list-empty) LIST_EMPTY=1 ;;
    --no-cache) USE_CACHE=0 ;;
    --force) FORCE=1 ;;
    -h|--help) usage; exit 0 ;;
    -*) echo "Unknown option: $1" >&2; exit 1 ;;
    *) [ -n "$ROOT" ] && { echo "Only one DIR allowed" >&2; exit 1; }; ROOT="$1" ;;
  esac
  shift
done

[ -n "$ROOT" ] || { echo "No folder given." >&2; usage; exit 1; }
ROOT="$(cd -- "$ROOT" && pwd)" || { echo "Cannot access folder." >&2; exit 1; }

# ---------- config: flags > env > DIR rc > HOME rc > defaults ----------
RC1_KEEP="" RC1_STRATEGY="" RC1_TRASH_DIR="" RC1_MATCH="" RC1_EXT="" RC1_TYPE="" RC1_MIN_SIZE="" RC1_MAX_SIZE="" RC1_EXCLUDE="" RC1_PREFER=""
RC2_KEEP="" RC2_STRATEGY="" RC2_TRASH_DIR="" RC2_MATCH="" RC2_EXT="" RC2_TYPE="" RC2_MIN_SIZE="" RC2_MAX_SIZE="" RC2_EXCLUDE="" RC2_PREFER=""
load_rc() { # $1=file $2=prefix
  [ -f "$1" ] || return 0
  while IFS='=' read -r k v; do
    case "$k" in KEEP|STRATEGY|TRASH_DIR|MATCH|EXT|TYPE|MIN_SIZE|MAX_SIZE|EXCLUDE|PREFER)
      printf -v "$2$k" '%s' "$v" ;;
    esac
  done < <(grep -E '^[A-Z_]+=' "$1" 2>/dev/null)
}
load_rc "$HOME/.deduperc" RC2_
load_rc "$ROOT/.deduperc" RC1_
pick() { # flag env rc1 rc2 default
  if [ -n "$1" ]; then echo "$1"; elif [ -n "$2" ]; then echo "$2";
  elif [ -n "$3" ]; then echo "$3"; elif [ -n "$4" ]; then echo "$4"; else echo "$5"; fi
}
KEEP=$(pick     "$F_KEEP"     "${KEEP:-}"      "$RC1_KEEP"      "$RC2_KEEP"      "$D_KEEP")
STRATEGY=$(pick "$F_STRATEGY" "${STRATEGY:-}"  "$RC1_STRATEGY"  "$RC2_STRATEGY"  "$D_STRATEGY")
TRASH_DIR=$(pick "$F_TRASH"   "${TRASH_DIR:-}" "$RC1_TRASH_DIR" "$RC2_TRASH_DIR" "$D_TRASH")
MATCH=$(pick    "$F_MATCH"    "${MATCH:-}"     "$RC1_MATCH"     "$RC2_MATCH"     "$D_MATCH")
EXT=$(pick      "$F_EXT"      "${EXT:-}"       "$RC1_EXT"       "$RC2_EXT"       "$D_EXT")
TYPE=$(pick     "$F_TYPE"     "${TYPE:-}"      "$RC1_TYPE"      "$RC2_TYPE"      "$D_TYPE")
MIN_RAW=$(pick  "$F_MIN"      "${MIN_SIZE:-}"  "$RC1_MIN_SIZE"  "$RC2_MIN_SIZE"  "$D_MIN")
MAX_RAW=$(pick  "$F_MAX"      "${MAX_SIZE:-}"  "$RC1_MAX_SIZE"  "$RC2_MAX_SIZE"  "$D_MAX")
EXCLUDE=$(pick  "$F_EXCLUDE"  "${EXCLUDE:-}"   "$RC1_EXCLUDE"   "$RC2_EXCLUDE"   "$D_EXCLUDE")
PREFER=$(pick   "$F_PREFER"   "${PREFER:-}"    "$RC1_PREFER"    "$RC2_PREFER"    "$D_PREFER")

parse_size() {
  local s n u
  s=$(printf '%s' "$1" | tr '[:lower:]' '[:upper:]'); s="${s%B}"
  n="${s%[KMGT]}"; u="${s#"$n"}"
  [[ "$n" =~ ^[0-9]+$ ]] || { echo "Bad size: $1" >&2; exit 1; }
  case "$u" in K) echo $((n*1024));; M) echo $((n*1048576));;
    G) echo $((n*1073741824));; T) echo $((n*1099511627776));; *) echo "$n";; esac
}
MIN_SIZE=$(parse_size "$MIN_RAW"); [ "$MIN_SIZE" -lt 1 ] && MIN_SIZE=1
MAX_SIZE=""; [ -n "$MAX_RAW" ] && MAX_SIZE=$(parse_size "$MAX_RAW")

[[ "$KEEP" =~ ^[0-9]+$ ]] || { echo "KEEP must be a non-negative integer" >&2; exit 1; }
case "$STRATEGY" in oldest|newest|shortname|longname|cleanname) ;;
  *) echo "Bad --strategy: $STRATEGY" >&2; exit 1 ;; esac

IFS=':' read -r -a PREFER_LIST <<< "$PREFER"

# type presets -> extension list
case "$TYPE" in
  pictures) PRESET_EXT=".jpg,.jpeg,.png,.gif,.bmp,.heic,.heif,.webp,.raw,.cr2,.nef,.arw,.dng,.tif,.tiff,.svg,.ico" ;;
  videos)   PRESET_EXT=".mp4,.mkv,.mov,.avi,.wmv,.flv,.webm,.m4v,.3gp,.mts,.ts,.mpg,.mpeg" ;;
  music)    PRESET_EXT=".mp3,.flac,.wav,.m4a,.aac,.ogg,.opus,.wma,.aiff,.mid" ;;
  documents) PRESET_EXT=".pdf,.doc,.docx,.xls,.xlsx,.ppt,.pptx,.txt,.md,.odt,.ods,.odp,.rtf,.csv,.epub" ;;
  archives) PRESET_EXT=".zip,.rar,.7z,.tar,.gz,.bz2,.xz,.iso" ;;
  junk)     PRESET_EXT=""; [ -z "$MATCH" ] && MATCH="desktop.ini" ;;
  all|"")   PRESET_EXT="" ;;
  *) echo "Bad --type: $TYPE" >&2; exit 1 ;;
esac
[ -n "$PRESET_EXT" ] && EXT="${EXT:+$EXT,}$PRESET_EXT"

PURGE=0
if [ "$KEEP" -eq 0 ]; then
  PURGE=1
  if [ -z "$MATCH" ] && [ -z "$EXT" ] && [ "$FORCE" -ne 1 ]; then
    echo "--keep 0 purges EVERY matching file. Add --match/--ext/--type, or --force." >&2
    exit 1
  fi
fi
if [ "$INTERACTIVE" -eq 1 ]; then
  [ "$PURGE" -eq 1 ] && { echo "--interactive doesn't combine with --keep 0." >&2; exit 1; }
  MODE=apply   # choices apply immediately, with trash + log + undo
fi

TRASH="${TRASH_DIR:-$ROOT/_dedupe_trash}"
DEFAULT_TRASH="$ROOT/_dedupe_trash"
if [ -n "$TRASH_DIR" ]; then LOG="$TRASH_DIR/dedupe_log.tsv"; else LOG="$ROOT/dedupe_log.tsv"; fi
CACHE="$TRASH/hash_cache.tsv"

# ---------- portability shims ----------
if stat -c '%s' / >/dev/null 2>&1; then
  size_mtime() { stat -c '%s %Y' -- "$1"; }
else
  size_mtime() { stat -f '%z %m' -- "$1"; }
fi
if command -v sha256sum >/dev/null; then HASHER=sha256sum
else HASHER="shasum -a 256"; fi
hash_of() { $HASHER < "$1" | cut -d' ' -f1; }
HAVE_PRINTF=0
find "$ROOT" -maxdepth 0 -printf '' >/dev/null 2>&1 && HAVE_PRINTF=1
IS_TTY=0; [ -t 2 ] && IS_TTY=1
TTYIN=/dev/stdin
( : < /dev/tty ) 2>/dev/null && TTYIN=/dev/tty

# ---------- list runs ----------
if [ "$LIST_RUNS" -eq 1 ]; then
  [ -f "$LOG" ] || { echo "No log at $LOG"; exit 1; }
  echo "Runs in $LOG:"
  awk -F'\t' 'NR>1 && $1!="" {c[$1]++} END {for (r in c) printf "  %s  %d file(s)\n", r, c[r]}' "$LOG" | sort
  echo "Undo one with: bash dedupe.sh \"$ROOT\" --undo RUN_ID"
  exit 0
fi

# ---------- empty-dir handling (error-tolerant; survives Android/data) ----------
list_empty_dirs() {
  find "$ROOT" -depth -type d -empty -print 2>/dev/null | while IFS= read -r d; do
    [ "$d" = "$ROOT" ] && continue
    case "$d" in "$TRASH"|"$TRASH"/*|"$DEFAULT_TRASH"|"$DEFAULT_TRASH"/*) continue ;; esac
    printf '%s\n' "$d"
  done
}
prune_empty_dirs() {
  local total=0 failed=0 pass_removed d
  while :; do
    pass_removed=0
    while IFS= read -r -d '' d; do
      [ "$d" = "$ROOT" ] && continue
      case "$d" in "$TRASH"|"$TRASH"/*|"$DEFAULT_TRASH"|"$DEFAULT_TRASH"/*) continue ;; esac
      if rmdir -- "$d" 2>/dev/null; then
        total=$((total+1)); pass_removed=$((pass_removed+1))
      else
        failed=$((failed+1))
      fi
    done < <(find "$ROOT" -depth -type d -empty -print0 2>/dev/null)
    [ "$pass_removed" -eq 0 ] && break
    failed=0
  done
  echo "Empty folders removed: $total"
  [ "$failed" -gt 0 ] && echo "Could not remove (locked/system, e.g. Android/data): $failed  — skipped, not an error"
  return 0
}

if [ "$LIST_EMPTY" -eq 1 ]; then
  echo "Empty folders under $ROOT:"
  list_empty_dirs
  exit 0
fi

# ---------- UNDO ----------
if [ "$MODE" = "undo" ]; then
  [ -f "$LOG" ] || { echo "No log at $LOG  (if you used --trash DIR, pass the same --trash)"; exit 1; }
  restored=0
  while IFS=$'\t' read -r rstamp _ orig _ dest; do
    [ -n "$UNDO_RUN" ] && [ "$rstamp" != "$UNDO_RUN" ] && continue
    [ -e "$dest" ] || continue
    if [ -e "$orig" ]; then echo "exists, skipped: $orig"; continue; fi
    mkdir -p -- "$(dirname -- "$orig")"
    mv -- "$dest" "$orig" && restored=$((restored+1))
  done < "$LOG"
  echo "Restored $restored file(s)${UNDO_RUN:+ from run $UNDO_RUN}."
  if [ "$PRUNE" -eq 1 ]; then prune_empty_dirs; fi
  exit 0
fi

# ---------- banner ----------
echo "dedupe.sh v2.1 — never deletes, never overwrites; every move is logged."
echo "Target   : $ROOT"
if [ "$INTERACTIVE" -eq 1 ]; then echo "Mode     : interactive (your choices apply immediately)"
else echo "Mode     : $MODE"; fi
if [ "$PURGE" -eq 1 ]; then
  echo "Keep     : 0  (PURGE: every match goes to trash, singletons included)"
else
  echo "Keep     : $KEEP copies per set   Strategy: keep $STRATEGY first"
fi
[ -n "$PREFER" ]  && echo "Prefer   : $PREFER (kept first, never trashed)"
[ -n "$MATCH" ]   && echo "Match    : $MATCH (name, case-insensitive)"
[ -n "$EXT" ]     && echo "Ext      : $EXT"
[ -n "$EXCLUDE" ] && echo "Exclude  : $EXCLUDE"
echo "Size     : >= $MIN_SIZE bytes${MAX_SIZE:+, <= $MAX_SIZE bytes}"
echo "Trash    : $TRASH"
echo

# ---------- filters ----------
shopt -s nocasematch
GSHORTCUTS=".gdoc .gsheet .gslides .gdraw .gform .gmap .gsite .glink"
is_preferred() { # path -> 0 if inside a prefer folder
  local p="$1" e
  for e in "${PREFER_LIST[@]}"; do
    [ -n "$e" ] || continue
    e="${e%/}"
    if [[ "$p" == "$ROOT/$e/"* ]] || [[ "$p" == "$e/"* ]]; then return 0; fi
  done
  return 1
}
want_file() { # path size -> 0 keep / 1 skip
  local f="$1" sz="$2" base lcb e pat ok
  case "$f" in
    "$TRASH"/*|"$DEFAULT_TRASH"/*|"$LOG"|"$ROOT/.deduperc") return 1 ;;
  esac
  [ "$sz" -eq 0 ] && { empty=$((empty+1)); return 1; }
  [ "$sz" -lt "$MIN_SIZE" ] && return 1
  [ -n "$MAX_SIZE" ] && [ "$sz" -gt "$MAX_SIZE" ] && return 1
  base="${f##*/}"; lcb="${base,,}"
  for e in $GSHORTCUTS; do [[ "$lcb" == *"$e" ]] && return 1; done
  if [ -n "$EXT" ]; then
    ok=1
    IFS=',' read -r -a _exts <<< "$EXT"
    for e in "${_exts[@]}"; do
      e="${e// /}"; e="${e,,}"; [ -n "$e" ] || continue
      [[ "$lcb" == *"$e" ]] && { ok=0; break; }
    done
    [ "$ok" -eq 1 ] && return 1
  fi
  if [ -n "$MATCH" ]; then [[ "$base" == $MATCH ]] || return 1; fi
  if [ -n "$EXCLUDE" ]; then
    IFS=':' read -r -a _pats <<< "$EXCLUDE"
    for pat in "${_pats[@]}"; do [ -n "$pat" ] && [[ "$f" == $pat ]] && return 1; done
  fi
  return 0
}

# ---------- SCAN ----------
declare -a P=() S=() M=()   # path size mtime per index
declare -A size_count=() members=() pathidx=()
empty=0 scanned=0

add_file() { # path size mtime
  local i=${#P[@]}
  P+=("$1"); S+=("$2"); M+=("$3")
  pathidx["$1"]=$i
  size_count[$2]=$(( ${size_count[$2]:-0} + 1 ))
}

if [ "$HAVE_PRINTF" -eq 1 ]; then
  while IFS= read -r -d '' rec; do
    sz="${rec%%$'\t'*}"; rest="${rec#*$'\t'}"
    mt="${rest%%$'\t'*}"; mt="${mt%%.*}"; f="${rest#*$'\t'}"
    scanned=$((scanned+1))
    want_file "$f" "$sz" || continue
    add_file "$f" "$sz" "$mt"
  done < <(find "$ROOT" -type f -printf '%s\t%T@\t%p\0' 2>/dev/null)
else
  while IFS= read -r -d '' f; do
    scanned=$((scanned+1))
    read -r sz mt < <(size_mtime "$f") || continue
    want_file "$f" "$sz" || continue
    add_file "$f" "$sz" "$mt"
  done < <(find "$ROOT" -type f -print0 2>/dev/null)
fi
echo "Files scanned: $scanned   matching filters: ${#P[@]}   empty skipped: $empty"

STAMP=$(date +%Y%m%d_%H%M%S)
RUN_TRASH="$TRASH/$STAMP"
moved=0 bytes=0 unreadable=0 QUIT=0

ensure_log() {
  if [ "$MODE" = "apply" ] && [ ! -f "$LOG" ]; then
    mkdir -p -- "$(dirname -- "$LOG")"
    printf 'time\thash\toriginal\tkept\ttrashed_to\n' > "$LOG"
  fi
}

trash_file() { # path hash kept-note
  local src="$1" h="$2" kept="$3" rel dest
  rel="${src#"$ROOT"/}"
  dest="$RUN_TRASH/$rel"
  mkdir -p -- "$(dirname -- "$dest")"
  if [ -e "$dest" ]; then dest="$dest.$(date +%s%N)"; fi
  if mv -- "$src" "$dest"; then
    printf '%s\t%s\t%s\t%s\t%s\n' "$STAMP" "$h" "$src" "$kept" "$dest" >> "$LOG"
    echo "   trashed: $src"
  else
    echo "   FAILED to move: $src" >&2
  fi
}

# ---------- PURGE MODE (--keep 0) ----------
if [ "$PURGE" -eq 1 ]; then
  ensure_log
  for i in "${!P[@]}"; do
    if is_preferred "${P[$i]}"; then echo "kept (preferred): ${P[$i]}"; continue; fi
    moved=$((moved+1)); bytes=$((bytes+S[i]))
    if [ "$MODE" = "apply" ]; then
      trash_file "${P[$i]}" "-" "-"
    else
      echo "WOULD trash: ${P[$i]}"
    fi
  done
  echo
  echo "Matches: $moved ($(awk -v b="$bytes" 'BEGIN{printf "%.2f", b/1048576}') MB)"
  if [ "$MODE" = "apply" ]; then
    echo "Trash : $RUN_TRASH"
    echo "Log   : $LOG   (undo with --undo)"
    if [ "$PRUNE" -eq 1 ]; then prune_empty_dirs; fi
  else
    echo "Preview only. Apply with: bash dedupe.sh \"$ROOT\" --keep 0 ${MATCH:+--match '$MATCH' }--apply"
  fi
  exit 0
fi

# ---------- HASH (size prefilter -> 64KB quick hash for big files -> cached full hash) ----------
declare -a SIZECAND=()
for i in "${!P[@]}"; do
  [ "${size_count[${S[$i]}]}" -gt 1 ] && SIZECAND+=("$i")
done

declare -A qh_count=() qh_key=()
for i in "${SIZECAND[@]}"; do
  [ "${S[$i]}" -ge "$QH_MIN" ] || continue
  if qh=$(head -c 65536 -- "${P[$i]}" 2>/dev/null | $HASHER | cut -c1-16) && [ -n "$qh" ]; then
    qh_key[$i]="${S[$i]}:$qh"
    qh_count[${qh_key[$i]}]=$(( ${qh_count[${qh_key[$i]}]:-0} + 1 ))
  fi
done

declare -A cache=()
if [ "$USE_CACHE" -eq 1 ] && [ -f "$CACHE" ]; then
  while IFS=$'\t' read -r cp csz cmt ch; do cache["$cp|$csz|$cmt"]="$ch"; done < "$CACHE"
fi
[ "$USE_CACHE" -eq 1 ] && mkdir -p -- "$TRASH"

declare -a CAND=() WEIRD=()
prefiltered=0 cached_n=0
for i in "${SIZECAND[@]}"; do
  if [ -n "${qh_key[$i]:-}" ] && [ "${qh_count[${qh_key[$i]}]}" -le 1 ]; then
    prefiltered=$((prefiltered+1)); continue   # unique first 64KB: cannot be a duplicate
  fi
  key="${P[$i]}|${S[$i]}|${M[$i]}"
  if [ -n "${cache[$key]:-}" ]; then
    members[${cache[$key]}]+="$i "; cached_n=$((cached_n+1)); continue
  fi
  case "${P[$i]}" in
    *$'\n'*|*\\*) WEIRD+=("$i") ;;   # sha256sum escapes these names; hash individually
    *) CAND+=("$i") ;;
  esac
done
TOTALH=$(( ${#CAND[@]} + ${#WEIRD[@]} ))
echo "Hashing $TOTALH file(s)  (from cache: $cached_n, skipped by quick-hash: $prefiltered)"
hashed=0

cache_put() { # path index hash
  [ "$USE_CACHE" -eq 1 ] || return 0
  case "$1" in *$'\t'*|*$'\n'*) return 0 ;; esac
  printf '%s\t%s\t%s\t%s\n' "$1" "${S[$2]}" "${M[$2]}" "$3" >> "$CACHE"
}

if [ "${#CAND[@]}" -gt 0 ]; then
  TMPLIST=$(mktemp)
  for i in "${CAND[@]}"; do printf '%s\0' "${P[$i]}"; done > "$TMPLIST"
  while IFS= read -r line; do
    h="${line:0:64}"
    fpath="${line:66}"
    [ -n "${pathidx[$fpath]:-}" ] || continue
    ii="${pathidx[$fpath]}"
    members[$h]+="$ii "
    cache_put "$fpath" "$ii" "$h"
    hashed=$((hashed+1))
    if [ "$IS_TTY" -eq 1 ] && [ $((hashed % 100)) -eq 0 ]; then
      printf '\r  %d / %d' "$hashed" "$TOTALH" >&2
    fi
  done < <(xargs -0 $HASHER -- < "$TMPLIST" 2>/dev/null)
  rm -f "$TMPLIST"
fi
for i in "${WEIRD[@]}"; do
  if h=$(hash_of "${P[$i]}"); then
    members[$h]+="$i "
    cache_put "${P[$i]}" "$i" "$h"
    hashed=$((hashed+1))
  fi
done
[ "$IS_TTY" -eq 1 ] && printf '\r' >&2
unreadable=$(( TOTALH - hashed ))
[ "$unreadable" -gt 0 ] && echo "Unreadable, skipped: $unreadable file(s)" >&2

# ---------- RESOLVE ----------
clean_score() { # name -> number; lower = cleaner
  local n="$1" p=0
  [[ "$n" == *copy* || "$n" == *"("[0-9]")"* || "$n" == *"("[0-9][0-9]")"* ]] && p=$((p+100))
  echo $(( p + ${#n} ))
}
inv() { echo $(( 9999999999 - $1 )); }

ensure_log
groups=0
for h in "${!members[@]}"; do
  [ "$QUIT" -eq 1 ] && break
  read -r -a idx <<< "${members[$h]}"
  [ "${#idx[@]}" -gt "$KEEP" ] || continue
  groups=$((groups+1))

  # sort keys: prefer, a, b, c numeric asc, then path; index is the LAST field
  sorted=()
  while IFS= read -r -d '' rec; do sorted+=("${rec##*$'\t'}"); done < <(
    for i in "${idx[@]}"; do
      f="${P[$i]}"; base="${f##*/}"; mt="${M[$i]}"
      sus=0; [ "$mt" -lt "$SANE_EPOCH" ] && sus=1
      cs=$(clean_score "$base")
      pref=1; is_preferred "$f" && pref=0
      case "$STRATEGY" in
        oldest)    a=$sus; b=$mt;        c=$cs ;;
        newest)    a=$sus; b=$(inv "$mt"); c=$cs ;;
        shortname) a=${#base}; b=$mt;    c=$cs ;;
        longname)  a=$(inv "${#base}"); b=$mt; c=$cs ;;
        cleanname) a=$cs; b=$sus;        c=$mt ;;
      esac
      printf '%d\t%011d\t%011d\t%011d\t%s\t%s\0' "$pref" "$a" "$b" "$c" "$f" "$i"
    done | sort -z -t $'\t' -k1,1n -k2,2n -k3,3n -k4,4n -k5,5
  )
  sz="${S[${idx[0]}]}"
  keeper="${P[${sorted[0]}]}"

  # ----- interactive: you choose -----
  if [ "$INTERACTIVE" -eq 1 ]; then
    echo
    echo "Set ${h:0:12} (${#sorted[@]} copies, $sz bytes each) — suggested keeps marked *"
    n=0
    for j in "${sorted[@]}"; do
      n=$((n+1))
      mark=' '; [ "$n" -le "$KEEP" ] && mark='*'
      pnote=""; is_preferred "${P[$j]}" && pnote="  (preferred)"
      printf '  [%d]%s %s%s\n' "$n" "$mark" "${P[$j]}" "$pnote"
    done
    keepset=""
    while :; do
      printf 'Keep which? [Enter=first %d] e.g. "1 3" / a=keep all / t=trash ALL / q=quit: ' "$KEEP" >&2
      IFS= read -r ans < "$TTYIN" || ans=q
      case "$ans" in
        q|Q) QUIT=1; break ;;
        a|A) keepset=all; break ;;
        t|T)
          printf 'Trash ALL %d copies of this set? [y/N]: ' "${#sorted[@]}" >&2
          IFS= read -r conf < "$TTYIN" || conf=n
          [[ "$conf" == y || "$conf" == Y ]] && { keepset=none; break; }
          ;;
        "")
          keepset=""
          for ((k=1; k<=KEEP && k<=${#sorted[@]}; k++)); do keepset+=" $k"; done
          break ;;
        *)
          ok=1; picks=""
          read -r -a nums <<< "${ans//,/ }"
          for v in "${nums[@]}"; do
            [[ "$v" =~ ^[0-9]+$ ]] && [ "$v" -ge 1 ] && [ "$v" -le "${#sorted[@]}" ] || { ok=0; break; }
            picks+=" $v"
          done
          [ "$ok" -eq 1 ] && [ -n "$picks" ] && { keepset="$picks"; break; }
          echo "  Didn't get that — numbers 1-${#sorted[@]}, or a / t / q." >&2
          ;;
      esac
    done
    [ "$QUIT" -eq 1 ] && break
    [ "$keepset" = all ] && continue
    n=0
    for j in "${sorted[@]}"; do
      n=$((n+1))
      if [ "$keepset" != none ] && [[ " $keepset " == *" $n "* ]]; then
        echo "   keep   : ${P[$j]}"
        continue
      fi
      if is_preferred "${P[$j]}"; then echo "   kept (preferred): ${P[$j]}"; continue; fi
      moved=$((moved+1)); bytes=$((bytes+sz))
      trash_file "${P[$j]}" "$h" "$keeper"
    done
    continue
  fi

  # ----- automatic resolve -----
  echo "Set ${h:0:12} (${#idx[@]} copies, $sz bytes) - keeping:"
  for j in "${sorted[@]:0:KEEP}"; do printf '   %s\n' "${P[$j]}"; done

  for j in "${sorted[@]:KEEP}"; do
    if is_preferred "${P[$j]}"; then echo "   kept (preferred): ${P[$j]}"; continue; fi
    moved=$((moved+1)); bytes=$((bytes+sz))
    if [ "$MODE" = "apply" ]; then
      trash_file "${P[$j]}" "$h" "$keeper"
    else
      echo "   WOULD trash: ${P[$j]}"
    fi
  done
done

# ---------- SUMMARY ----------
echo
[ "$QUIT" -eq 1 ] && echo "Stopped early — choices already made are done and logged."
echo "Duplicate sets over keep-limit: $groups"
echo "Extra copies : $moved ($(awk -v b="$bytes" 'BEGIN{printf "%.2f", b/1048576}') MB)"
echo "Empty skipped: $empty"
if [ "$MODE" = "apply" ]; then
  echo "Trash        : $RUN_TRASH  (review, then delete when satisfied)"
  echo "Log          : $LOG  (undo all: --undo   this run only: --undo $STAMP)"
  if [ "$PRUNE" -eq 1 ]; then prune_empty_dirs; fi
else
  echo "Preview only. Apply with: bash dedupe.sh \"$ROOT\" --apply"
fi
exit 0
