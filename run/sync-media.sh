#!/bin/bash -eu

CONFIG_FILE=/root/.teslaMediaSyncConfig

if [ ! -r "$CONFIG_FILE" ]
then
  log "Media sync is not configured."
  exit 0
fi

source "$CONFIG_FILE"

MEDIA_SYNC_ENABLED=${MEDIA_SYNC_ENABLED:-false}
MEDIA_SYNC_SOURCE_DIR=${MEDIA_SYNC_SOURCE_DIR:-TeslaUSB-Media}
MEDIA_SYNC_SOUNDS_SOURCE_DIR=${MEDIA_SYNC_SOUNDS_SOURCE_DIR:-TeslaExtras}
MEDIA_SYNC_MUSIC_SOURCE_DIR=${MEDIA_SYNC_MUSIC_SOURCE_DIR:-TeslaMedia}

if [ "$MEDIA_SYNC_ENABLED" != "true" ]
then
  log "Media sync is disabled."
  exit 0
fi

function validate_relative_path () {
  local path_value="$1"
  local path_name="$2"

  case "$path_value" in
    /*|*..*)
      log "Invalid $path_name: $path_value"
      exit 1
      ;;
  esac
}

validate_relative_path "$MEDIA_SYNC_SOURCE_DIR" "MEDIA_SYNC_SOURCE_DIR"
validate_relative_path "$MEDIA_SYNC_SOUNDS_SOURCE_DIR" "MEDIA_SYNC_SOUNDS_SOURCE_DIR"
validate_relative_path "$MEDIA_SYNC_MUSIC_SOURCE_DIR" "MEDIA_SYNC_MUSIC_SOURCE_DIR"

if [ ! -e "$SOUNDS_MOUNT" ] && [ ! -e "$MUSIC_MOUNT" ]
then
  log "Media sync requires at least one sounds or music partition. Set campercent below 100."
  exit 0
fi

SOURCE_DIR="$ARCHIVE_MOUNT/$MEDIA_SYNC_SOURCE_DIR"
SOUNDS_SOURCE_DIR="$SOURCE_DIR/$MEDIA_SYNC_SOUNDS_SOURCE_DIR"
MUSIC_SOURCE_DIR="$SOURCE_DIR/$MEDIA_SYNC_MUSIC_SOURCE_DIR"

if [ ! -d "$SOUNDS_SOURCE_DIR" ] && [ -d "$SOURCE_DIR/LightShow" ]
then
  SOUNDS_SOURCE_DIR="$SOURCE_DIR"
fi

if [ ! -d "$SOUNDS_SOURCE_DIR" ] && [ -d "$SOURCE_DIR/SOUNDS_LS" ]
then
  SOUNDS_SOURCE_DIR="$SOURCE_DIR/SOUNDS_LS"
fi

if [ ! -d "$MUSIC_SOURCE_DIR" ] && [ -d "$SOURCE_DIR/Music" ]
then
  MUSIC_SOURCE_DIR="$SOURCE_DIR"
fi

SOUNDS_MOUNTED_BY_SYNC=false
MUSIC_MOUNTED_BY_SYNC=false

if [ ! -d "$SOURCE_DIR" ]
then
  log "Media sync source does not exist: $SOURCE_DIR"
  exit 0
fi

function require_mount_path () {
  local mount_point="$1"
  local description="$2"

  if [ ! -e "$mount_point" ]
  then
    log "Skipping $description sync because $mount_point does not exist."
    return 1
  fi

  return 0
}

function ensure_media_mount () {
  local mount_point="$1"
  local description="$2"

  if ! require_mount_path "$mount_point" "$description"
  then
    return 1
  fi

  if ! findmnt --mountpoint "$mount_point" > /dev/null
  then
    case "$mount_point" in
      "$SOUNDS_MOUNT")
        SOUNDS_MOUNTED_BY_SYNC=true
        ;;
      "$MUSIC_MOUNT")
        MUSIC_MOUNTED_BY_SYNC=true
        ;;
    esac
  fi

  ensure_mountpoint_is_mounted_with_retry "$mount_point"
  return 0
}

function cleanup () {
  if [ "$SOUNDS_MOUNTED_BY_SYNC" = "true" ] && findmnt --mountpoint "$SOUNDS_MOUNT" > /dev/null
  then
    unmount_sounds_file || true
  fi

  if [ "$MUSIC_MOUNTED_BY_SYNC" = "true" ] && findmnt --mountpoint "$MUSIC_MOUNT" > /dev/null
  then
    unmount_music_file || true
  fi
}

trap cleanup EXIT

function copy_path_if_present () {
  local source_path="$1"
  local destination_path="$2"

  if [ ! -e "$source_path" ]
  then
    return
  fi

  log "Syncing $source_path to $destination_path..."
  rm -rf "$destination_path"
  cp -a "$source_path" "$destination_path"
}

function sync_sounds_drive () {
  if [ ! -d "$SOUNDS_SOURCE_DIR" ]
  then
    log "Sounds sync source does not exist: $SOUNDS_SOURCE_DIR"
    return
  fi

  if ! ensure_media_mount "$SOUNDS_MOUNT" "sounds"
  then
    return
  fi

  copy_path_if_present "$SOUNDS_SOURCE_DIR/LicensePlate" "$SOUNDS_MOUNT/LicensePlate"
  copy_path_if_present "$SOUNDS_SOURCE_DIR/LightShow" "$SOUNDS_MOUNT/LightShow"
  copy_path_if_present "$SOUNDS_SOURCE_DIR/Boombox" "$SOUNDS_MOUNT/Boombox"
  copy_path_if_present "$SOUNDS_SOURCE_DIR/Not in Use Sounds" "$SOUNDS_MOUNT/Not in Use Sounds"
  copy_path_if_present "$SOUNDS_SOURCE_DIR/Wraps" "$SOUNDS_MOUNT/Wraps"

  if [ -f "$SOUNDS_SOURCE_DIR/LockChime.wav" ]
  then
    copy_path_if_present "$SOUNDS_SOURCE_DIR/LockChime.wav" "$SOUNDS_MOUNT/LockChime.wav"
  elif [ -f "$SOUNDS_SOURCE_DIR/Boombox/LockChime.wav" ]
  then
    copy_path_if_present "$SOUNDS_SOURCE_DIR/Boombox/LockChime.wav" "$SOUNDS_MOUNT/LockChime.wav"
  fi
}

function sync_music_drive () {
  if [ ! -d "$MUSIC_SOURCE_DIR" ]
  then
    log "Music sync source does not exist: $MUSIC_SOURCE_DIR"
    return
  fi

  if ! ensure_media_mount "$MUSIC_MOUNT" "music"
  then
    return
  fi

  copy_path_if_present "$MUSIC_SOURCE_DIR/Music" "$MUSIC_MOUNT/Music"
}

log "Starting media sync."

sync_sounds_drive
sync_music_drive

if [ -x /root/bin/validate-media.sh ]
then
  /root/bin/validate-media.sh || log "Media validation failed."
fi

sync

log "Finished media sync."
