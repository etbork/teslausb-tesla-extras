#!/bin/bash -eu

LOW_SPACE_PERCENT=${LOW_SPACE_PERCENT:-90}
LOW_SPACE_CHECK_CAM=${LOW_SPACE_CHECK_CAM:-false}

function check_mount_space () {
  local label="$1"
  local mount_point="$2"

  if ! findmnt --mountpoint "$mount_point" > /dev/null
  then
    return
  fi

  local used_percent
  used_percent="$( df -P "$mount_point" | awk 'NR==2 { gsub("%", "", $5); print $5 }' )"

  if [ "$used_percent" -ge "$LOW_SPACE_PERCENT" ]
  then
    local free_space
    free_space="$( df -h "$mount_point" | awk 'NR==2 { print $4 }' )"
    local message="$label is ${used_percent}% full. Free space: $free_space."
    log "$message"
    /root/bin/send-pushover "TeslaUSB Low Space" "$message" "0" || true
  fi
}

if [ "$LOW_SPACE_CHECK_CAM" = "true" ]
then
  check_mount_space "CAM" "$CAM_MOUNT"
fi

check_mount_space "MUSIC" "$MUSIC_MOUNT"
check_mount_space "TeslaExtras" "$SOUNDS_MOUNT"
check_mount_space "Archive" "$ARCHIVE_MOUNT"
