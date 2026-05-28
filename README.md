# TeslaUSB Tesla Extras

This project is a personal fork of [cimryan/teslausb](https://github.com/cimryan/teslausb). It keeps the core TeslaUSB idea: a Raspberry Pi Zero/Zero W/Zero 2 W pretends to be one or more USB drives for a Tesla using Linux USB gadget mode and FAT32 backing files.

This fork pivots the transfer workflow to a Pi-hosted local web portal. The Pi can create its own Wi-Fi hotspot, serve a browser UI, and let a phone or computer browse, download, and upload TeslaCam/TeslaExtras/TeslaMedia files directly.

## What This Fork Does

- Presents Tesla-visible USB storage from Raspberry Pi backing files.
- Optionally exposes three Tesla-visible drives: dashcam, extras, and music.
- Hosts a local TeslaUSB web portal over a Pi-created Wi-Fi hotspot.
- Lets users explicitly start and end transfer sessions from the web UI.
- Supports browsing and downloading TeslaCam clips.
- Supports uploading lock chimes, light shows, wraps, license plate images, Boombox sounds, and music.
- Adds compatibility fixes for newer Raspberry Pi OS boot paths and rerunning setup.

## Recommended Hardware

- Raspberry Pi Zero 2 W.
- High endurance microSD card, 128 GB or 256 GB.
- A known-good data-capable USB cable.
- Optional second USB power cable if the car data port does not power the Pi reliably.

Important: on a Pi Zero / Zero 2 W, the data cable must go into the Pi port labeled `USB`, not `PWR IN`. If the Pi is powered through `PWR IN`, it may boot and join Wi-Fi, but the car will not see any USB drives.

## Drive Layout

This fork can expose up to three Tesla-visible drives:

| Drive label | Backing file | Purpose |
| --- | --- | --- |
| `TESLADRIVE` | `cam_disk.bin` | TeslaCam dashcam storage |
| `TeslaExtras` | `sounds_disk.bin` | Light shows, lock chime, wraps, license plate art, Boombox |
| `TeslaMedia` | `music_disk.bin` | Music library |

These are not physical partitions on the SD card. They are large FAT32 backing files stored on the Pi and exposed to the Tesla through USB gadget mode.

## TeslaExtras Layout

`TeslaExtras` is intended for files that are not normal music:

- `LightShow/` for `.fseq` files and matching `.mp3` or `.wav` audio.
- `LockChime.wav` at the root of `TeslaExtras`.
- `Wraps/` for wrap images.
- `LicensePlate/LicensePlate.png`.
- `Boombox/` for Boombox files if used.

## TeslaMedia Layout

Music belongs under:

```text
TeslaMedia/
  Music/
    Song.flac
```

FLAC files work well for lossless storage. Keep filenames simple where possible. FAT32 can reject some characters, especially smart quotes and other special Unicode punctuation.

## Web Portal

The portal uses an explicit transfer session. Starting a session disconnects the USB mass-storage gadget from the Tesla, mounts the backing files locally, and exposes them in the web UI. Ending the session syncs and unmounts the backing files before reconnecting the Tesla-visible USB drives.

Example portal configuration:

```sh
export campercent=60
export soundspercent=15

export CAM_LABEL=TESLADRIVE
export SOUNDS_LABEL=TeslaExtras
export MUSIC_LABEL=TeslaMedia

export PORTAL_ENABLED=true
export PORTAL_WIFI_SSID=TeslaUSB
export PORTAL_WIFI_PASSWORD=teslausbportal
export PORTAL_ADDRESS=192.168.50.1
export PORTAL_UPLOADS_ENABLED=true
```

After boot, join the `TeslaUSB` Wi-Fi network and open:

```text
http://teslausb.local
http://192.168.50.1
```

## Installation Overview

Flash Raspberry Pi OS Lite to the SD card, enable SSH, configure Wi-Fi, then run the TeslaUSB setup script from this fork:

```sh
wget https://raw.githubusercontent.com/etbork/teslausb-tesla-extras/codex/teslausb-tesla-extras/setup/pi/setup-teslausb
chmod +x setup-teslausb
sudo ./setup-teslausb
```

If the repository is private, fresh installs need a different download method, such as cloning with GitHub authentication or copying the setup script locally.

## Operational Notes

- Start a portal transfer session only when the Tesla does not need active access to the USB drives.
- The transfer session disconnects the USB drives from the Tesla while it mounts the backing files internally.
- End the transfer session when finished so the Pi can reconnect the Tesla-visible drives.
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
- No portal transfer session is active.

The USB controller state can be checked with:

```sh
cat /sys/class/udc/*/state
```

If it says `not attached`, the Pi does not detect the car as a USB host.

### Lock chime warning

Place the file here:

```text
TeslaExtras/LockChime.wav
```

The filename must be exactly `LockChime.wav`.

## Security Notes

Do not commit local configuration files with:

- Wi-Fi credentials.
- Private GitHub tokens.

The portal is intended for local-network use only.

## License And Upstream

This fork is based on [cimryan/teslausb](https://github.com/cimryan/teslausb). See the upstream project for original history, design, and license context.
