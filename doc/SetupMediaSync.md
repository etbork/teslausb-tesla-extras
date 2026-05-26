# Syncing music and Tesla extras

TeslaUSB can optionally copy media files from your archive share onto separate
Tesla-visible media drives. This lets a Windows, macOS, or Samba server act as
the source of truth for files you want the Tesla to see.

For a three-drive layout similar to a manually partitioned drive, set
`campercent` and `soundspercent`. For example, `export campercent=60` and
`export soundspercent=15` creates:

* `TESLADRIVE` for TeslaCam footage
* `TeslaExtras` for lock sounds, light shows, wraps, and license plate art
* `TeslaMedia` for music

## Archive share layout

Create a folder in your archive share named `TeslaUSB-Media`:

```
TeslaUSB-Media/
  TeslaExtras/
    LicensePlate/
      LicensePlate.png
    LightShow/
      show1.fseq
      show1.wav
      show2.fseq
      show2.mp3
    LockChime.wav
    Not in Use Sounds/
      Airplane.wav
    Wraps/
      Robotaxi.png
  TeslaMedia/
    Music/
      song.flac
```

The Pi syncs these items onto the `TeslaExtras` drive:

* `LightShow/` for custom light shows.
* `LockChime.wav` at the root of the extras drive.
* `LicensePlate/`, `Wraps/`, `Not in Use Sounds/`, and `Boombox/` if present.

The Pi syncs `TeslaMedia/Music/` onto the `TeslaMedia` drive as `Music/`.

After syncing, TeslaUSB writes reports back to the archive share:

* `TeslaUSB-Status/status.txt`
* `TeslaUSB-Status/media-sync-report.txt`
* `TeslaUSB-Status/media-warnings.txt`, only when validation finds a problem

If Pushover is configured, media sync does not send a separate success
notification. Instead, the normal TeslaUSB completion notification includes
whether media sync completed, failed, or was not configured.

## Light show rules

The Tesla Light Show folder must be named `LightShow`. Each show needs a
matching `.fseq` sequence file and `.mp3` or `.wav` audio file with the same
base name, for example:

```
LightShow/
  thriller.fseq
  thriller.wav
  holiday.fseq
  holiday.mp3
```

Tesla supports multiple shows on one USB drive on vehicle software 2023.44.25
or newer. The USB root used for light shows must not contain a root-level
`TeslaCam` folder, which is why this feature syncs to the separate
`TeslaExtras` drive.

## Setup variables

Add these variables before running setup:

```
export campercent=60
export soundspercent=15
export CAM_LABEL=TESLADRIVE
export SOUNDS_LABEL=TeslaExtras
export MUSIC_LABEL=TeslaMedia
export MEDIA_SYNC_ENABLED=true
export MEDIA_SYNC_SOURCE_DIR=TeslaUSB-Media
export MEDIA_SYNC_SOUNDS_SOURCE_DIR=TeslaExtras
export MEDIA_SYNC_MUSIC_SOURCE_DIR=TeslaMedia
```

Then return to the main setup instructions.
