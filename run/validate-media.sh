#!/bin/bash -eu

STATUS_DIR="$ARCHIVE_MOUNT/TeslaUSB-Status"
REPORT_FILE="$STATUS_DIR/media-sync-report.txt"
WARNINGS_FILE="$STATUS_DIR/media-warnings.txt"

mkdir -p "$STATUS_DIR"
: > "$REPORT_FILE"
: > "$WARNINGS_FILE"

function report () {
  echo "$1" >> "$REPORT_FILE"
}

function warn () {
  echo "$1" >> "$WARNINGS_FILE"
  report "WARNING: $1"
}

report "TeslaUSB media report"
report "Generated: $( date )"
report ""

if [ -d "$SOUNDS_MOUNT/LightShow" ]
then
  report "LightShow files:"
  find "$SOUNDS_MOUNT/LightShow" -maxdepth 1 -type f | sort | sed "s#^$SOUNDS_MOUNT/LightShow/##" >> "$REPORT_FILE"
  report ""

  shopt -s nullglob
  for sequence_file in "$SOUNDS_MOUNT"/LightShow/*.fseq
  do
    base_name="${sequence_file%.*}"
    if [ ! -f "$base_name.mp3" ] && [ ! -f "$base_name.wav" ]
    then
      warn "$( basename "$sequence_file" ) does not have a matching .mp3 or .wav file."
    fi
  done

  for audio_file in "$SOUNDS_MOUNT"/LightShow/*.mp3 "$SOUNDS_MOUNT"/LightShow/*.wav
  do
    base_name="${audio_file%.*}"
    if [ ! -f "$base_name.fseq" ]
    then
      warn "$( basename "$audio_file" ) does not have a matching .fseq file."
    fi
  done
else
  report "No LightShow folder found."
fi

report ""

if [ -d "$SOUNDS_MOUNT/Boombox" ]
then
  report "Boombox files:"
  find "$SOUNDS_MOUNT/Boombox" -maxdepth 1 -type f | sort | sed "s#^$SOUNDS_MOUNT/Boombox/##" >> "$REPORT_FILE"
else
  report "No Boombox folder found."
fi

report ""

if [ -f "$SOUNDS_MOUNT/LockChime.wav" ]
then
  report "LockChime.wav found at extras drive root."
else
  warn "LockChime.wav was not found at the extras drive root."
fi

report ""

if [ -d "$MUSIC_MOUNT/Music" ]
then
  report "Music files:"
  find "$MUSIC_MOUNT/Music" -maxdepth 1 -type f | sort | sed "s#^$MUSIC_MOUNT/Music/##" >> "$REPORT_FILE"
else
  report "No Music folder found."
fi

if [ -s "$WARNINGS_FILE" ]
then
  warning_count="$( wc -l < "$WARNINGS_FILE" | tr -d ' ' )"
  /root/bin/send-pushover "TeslaUSB Media Warning" "$warning_count media warning(s). See TeslaUSB-Status/media-warnings.txt." "0" || true
else
  rm -f "$WARNINGS_FILE"
fi

log "Wrote media report to $REPORT_FILE."
