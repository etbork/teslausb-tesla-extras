#!/bin/bash -eu

STATUS_DIR="$ARCHIVE_MOUNT/TeslaUSB-Status"
STATUS_FILE="$STATUS_DIR/status.txt"
EVENT_LOG_DEST="$STATUS_DIR/events.log"
SOURCE_EVENT_LOG="${EVENT_LOG:-/mutable/teslausb-events.log}"

mkdir -p "$STATUS_DIR"

function mount_summary () {
  local label="$1"
  local mount_point="$2"

  if findmnt --mountpoint "$mount_point" > /dev/null
  then
    df -h "$mount_point" | awk -v label="$label" 'NR==2 { print label ": " $4 " free of " $2 " (" $5 " used)" }'
  else
    echo "$label: not mounted"
  fi
}

{
  echo "TeslaUSB status"
  echo "Generated: $( date )"
  echo "Hostname: $( hostname )"
  echo "Uptime: $( uptime -p 2>/dev/null || uptime )"
  echo ""
  mount_summary "CAM" "$CAM_MOUNT"
  mount_summary "TeslaExtras" "$SOUNDS_MOUNT"
  mount_summary "MUSIC" "$MUSIC_MOUNT"
  mount_summary "Archive" "$ARCHIVE_MOUNT"
  echo ""
  echo "Recent events:"
  if [ -r "$SOURCE_EVENT_LOG" ]
  then
    tail -n 30 "$SOURCE_EVENT_LOG"
  else
    echo "No events logged yet."
  fi
} > "$STATUS_FILE"

if [ -r "$SOURCE_EVENT_LOG" ]
then
  cp "$SOURCE_EVENT_LOG" "$EVENT_LOG_DEST"
fi

log "Wrote status report to $STATUS_FILE."
