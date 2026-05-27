#!/bin/bash -eu

CAM_PERCENT="$1"
BACKINGFILES_MOUNTPOINT="$2"
SOUNDS_PERCENT="${soundspercent:-0}"
BACKINGFILES_RESERVE_MB="${BACKINGFILES_RESERVE_MB:-2048}"

CAM_LABEL="${CAM_LABEL:-CAM}"
SOUNDS_LABEL="${SOUNDS_LABEL:-TeslaExtras}"
MUSIC_LABEL="${MUSIC_LABEL:-MUSIC}"

G_MASS_STORAGE_CONF_FILE_NAME=/etc/modprobe.d/g_mass_storage.conf

function add_drive () {
  local name="$1"
  local label="$2"
  local size="$3"

  local filename="$4"
  echo "Allocating ${size}K for $filename..."
  fallocate -l "$size"K "$filename"
  mkfs.vfat "$filename" -F 32 -n "$label"

  local mountpoint=/mnt/"$name"

  mkdir -p "$mountpoint"
  echo "$filename $mountpoint vfat noauto,users,umask=000 0 0" >> /etc/fstab
}

function create_teslacam_directory () {
  mount /mnt/cam
  mkdir /mnt/cam/TeslaCam
  umount /mnt/cam
}

FREE_1K_BLOCKS="$(df --output=avail --block-size=1K $BACKINGFILES_MOUNTPOINT/ | tail -n 1)"
RESERVED_1K_BLOCKS="$(( BACKINGFILES_RESERVE_MB * 1024 ))"
if [ "$FREE_1K_BLOCKS" -le "$RESERVED_1K_BLOCKS" ]
then
  echo "STOP: Not enough free space to reserve ${BACKINGFILES_RESERVE_MB}MB for the operating system."
  exit 1
fi
FREE_1K_BLOCKS="$(( FREE_1K_BLOCKS - RESERVED_1K_BLOCKS ))"

CAM_DISK_SIZE="$(( $FREE_1K_BLOCKS * $CAM_PERCENT / 100 ))"
CAM_DISK_FILE_NAME="$BACKINGFILES_MOUNTPOINT/cam_disk.bin"
add_drive "cam" "$CAM_LABEL" "$CAM_DISK_SIZE" "$CAM_DISK_FILE_NAME"

if [ "$SOUNDS_PERCENT" -gt 0 ]
then
  SOUNDS_DISK_SIZE="$(( $FREE_1K_BLOCKS * $SOUNDS_PERCENT / 100 ))"
  SOUNDS_DISK_FILE_NAME="$BACKINGFILES_MOUNTPOINT/sounds_disk.bin"
  MUSIC_DISK_SIZE="$(( $FREE_1K_BLOCKS - $CAM_DISK_SIZE - $SOUNDS_DISK_SIZE ))"
  MUSIC_DISK_FILE_NAME="$BACKINGFILES_MOUNTPOINT/music_disk.bin"

  add_drive "sounds" "$SOUNDS_LABEL" "$SOUNDS_DISK_SIZE" "$SOUNDS_DISK_FILE_NAME"
  add_drive "music" "$MUSIC_LABEL" "$MUSIC_DISK_SIZE" "$MUSIC_DISK_FILE_NAME"
  echo "options g_mass_storage file=$CAM_DISK_FILE_NAME,$SOUNDS_DISK_FILE_NAME,$MUSIC_DISK_FILE_NAME removable=1,1,1 ro=0,0,0 stall=0 iSerialNumber=123456" > "$G_MASS_STORAGE_CONF_FILE_NAME"
elif [ "$CAM_PERCENT" -lt 100 ]
then
  MUSIC_DISK_SIZE="$(df --output=avail --block-size=1K $BACKINGFILES_MOUNTPOINT/ | tail -n 1)"
  MUSIC_DISK_FILE_NAME="$BACKINGFILES_MOUNTPOINT/music_disk.bin"
  add_drive "music" "$MUSIC_LABEL" "$MUSIC_DISK_SIZE" "$MUSIC_DISK_FILE_NAME"
  echo "options g_mass_storage file=$MUSIC_DISK_FILE_NAME,$CAM_DISK_FILE_NAME removable=1,1 ro=0,0 stall=0 iSerialNumber=123456" > "$G_MASS_STORAGE_CONF_FILE_NAME"
else
  echo "options g_mass_storage file=$CAM_DISK_FILE_NAME removable=1 ro=0 stall=0 iSerialNumber=123456" > "$G_MASS_STORAGE_CONF_FILE_NAME"
fi

create_teslacam_directory
