#!/bin/bash -eu

log "Moving clips to archive..."

NUM_FILES_MOVED=0
ARCHIVE_DESTINATION="${ARCHIVE_MOUNT}/TeslaCamArchive"
ARCHIVE_CLIP_FOLDERS="${ARCHIVE_CLIP_FOLDERS:-SavedClips SentryClips RecentClips Photobooth EncryptedClips}"

if [ -r /root/.teslaCamArchiveConfig ]
then
  source /root/.teslaCamArchiveConfig
  ARCHIVE_DESTINATION="${ARCHIVE_MOUNT}/${archivepath:-TeslaCamArchive}"
fi

mkdir -p "$ARCHIVE_DESTINATION"

for clip_folder in $ARCHIVE_CLIP_FOLDERS
do
  source_folder="$CAM_MOUNT/TeslaCam/$clip_folder"
  destination_folder="$ARCHIVE_DESTINATION/$clip_folder"

  if [ ! -d "$source_folder" ]
  then
    continue
  fi

  mkdir -p "$destination_folder"

  for file_name in "$source_folder"/*
  do
    [ -e "$file_name" ] || continue
    log "Moving $file_name ..."

    if mv -f -t "$destination_folder" -- "$file_name" >> "$LOG_FILE" 2>&1
    then
      log "Moved $file_name."
      NUM_FILES_MOVED=$((NUM_FILES_MOVED + 1))
    else
      log "Failed to move $file_name."
    fi
  done
done

log "Moved $NUM_FILES_MOVED file(s)."

log "Finished moving clips to archive."
