#!/bin/bash -eu

log "Moving clips to rclone archive..."

source /root/.teslaCamRcloneConfig

NUM_FILES_MOVED=0

for file_name in "$CAM_MOUNT"/TeslaCam/saved*; do
  [ -e "$file_name" ] || continue
  log "Moving $file_name ..."
  if rclone --config /root/.config/rclone/rclone.conf move "$file_name" "$drive:$path" >> "$LOG_FILE" 2>&1
  then
    log "Moved $file_name."
    NUM_FILES_MOVED=$((NUM_FILES_MOVED + 1))
  else
    log "Failed to move $file_name."
  fi
done
log "Moved $NUM_FILES_MOVED file(s)."

log "Finished moving clips to rclone archive"
