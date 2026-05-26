#!/bin/bash -eu

log "Archiving through rsync..."

source /root/.teslaCamRsyncConfig

shopt -s nullglob
saved_files=(/mnt/cam/TeslaCam/saved*)

if [ "${#saved_files[@]}" -eq 0 ]
then
  log "No files to archive through rsync."
  exit 0
fi

num_files_moved=$(rsync -auzvh --remove-source-files --no-perms --stats --log-file=/tmp/archive-rsync-cmd.log "${saved_files[@]}" "$user@$server:$path" | awk '/files transferred/{print $NF}')

if [ "$num_files_moved" -gt 0 ]
then
  log "Successfully synced $num_files_moved file(s) through rsync."
else
  log "No files were transferred through rsync."
fi
