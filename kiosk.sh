#!/bin/bash
export XDG_RUNTIME_DIR=/run/user/1000 WAYLAND_DISPLAY=wayland-0
DIR="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
export INFOSCREEN_DIR="$DIR"    # kiosk.lua reads it; mpv inherits our environment
EXITLOG="$DIR/mpv_exits.log"    # persistent (survives the nightly reboot, unlike /tmp/kiosk.log)
# Render every configured screen once at startup so tap-switching is instant (screens defined in screens.conf).
# Backgrounded: a slow API must not delay mpv launch; mpv paints the existing panel_*.bgra, renders refresh them.
while IFS=: read -r key script live; do
  case "$key" in ""|\#*) continue;; esac
  python3 "$DIR/$script" >/dev/null 2>&1 &
done < "$DIR/screens.conf"

# Only offer entries that actually exist: a playlist line pointing at a missing file used to
# take mpv down ("Exiting... (Some errors happened)") and get it respawned, which is the flash
# the display showed. The lua filters the same way at runtime and names the broken entry on
# screen; this is just so the very first file can never be the broken one.
pick(){
  grep -vhE '^[[:space:]]*#|^[[:space:]]*$' "$DIR/playlist.txt" \
    | while IFS= read -r f; do [ -e "$DIR/media/$f" ] && printf '%s\n' "$f"; done \
    | shuf -n1
}
while IFS= read -r f; do
  [ -e "$DIR/media/$f" ] || echo "[kiosk.sh] playlist entry has no file in media/: '$f'"
done < <(grep -vhE '^[[:space:]]*#|^[[:space:]]*$' "$DIR/playlist.txt")

while true; do
  M=$(pick); [ -z "$M" ] && M=$(ls "$DIR/media" | shuf -n1)
  # --idle=yes + --force-window=yes: a load failure (missing file, corrupt clip, codec mpv
  #   cannot open) leaves mpv alive and idle with its window intact instead of exiting, so the
  #   lua can name the file and load another one. Without it ANY bad entry kills the kiosk.
  # --term-status-msg= : silences the per-frame terminal status line, which wrote ~40 MB/day
  #   into /tmp/kiosk.log (on the SD card) and buried the actual errors in escape sequences.
  mpv --loop-file=inf --image-display-duration=inf --no-audio --no-osc --no-input-default-bindings \
      --hwdec=no --video-margin-ratio-left=0.62 --background="#0c1018" --alpha=blend --input-ipc-server=/tmp/mpv-kiosk.sock \
      --idle=yes --force-window=yes --term-status-msg= \
      --script="$DIR/kiosk.lua" "$DIR/media/$M"
  rc=$?
  # Why mpv died, kept across reboots. "you fixed it twice and it still crashes" was hard to
  # answer because the only record was a /tmp log that the next start truncated. rc>128 is a
  # signal (135 = SIGBUS, the old short-.bgra crash; 139 = SIGSEGV; 137 = SIGKILL/OOM).
  { printf '%s mpv exited rc=%s' "$(date '+%F %T')" "$rc"
    [ "$rc" -gt 128 ] 2>/dev/null && printf ' (signal %s)' "$((rc - 128))"
    printf ' start-media=%s\n' "$M"
  } >> "$EXITLOG"
  tail -n 200 "$EXITLOG" > "$EXITLOG.$$" && mv "$EXITLOG.$$" "$EXITLOG"
  sleep 2
done
