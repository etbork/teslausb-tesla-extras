# TeslaUSB Tesla Extras

This project is a personal fork of [cimryan/teslausb](https://github.com/cimryan/teslausb). It keeps the original TeslaUSB idea: a Raspberry Pi Zero/Zero W/Zero 2 W pretends to be one or more USB drives for a Tesla, records dashcam footage, and archives clips to a network share when the archive server is reachable.

This fork adds a more complete Tesla media setup for modern Tesla USB use: dashcam footage, lossless music, light shows, lock chimes, wraps, license plate art, folder-based archive organization, Pushover status notifications, and a configurable delay before the Pi disconnects the USB drives to sync.

## What This Fork Does

- Presents Tesla-visible USB storage from Raspberry Pi backing files.
- Archives TeslaCam footage to a Windows/macOS/Linux SMB share.
- Archives TeslaCam clips into separate folders such as `RecentClips`, `SavedClips`, `SentryClips`, `Photobooth`, and `EncryptedClips`.
- Syncs music from a network share to a Tesla-visible `TeslaMedia` drive.
- Syncs Tesla extras from a network share to a Tesla-visible `TeslaExtras` drive.
- Supports light shows, lock chimes, wraps, license plate art, Boombox files, and legacy `SOUNDS_LS` layouts.
- Generates status and media reports on the archive share.
- Sends Pushover notifications for sync completion, archive failures, media sync failures, and media warnings.
- Adds a configurable archive delay so the Pi does not immediately yank the USB drive away from the car as soon as the archive share is reachable.
- Adds compatibility fixes for newer Raspberry Pi OS boot paths and rerunning setup.

## Based On The Original TeslaUSB

Credit goes to the original TeslaUSB project:

[https://github.com/cimryan/teslausb](https://github.com/cimryan/teslausb)

The original project already handled the hard part: using Linux USB gadget mode and `g_mass_storage` so a Raspberry Pi Zero can look like a USB drive to the Tesla. This fork keeps that approach and extends it for a multi-drive Tesla media workflow.

## Recommended Hardware

- Raspberry Pi Zero 2 W.
- High endurance microSD card, 128 GB or 256 GB.
- A known-good data-capable USB cable.
- Optional second USB power cable if the car data port does not power the Pi reliably.
- A Windows/macOS/Linux machine that stays on and hosts the SMB archive/media share.

Important: on a Pi Zero / Zero 2 W, the data cable must go into the Pi port labeled `USB`, not `PWR IN`. If the Pi is powered through `PWR IN`, it may boot and join Wi-Fi, but the car will not see any USB drives.

Also recommended: reserve the Pi's IP address in your router/DHCP server. A router-side DHCP reservation is preferred over hard-coding a static IP on the Pi because it keeps the Pi portable while still giving Apple Shortcuts, Home Assistant, and SSH automations a stable target.

## Drive Layout

This fork can expose up to three Tesla-visible drives:

| Drive label | Backing file | Purpose |
| --- | --- | --- |
| `TESLADRIVE` | `cam_disk.bin` | TeslaCam dashcam storage |
| `TeslaExtras` | `sounds_disk.bin` | Light shows, lock chime, wraps, license plate art, Boombox |
| `TeslaMedia` | `music_disk.bin` | Music library |

These are not physical partitions on the SD card. They are large FAT32 backing files stored on the Pi and exposed to the Tesla through USB gadget mode.

## Archive Share Layout

The archive server/share should contain a layout like this:

```text
TeslaUSB/
  TeslaCamArchive/
    RecentClips/
    SavedClips/
    SentryClips/
    Photobooth/
    EncryptedClips/
  TeslaUSB-Media/
    TeslaExtras/
      LicensePlate/
        LicensePlate.png
      LightShow/
        Example.fseq
        Example.mp3
      Wraps/
        Example.png
      Boombox/
      LockChime.wav
    TeslaMedia/
      Music/
        Song.flac
  TeslaUSB-Status/
    status.txt
    media-sync-report.txt
    media-warnings.txt
```

### TeslaExtras

`TeslaExtras` is intended for files that are not normal music:

- `LightShow/` for `.fseq` files and matching `.mp3` or `.wav` audio.
- `LockChime.wav` at the root of `TeslaExtras`.
- `Wraps/` for wrap images.
- `LicensePlate/LicensePlate.png`.
- `Boombox/` for Boombox files if used.

Legacy layouts are still tolerated. If the sync source has `LightShow` or `SOUNDS_LS` directly under the media folder, the sync script can fall back to those.

### TeslaMedia

Music belongs under:

```text
TeslaUSB-Media/TeslaMedia/Music/
```

FLAC files work well for lossless storage. Keep filenames simple where possible. FAT32 can reject some characters, especially smart quotes and other special Unicode punctuation.

## Key Improvements In This Fork

### Media Sync

The original project could store music, but this fork adds server-managed media sync. You can update music, light shows, wraps, and lock chimes on the archive share from another device, then the Pi copies those files onto the Tesla-visible USB drives during sync.

### Separate Extras Drive

This fork supports a dedicated extras drive using `soundspercent`, which keeps TeslaCam, music, and extras organized separately.

### Folder-Based TeslaCam Archive

Clips are archived into matching folders instead of one mixed directory:

- `RecentClips`
- `SavedClips`
- `SentryClips`
- `Photobooth`
- `EncryptedClips`

### Pushover Notifications

When configured, the Pi can send notifications for:

- Archive complete.
- Archive failed.
- Media sync failed.
- Media warnings.
- Sync complete with media sync status.

### Media Reports

The Pi writes report files to `TeslaUSB-Status` on the archive share:

- `status.txt`
- `media-sync-report.txt`
- `media-warnings.txt`

These are useful when checking whether music, light shows, and lock chimes synced correctly.

### Configurable Archive Delay

This fork supports:

```sh
ARCHIVE_START_DELAY_SECONDS=300
```

That waits 5 minutes before the Pi disconnects the Tesla-visible USB drives and starts archiving. This helps avoid grabbing the drive immediately while the car is still awake or writing clips.

### Newer Raspberry Pi OS Compatibility

This fork includes setup fixes for newer Raspberry Pi OS images:

- Modern `/boot/firmware` boot paths.
- Missing legacy `/etc/rc.local`.
- Missing legacy setup LED/progress helpers.
- Rerunning setup without duplicating entries.
- Backing files stored on the root filesystem when a separate backing partition is not used.
- Branch names encoded correctly for raw GitHub script downloads.

## Example Setup Variables

For a three-drive setup:

```sh
export ARCHIVE_SYSTEM=cifs
export archiveserver=192.168.68.69
export sharename=TeslaUSB
export shareuser=teslausb
export sharepassword='your-password'

export campercent=60
export soundspercent=15

export CAM_LABEL=TESLADRIVE
export SOUNDS_LABEL=TeslaExtras
export MUSIC_LABEL=TeslaMedia

export ARCHIVE_CLIP_FOLDERS="SavedClips SentryClips RecentClips Photobooth EncryptedClips"

export MEDIA_SYNC_ENABLED=true
export MEDIA_SYNC_SOURCE_DIR=TeslaUSB-Media
export MEDIA_SYNC_SOUNDS_SOURCE_DIR=TeslaExtras
export MEDIA_SYNC_MUSIC_SOURCE_DIR=TeslaMedia

export pushover_enabled=true
export pushover_user_key='your-user-key'
export pushover_app_key='your-app-key'
```

The live 5-minute delay is configured through systemd:

```ini
[Service]
Environment=ARCHIVE_START_DELAY_SECONDS=300
```

Manual sync can be triggered with:

```sh
sudo teslausb-sync-now
```

## Installation Overview

Flash Raspberry Pi OS Lite to the SD card, enable SSH, configure Wi-Fi, then run the TeslaUSB setup script from this fork:

```sh
wget https://raw.githubusercontent.com/etbork/teslausb-tesla-extras/codex/teslausb-tesla-extras/setup/pi/setup-teslausb
chmod +x setup-teslausb
sudo ./setup-teslausb
```

If the repository is private, fresh installs need a different download method, such as cloning with GitHub authentication or copying the setup script locally.

## Apple Shortcut Manual Sync

This fork installs a helper command for manual sync requests:

```sh
sudo teslausb-sync-now
```

That command creates the TeslaUSB archive trigger and restarts the archive loop. It still respects `ARCHIVE_START_DELAY_SECONDS`, so with a 300-second delay the Pi waits 5 minutes before disconnecting the Tesla-visible drives and archiving.

To trigger it from an iPhone:

1. Create a Shortcut.
2. Add **Run Script Over SSH**.
3. Host: the Pi's reserved IP address, for example `192.168.68.51`.
4. User: `pi`.
5. Authentication: your Pi password or SSH key.
6. Script:

```sh
sudo teslausb-sync-now
```

Name the Shortcut something like `Sync TeslaUSB`, then run it from the Shortcuts app, a Home Screen icon, or Siri.

## Operational Notes

- The Pi must be on Wi-Fi to reach the SMB archive share.
- Reserve the Pi's IP address in the router so phone shortcuts and automations can reliably reach it.
- The archive cycle disconnects the USB drives from the Tesla while it mounts the backing files internally.
- If the Tesla is still actively writing, archiving too soon can look strange in the car UI. Use `ARCHIVE_START_DELAY_SECONDS` to add a buffer.
- TeslaUSB expects a reachable/unreachable archive cycle. In the original design, the car leaves home Wi-Fi, then returns. If the car stays home and Sentry keeps it awake, the Pi may stay powered and reachable for long periods.
- Some Tesla USB ports are power-only. The Pi can be online but invisible to the car if the data cable is in the wrong port or plugged into the Pi's `PWR IN` port.
- On the Pi Zero 2 W, the car data cable must use the Pi's `USB` port.

## Troubleshooting

### The Pi is online but the car sees no USB drives

Check:

- Cable is data-capable.
- Cable is plugged into the Pi port labeled `USB`.
- Car port supports USB data.
- `dtoverlay=dwc2,dr_mode=peripheral` is present in `/boot/firmware/config.txt`.
- `g_mass_storage` is loaded.

The USB controller state can be checked with:

```sh
cat /sys/class/udc/*/state
```

If it says `not attached`, the Pi does not detect the car as a USB host.

### Music or media sync fails

Check `TeslaUSB-Status/media-sync-report.txt` and `TeslaUSB-Status/media-warnings.txt` on the archive share.

FAT32 can reject some filenames. Avoid smart quotes and unusual punctuation in music filenames.

### Lock chime warning

Place the file here:

```text
TeslaUSB-Media/TeslaExtras/LockChime.wav
```

The filename must be exactly `LockChime.wav`.

### Slow archiving

Archiving can be slow because data travels through several layers:

```text
Tesla writes to USB gadget -> Pi SD backing file -> Pi mounts FAT image -> Wi-Fi -> SMB share
```

The Pi Zero 2 W is small and Wi-Fi/SMB/FAT image operations can be slow, especially for video clips.

## Security Notes

Do not commit local configuration files with:

- Wi-Fi credentials.
- SMB passwords.
- Pushover tokens.
- Private GitHub tokens.

Use a dedicated archive-share user with access only to the TeslaUSB share.

## License And Upstream

This fork is based on [cimryan/teslausb](https://github.com/cimryan/teslausb). See the upstream project for original history, design, and license context.
