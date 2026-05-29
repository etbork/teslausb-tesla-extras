#!/usr/bin/env python3

import html
import json
import mimetypes
import os
import posixpath
import re
import shutil
import struct
import subprocess
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

SESSION_HELPER = os.environ.get("PORTAL_SESSION_HELPER", "/usr/local/bin/teslausb-portal-session")
HOST = os.environ.get("PORTAL_BIND_HOST", "0.0.0.0")
PORT = int(os.environ.get("PORTAL_PORT", "80"))
UPLOADS_ENABLED = os.environ.get("PORTAL_UPLOADS_ENABLED", "true").lower() == "true"
DELETES_ENABLED = os.environ.get("PORTAL_DELETES_ENABLED", "false").lower() == "true"
LOG_FILES = ["/mutable/portal.log"]
HOSTAPD_CONF = Path(os.environ.get("PORTAL_HOSTAPD_CONF", "/etc/hostapd/hostapd.conf"))
SESSION_TIMEOUT_SECONDS = int(os.environ.get("PORTAL_SESSION_TIMEOUT_SECONDS", "300"))
SESSION_EXTEND_SECONDS = int(os.environ.get("PORTAL_SESSION_EXTEND_SECONDS", str(SESSION_TIMEOUT_SECONDS)))
SESSION_DEADLINE_FILE = Path(os.environ.get("PORTAL_SESSION_DEADLINE_FILE", "/run/teslausb-portal-session.deadline"))
SESSION_DEADLINE = 0.0
SESSION_DEADLINE_LOCK = threading.Lock()

DRIVES = {
    "cam": {"label": "TESLADRIVE", "title": "Dash cam", "root": Path("/mnt/cam"), "home_path": "TeslaCam"},
    "sounds": {"label": "TeslaExtras", "title": "Extras", "root": Path("/mnt/sounds"), "home_path": ""},
    "music": {"label": "TeslaMedia", "title": "Music", "root": Path("/mnt/music"), "home_path": "Music"},
}

UPLOAD_TARGETS = {
    "lockchime": {"label": "Lock chime", "drive": "sounds", "dir": "", "extensions": {".wav"}, "fixed_name": "LockChime.wav"},
    "lightshow": {"label": "Light show", "drive": "sounds", "dir": "LightShow", "extensions": {".fseq", ".mp3", ".wav"}},
    "wrap": {"label": "Wrap", "drive": "sounds", "dir": "Wraps", "extensions": {".png", ".jpg", ".jpeg"}},
    "licenseplate": {"label": "License plate", "drive": "sounds", "dir": "LicensePlate", "extensions": {".png"}, "fixed_name": "LicensePlate.png"},
    "boombox": {"label": "Boombox", "drive": "sounds", "dir": "Boombox", "extensions": {".mp3", ".wav"}},
    "music": {"label": "Music", "drive": "music", "dir": "Music", "extensions": {".mp3", ".wav", ".flac", ".m4a", ".aac"}},
}

SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._()+-]{0,120}$")
MAX_ART_BYTES = 8 * 1024 * 1024
SAFE_SSID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,31}$")


def run_helper(action):
    completed = subprocess.run(
        [SESSION_HELPER, action],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stdout.strip() or f"{SESSION_HELPER} {action} failed")
    return completed.stdout


def format_bytes(value):
    if value is None:
        return "unknown"
    units = ["B", "KB", "MB", "GB", "TB"]
    size = float(value)
    for unit in units:
        if size < 1024 or unit == units[-1]:
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024


def safe_join(root, rel):
    rel = unquote(rel or "")
    if any(part == ".." for part in rel.replace("\\", "/").split("/")):
        raise ValueError("Invalid path.")
    rel = posixpath.normpath("/" + rel).lstrip("/")
    target = (root / rel).resolve()
    root_resolved = root.resolve()
    if target != root_resolved and root_resolved not in target.parents:
        raise ValueError("Invalid path.")
    return target, rel


def validate_filename(name, allowed_extensions, fixed_name=None):
    if "/" in (name or "") or "\\" in (name or ""):
        raise ValueError("Filename must not include a path.")
    base = os.path.basename(name or "")
    if fixed_name:
        base = fixed_name
    if not SAFE_NAME.match(base):
        raise ValueError("Filename uses unsupported characters. Avoid smart quotes and special punctuation.")
    suffix = Path(base).suffix.lower()
    if suffix not in allowed_extensions:
        raise ValueError(f"Unsupported file type: {suffix or '(none)'}")
    return base


def pi_serial():
    try:
        for line in Path("/proc/cpuinfo").read_text(errors="replace").splitlines():
            if line.lower().startswith("serial"):
                serial = line.split(":", 1)[1].strip()
                if serial:
                    return serial
    except OSError:
        pass
    return ""


def default_hotspot_ssid():
    serial = pi_serial()
    return f"Glovebox-{serial}" if serial else "Glovebox"


def default_hotspot_password():
    serial = pi_serial()
    return serial[-8:] if len(serial) >= 8 else "00000000"


def read_hostapd_config():
    data = {}
    try:
        for raw_line in HOSTAPD_CONF.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            data[key.strip()] = value.strip()
    except OSError:
        pass
    return data


def hotspot_status():
    config = read_hostapd_config()
    serial = pi_serial()
    return {
        "serial": serial,
        "ssid": config.get("ssid") or default_hotspot_ssid(),
        "default_ssid": default_hotspot_ssid(),
        "default_password_hint": default_hotspot_password(),
        "password_min_length": 8,
    }


def validate_hotspot_settings(ssid, password=None):
    ssid = str(ssid or "").strip()
    if not SAFE_SSID.match(ssid):
        raise ValueError("Hotspot name must be 1-32 characters and use letters, numbers, spaces, dots, dashes, or underscores.")
    password = None if password is None else str(password)
    if password:
        if len(password) < 8 or len(password) > 63:
            raise ValueError("Hotspot password must be 8-63 characters for WPA2.")
        if any(ord(ch) < 32 or ord(ch) > 126 for ch in password):
            raise ValueError("Hotspot password must use standard printable characters.")
    return ssid, password


def update_hotspot_settings(ssid, password=None):
    ssid, password = validate_hotspot_settings(ssid, password)
    config = read_hostapd_config()
    if not config:
        raise ValueError("Hotspot config was not found on this Pi.")
    lines = HOSTAPD_CONF.read_text(encoding="utf-8", errors="replace").splitlines()
    saw_ssid = False
    saw_password = False
    updated = []
    for line in lines:
        if line.startswith("ssid="):
            updated.append(f"ssid={ssid}")
            saw_ssid = True
        elif password and line.startswith("wpa_passphrase="):
            updated.append(f"wpa_passphrase={password}")
            saw_password = True
        else:
            updated.append(line)
    if not saw_ssid:
        updated.append(f"ssid={ssid}")
    if password and not saw_password:
        updated.append(f"wpa_passphrase={password}")
    HOSTAPD_CONF.write_text("\n".join(updated) + "\n", encoding="utf-8")
    subprocess.run(["systemctl", "restart", "hostapd"], check=False, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    return hotspot_status()


def parse_multipart(headers, stream):
    content_type = headers.get("Content-Type", "")
    match = re.search(r"boundary=(?P<boundary>[^;]+)", content_type)
    if not match:
        raise ValueError("Upload request is missing a multipart boundary.")
    boundary = match.group("boundary").strip().strip('"').encode()
    length = int(headers.get("Content-Length", "0"))
    if length <= 0:
        raise ValueError("Upload request is empty.")
    body = stream.read(length)
    marker = b"--" + boundary
    fields = {}
    files = {}
    for part in body.split(marker):
        part = part.strip(b"\r\n")
        if not part or part == b"--":
            continue
        if part.endswith(b"--"):
            part = part[:-2].rstrip(b"\r\n")
        if b"\r\n\r\n" not in part:
            continue
        raw_headers, payload = part.split(b"\r\n\r\n", 1)
        payload = payload.rstrip(b"\r\n")
        header_lines = raw_headers.decode("utf-8", "replace").split("\r\n")
        disposition = ""
        for line in header_lines:
            if line.lower().startswith("content-disposition:"):
                disposition = line
                break
        name_match = re.search(r'name="([^"]+)"', disposition)
        if not name_match:
            continue
        name = name_match.group(1)
        filename_match = re.search(r'filename="([^"]*)"', disposition)
        if filename_match:
            files[name] = {"filename": filename_match.group(1), "data": payload}
        else:
            fields[name] = payload.decode("utf-8", "replace")
    return fields, files


def read_recent_logs():
    lines = []
    for log_file in LOG_FILES:
        path = Path(log_file)
        if not path.is_file():
            continue
        try:
            tail = path.read_text(errors="replace").splitlines()[-18:]
        except OSError:
            continue
        lines.append(f"== {log_file} ==")
        lines.extend(tail)
    return "\n".join(lines[-80:]) or "No portal activity yet."


def count_files(root):
    if not root.exists():
        return 0
    total = 0
    try:
        for _, _, files in os.walk(root):
            total += len(files)
    except OSError:
        return 0
    return total


def directory_size(root):
    if not root.exists():
        return 0
    total = 0
    try:
        for current_root, _, files in os.walk(root):
            for filename in files:
                try:
                    total += (Path(current_root) / filename).stat().st_size
                except OSError:
                    continue
    except OSError:
        return 0
    return total


def directory_thumbnail(root, rel):
    target, rel = safe_join(root, rel)
    if not target.is_dir():
        return ""
    candidates = [
        "thumb.png",
        "thumb.jpg",
        "thumb.jpeg",
        "thumbnail.png",
        "thumbnail.jpg",
        "thumbnail.jpeg",
    ]
    for name in candidates:
        candidate = target / name
        if candidate.is_file():
            child_rel = posixpath.join(rel, name) if rel else name
            return f"/download?drive=cam&path={quote(child_rel)}"
    return ""


def is_hidden_event_support_file(drive_key, parent_rel, name):
    if drive_key != "cam":
        return False
    parts = [part for part in parent_rel.split("/") if part]
    if len(parts) != 3 or parts[0] != "TeslaCam" or parts[1] not in {"SavedClips", "SentryClips"}:
        return False
    lowered = name.lower()
    return lowered in {
        "event.json",
        "event.mp4",
        "thumb.png",
        "thumb.jpg",
        "thumb.jpeg",
        "thumbnail.png",
        "thumbnail.jpg",
        "thumbnail.jpeg",
    }


def count_files_under(root, rel):
    target, _ = safe_join(root, rel)
    return count_files(target) if target.exists() else 0


def directory_size_under(root, rel):
    target, _ = safe_join(root, rel)
    return directory_size(target) if target.exists() else 0


def count_light_shows(root):
    target, _ = safe_join(root, "LightShow")
    if not target.exists():
        return 0
    shows = set()
    try:
        for child in target.iterdir():
            if not child.is_file():
                continue
            suffix = child.suffix.lower()
            if suffix in {".fseq", ".mp3", ".wav"}:
                shows.add(child.stem.lower())
    except OSError:
        return 0
    return len(shows)


def home_counts():
    counts = {"dashcam": None, "photobooth": None, "music": None, "lightshow": None, "chime": None}
    cam_root = DRIVES["cam"]["root"]
    sounds_root = DRIVES["sounds"]["root"]
    music_root = DRIVES["music"]["root"]
    if cam_root.exists():
        counts["dashcam"] = sum(count_files_under(cam_root, rel) for rel in (
            "TeslaCam/RecentClips",
            "TeslaCam/SavedClips",
            "TeslaCam/SentryClips",
            "TeslaCam/EncryptedClips",
        ))
        counts["photobooth"] = count_files_under(cam_root, "TeslaCam/Photobooth")
    if music_root.exists():
        counts["music"] = count_files_under(music_root, "Music")
    if sounds_root.exists():
        counts["lightshow"] = count_light_shows(sounds_root)
        try:
            counts["chime"] = 1 if (sounds_root / "LockChime.wav").exists() else 0
        except OSError:
            counts["chime"] = 0
    return counts


def home_folder_sizes():
    sizes = {"photobooth": None}
    cam_root = DRIVES["cam"]["root"]
    if cam_root.exists():
        sizes["photobooth"] = directory_size_under(cam_root, "TeslaCam/Photobooth")
    return sizes


def session_deadline():
    with SESSION_DEADLINE_LOCK:
        return SESSION_DEADLINE


def write_session_deadline(deadline):
    try:
        SESSION_DEADLINE_FILE.write_text(f"{deadline:.6f}\n", encoding="utf-8")
    except OSError:
        pass


def read_session_deadline():
    try:
        return float(SESSION_DEADLINE_FILE.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return 0.0


def load_session_deadline():
    global SESSION_DEADLINE
    deadline = read_session_deadline()
    with SESSION_DEADLINE_LOCK:
        SESSION_DEADLINE = deadline
    return deadline


def set_session_deadline(seconds=None):
    global SESSION_DEADLINE
    with SESSION_DEADLINE_LOCK:
        SESSION_DEADLINE = time.time() + (seconds or SESSION_TIMEOUT_SECONDS)
        deadline = SESSION_DEADLINE
    write_session_deadline(deadline)
    return deadline


def clear_session_deadline():
    global SESSION_DEADLINE
    with SESSION_DEADLINE_LOCK:
        SESSION_DEADLINE = 0.0
    try:
        SESSION_DEADLINE_FILE.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass


def session_watchdog():
    while True:
        time.sleep(5)
        deadline = session_deadline()
        if not deadline or time.time() < deadline:
            continue
        try:
            status = json.loads(run_helper("status"))
            if status.get("session_active"):
                run_helper("stop")
        except Exception:
            pass
        finally:
            clear_session_deadline()


def get_status():
    try:
        raw = run_helper("status")
        status = json.loads(raw)
    except Exception as exc:
        status = {"session_active": False, "usb": "unknown", "mounts": {}, "error": str(exc)}
    if not status.get("session_active"):
        clear_session_deadline()
    elif not session_deadline():
        # If the portal service restarted mid-session, mounts can remain active
        # while the in-memory timer is gone. Re-arm a short timeout so transfer
        # mode cannot persist indefinitely.
        set_session_deadline()
    drives = {}
    for key, info in DRIVES.items():
        root = info["root"]
        mounted = status.get("mounts", {}).get(key, False)
        usage = None
        files = 0
        if mounted:
            try:
                disk = shutil.disk_usage(root)
                usage = {"free": disk.free, "total": disk.total, "used": disk.used}
                files = count_files(root)
            except OSError:
                usage = None
        drives[key] = {
            "key": key,
            "label": info["label"],
            "title": info["title"],
            "mounted": mounted,
            "usage": usage,
            "files": files,
            "home_path": info["home_path"],
        }
    status["drives"] = drives
    status["uploads_enabled"] = UPLOADS_ENABLED
    status["deletes_enabled"] = DELETES_ENABLED
    status["session_expires_at"] = int(session_deadline()) if status.get("session_active") and session_deadline() else None
    status["session_timeout_seconds"] = SESSION_TIMEOUT_SECONDS
    status["home_counts"] = home_counts()
    status["home_folder_sizes"] = home_folder_sizes()
    status["hotspot"] = hotspot_status()
    status["upload_targets"] = {
        key: {
            "label": value["label"],
            "drive": value["drive"],
            "dir": value["dir"],
            "extensions": sorted(value["extensions"]),
        }
        for key, value in UPLOAD_TARGETS.items()
    }
    status["logs"] = read_recent_logs()
    return status


def list_directory(drive_key, rel, limit=None, offset=0):
    if drive_key not in DRIVES:
        raise ValueError("Unknown drive.")
    root = DRIVES[drive_key]["root"]
    target, rel = safe_join(root, rel)
    if not target.exists():
        target = root
        rel = ""
    if not target.is_dir():
        raise ValueError("Browse path is not a directory.")
    children = [
        child for child in sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        if not (child.is_file() and is_hidden_event_support_file(drive_key, rel, child.name))
    ]
    total = len(children)
    offset = max(0, int(offset or 0))
    if limit:
        children = children[offset:offset + max(1, int(limit))]
    items = []
    for child in children:
        try:
            stat = child.stat()
        except OSError:
            continue
        is_dir = child.is_dir()
        size = directory_size(child) if is_dir else stat.st_size
        child_rel = posixpath.join(rel, child.name) if rel else child.name
        thumbnail = directory_thumbnail(root, child_rel) if is_dir and drive_key == "cam" else ""
        items.append({
            "name": child.name,
            "path": child_rel,
            "is_dir": is_dir,
            "size": size,
            "size_label": format_bytes(size),
            "modified": int(stat.st_mtime),
            "download": "" if is_dir else f"/download?drive={quote(drive_key)}&path={quote(child_rel)}",
            "thumbnail": thumbnail,
            "art": "" if is_dir or child.suffix.lower() not in {".mp3", ".flac", ".m4a", ".aac", ".mp4"} else f"/art?drive={quote(drive_key)}&path={quote(child_rel)}",
        })
    parent = posixpath.dirname(rel) if rel else ""
    return {"drive": drive_key, "path": rel, "parent": parent, "items": items, "total": total, "offset": offset, "limit": limit}


def synchsafe_to_int(data):
    return ((data[0] & 0x7F) << 21) | ((data[1] & 0x7F) << 14) | ((data[2] & 0x7F) << 7) | (data[3] & 0x7F)


def guess_image_type(data, fallback="application/octet-stream"):
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"GIF87a") or data.startswith(b"GIF89a"):
        return "image/gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    return fallback or "application/octet-stream"


def extract_id3_art(path):
    with path.open("rb") as file:
        header = file.read(10)
        if len(header) != 10 or header[:3] != b"ID3":
            return None
        version = header[3]
        tag_size = synchsafe_to_int(header[6:10])
        tag = file.read(min(tag_size, MAX_ART_BYTES + 1024 * 1024))
    offset = 0
    while offset + 10 <= len(tag):
        frame_id = tag[offset:offset + 4]
        if not frame_id.strip(b"\x00"):
            break
        if version == 4:
            frame_size = synchsafe_to_int(tag[offset + 4:offset + 8])
        else:
            frame_size = int.from_bytes(tag[offset + 4:offset + 8], "big")
        payload = tag[offset + 10:offset + 10 + frame_size]
        if frame_id == b"APIC" and len(payload) > 4:
            mime_end = payload.find(b"\x00", 1)
            if mime_end != -1 and mime_end + 2 < len(payload):
                mime = payload[1:mime_end].decode("latin1", "ignore") or "image/jpeg"
                desc_start = mime_end + 2
                desc_end = payload.find(b"\x00", desc_start)
                if desc_end != -1 and desc_end + 1 < len(payload):
                    image = payload[desc_end + 1:]
                    if 0 < len(image) <= MAX_ART_BYTES:
                        return mime, image
        offset += 10 + frame_size
    return None


def extract_flac_art(path):
    with path.open("rb") as file:
        if file.read(4) != b"fLaC":
            return None
        while True:
            header = file.read(4)
            if len(header) != 4:
                return None
            is_last = bool(header[0] & 0x80)
            block_type = header[0] & 0x7F
            block_len = int.from_bytes(header[1:4], "big")
            if block_type == 6:
                block = file.read(min(block_len, MAX_ART_BYTES + 4096))
                if len(block) < 32:
                    return None
                pos = 4
                mime_len = int.from_bytes(block[pos:pos + 4], "big")
                pos += 4
                mime = block[pos:pos + mime_len].decode("latin1", "ignore") or "image/jpeg"
                pos += mime_len
                desc_len = int.from_bytes(block[pos:pos + 4], "big")
                pos += 4 + desc_len + 16
                image_len = int.from_bytes(block[pos:pos + 4], "big")
                pos += 4
                image = block[pos:pos + image_len]
                if 0 < len(image) <= MAX_ART_BYTES:
                    return mime, image
                return None
            file.seek(block_len, os.SEEK_CUR)
            if is_last:
                return None


def iter_mp4_atoms(data, start=0, end=None):
    end = len(data) if end is None else min(end, len(data))
    pos = start
    while pos + 8 <= end:
        size = int.from_bytes(data[pos:pos + 4], "big")
        atom_type = data[pos + 4:pos + 8]
        header = 8
        if size == 1 and pos + 16 <= end:
            size = int.from_bytes(data[pos + 8:pos + 16], "big")
            header = 16
        elif size == 0:
            size = end - pos
        if size < header or pos + size > end:
            break
        yield atom_type, pos + header, pos + size
        pos += size


def extract_mp4_covr_from_atoms(data, start=0, end=None):
    for atom_type, content_start, content_end in iter_mp4_atoms(data, start, end):
        if atom_type == b"covr":
            for child_type, child_start, child_end in iter_mp4_atoms(data, content_start, content_end):
                if child_type == b"data" and child_end - child_start > 8:
                    data_type = int.from_bytes(data[child_start:child_start + 4], "big")
                    image = data[child_start + 8:child_end]
                    mime = "image/png" if data_type == 14 else "image/jpeg" if data_type == 13 else guess_image_type(image)
                    if 0 < len(image) <= MAX_ART_BYTES:
                        return mime, image
        elif atom_type in {b"moov", b"udta", b"meta", b"ilst"}:
            nested_start = content_start + 4 if atom_type == b"meta" else content_start
            found = extract_mp4_covr_from_atoms(data, nested_start, content_end)
            if found:
                return found
    return None


def extract_mp4_art(path):
    data = path.read_bytes()
    if len(data) > 64 * 1024 * 1024:
        data = data[:64 * 1024 * 1024]
    return extract_mp4_covr_from_atoms(data)


def extract_album_art(path):
    suffix = path.suffix.lower()
    try:
        if suffix == ".mp3":
            return extract_id3_art(path)
        if suffix == ".flac":
            return extract_flac_art(path)
        if suffix in {".m4a", ".aac", ".mp4"}:
            return extract_mp4_art(path)
    except Exception:
        return None
    return None


def require_mutagen():
    try:
        import mutagen  # noqa: F401
    except Exception as exc:
        raise ValueError("Music editing needs python3-mutagen installed on the Pi.") from exc


def read_music_metadata(path):
    require_mutagen()
    from mutagen import File as MutagenFile

    audio = MutagenFile(path, easy=True)
    if audio is None:
        raise ValueError("Unsupported audio metadata format.")

    def first(*keys):
        for key in keys:
            values = audio.get(key)
            if values:
                return str(values[0])
        return ""

    return {
        "title": first("title"),
        "artist": first("artist", "albumartist"),
        "album": first("album"),
        "track": first("tracknumber"),
    }


def write_music_metadata(path, fields, art_file=None):
    require_mutagen()
    from mutagen import File as MutagenFile

    suffix = path.suffix.lower()
    audio = MutagenFile(path, easy=True)
    if audio is None:
        raise ValueError("Unsupported audio metadata format.")
    for key, value in {
        "title": fields.get("title", ""),
        "artist": fields.get("artist", ""),
        "album": fields.get("album", ""),
        "tracknumber": fields.get("track", ""),
    }.items():
        value = str(value or "").strip()
        if value:
            audio[key] = [value]
        elif key in audio:
            del audio[key]
    audio.save()

    if not art_file or not art_file.get("data"):
        os.sync()
        return {"ok": True}

    image = art_file["data"]
    if len(image) > MAX_ART_BYTES:
        raise ValueError("Album art must be 8 MB or smaller.")
    mime = guess_image_type(image)
    if mime not in {"image/jpeg", "image/png"}:
        raise ValueError("Album art must be a JPG or PNG.")

    if suffix == ".mp3":
        from mutagen.id3 import APIC, ID3, ID3NoHeaderError
        try:
            tags = ID3(path)
        except ID3NoHeaderError:
            tags = ID3()
        tags.delall("APIC")
        tags.add(APIC(encoding=3, mime=mime, type=3, desc="Cover", data=image))
        tags.save(path)
    elif suffix == ".flac":
        from mutagen.flac import FLAC, Picture
        audio = FLAC(path)
        audio.clear_pictures()
        picture = Picture()
        picture.type = 3
        picture.mime = mime
        picture.desc = "Cover"
        picture.data = image
        audio.add_picture(picture)
        audio.save()
    elif suffix in {".m4a", ".aac", ".mp4"}:
        from mutagen.mp4 import MP4, MP4Cover
        audio = MP4(path)
        fmt = MP4Cover.FORMAT_PNG if mime == "image/png" else MP4Cover.FORMAT_JPEG
        audio["covr"] = [MP4Cover(image, imageformat=fmt)]
        audio.save()
    else:
        raise ValueError("Album art editing is supported for MP3, FLAC, M4A, and AAC.")
    os.sync()
    return {"ok": True}


def rename_file(drive_key, rel, new_name):
    if drive_key not in DRIVES:
        raise ValueError("Unknown drive.")
    root = DRIVES[drive_key]["root"]
    target, rel = safe_join(root, rel)
    if not target.is_file():
        raise ValueError("Rename path is not a file.")
    allowed = {".fseq", ".mp3", ".wav"} if drive_key == "sounds" and posixpath.dirname(rel) == "LightShow" else None
    if allowed is None:
        raise ValueError("This file cannot be renamed here.")
    filename = validate_filename(new_name, allowed)
    destination = target.with_name(filename)
    if destination.exists() and destination != target:
        raise ValueError("A file with that name already exists.")
    target.rename(destination)
    os.sync()
    return {"ok": True, "drive": drive_key, "path": posixpath.join(posixpath.dirname(rel), filename), "filename": filename}


def delete_path(drive_key, rel):
    if not DELETES_ENABLED:
        raise ValueError("Deletes are disabled.")
    if drive_key not in DRIVES:
        raise ValueError("Unknown drive.")
    root = DRIVES[drive_key]["root"]
    target, rel = safe_join(root, rel)
    if target == root.resolve():
        raise ValueError("Refusing to delete the drive root.")
    if not target.exists():
        raise ValueError("Path does not exist.")
    if target.is_dir():
        shutil.rmtree(target)
    else:
        target.unlink()
    os.sync()
    return {"ok": True, "drive": drive_key, "path": rel}


def delete_media_path(drive_key, rel):
    if drive_key not in DRIVES:
        raise ValueError("Unknown drive.")
    parent = posixpath.dirname(rel)
    allowed = (
        drive_key == "music" and parent == "Music"
    ) or (
        drive_key == "sounds" and parent == "LightShow"
    )
    if not allowed:
        raise ValueError("Only Music and LightShow files can be deleted here.")
    root = DRIVES[drive_key]["root"]
    target, rel = safe_join(root, rel)
    if not target.is_file():
        raise ValueError("Delete path is not a file.")
    target.unlink()
    os.sync()
    return {"ok": True, "drive": drive_key, "path": rel}


class PortalHandler(BaseHTTPRequestHandler):
    server_version = "TeslaUSBPortal/2.1"

    def send_text(self, body, status=HTTPStatus.OK, content_type="text/html; charset=utf-8"):
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_json(self, payload, status=HTTPStatus.OK):
        self.send_text(json.dumps(payload), status=status, content_type="application/json")

    def redirect(self, location="/"):
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", location)
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/":
                self.send_text(APP_HTML)
            elif parsed.path == "/api/status":
                self.send_json(get_status())
            elif parsed.path == "/api/list":
                query = parse_qs(parsed.query)
                limit = query.get("limit", [None])[0]
                offset = query.get("offset", [0])[0]
                self.send_json(list_directory(query.get("drive", ["cam"])[0], query.get("path", [""])[0], int(limit) if limit else None, int(offset or 0)))
            elif parsed.path == "/download":
                self.download(parsed)
            elif parsed.path == "/art":
                self.album_art(parsed)
            elif parsed.path == "/api/music-metadata":
                self.handle_music_metadata_get(parsed)
            else:
                self.send_error(HTTPStatus.NOT_FOUND)
        except Exception as exc:
            self.send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)

    def do_HEAD(self):
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/download":
                self.download(parsed, head_only=True)
            elif parsed.path == "/art":
                self.album_art(parsed, head_only=True)
            elif parsed.path == "/":
                data = APP_HTML.encode("utf-8")
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
            else:
                self.send_error(HTTPStatus.NOT_FOUND)
        except Exception as exc:
            self.send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)

    def do_POST(self):
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/session/start":
                run_helper("start")
                set_session_deadline()
                self.send_json(get_status())
            elif parsed.path == "/session/extend":
                status = get_status()
                if not status.get("session_active"):
                    raise ValueError("Start a transfer session before extending it.")
                set_session_deadline(SESSION_EXTEND_SECONDS)
                self.send_json(get_status())
            elif parsed.path == "/session/stop":
                run_helper("stop")
                clear_session_deadline()
                self.send_json(get_status())
            elif parsed.path == "/upload":
                self.handle_upload()
            elif parsed.path == "/api/rename":
                self.handle_rename()
            elif parsed.path == "/api/music-metadata":
                self.handle_music_metadata_post()
            elif parsed.path == "/api/delete":
                self.handle_delete()
            elif parsed.path == "/api/media-delete":
                self.handle_media_delete()
            elif parsed.path == "/api/hotspot":
                self.handle_hotspot_update()
            else:
                self.send_error(HTTPStatus.NOT_FOUND)
        except Exception as exc:
            self.send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)

    def download(self, parsed, head_only=False):
        query = parse_qs(parsed.query)
        drive_key = query.get("drive", [""])[0]
        rel = query.get("path", [""])[0]
        if drive_key not in DRIVES:
            raise ValueError("Unknown drive.")
        target, _ = safe_join(DRIVES[drive_key]["root"], rel)
        if not target.is_file():
            raise ValueError("Download path is not a file.")
        content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        file_size = target.stat().st_size
        range_header = self.headers.get("Range", "")
        start = 0
        end = file_size - 1
        status = HTTPStatus.OK
        if range_header:
            match = re.match(r"bytes=(\d*)-(\d*)$", range_header.strip())
            if not match:
                self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                self.send_header("Content-Range", f"bytes */{file_size}")
                self.send_header("Accept-Ranges", "bytes")
                self.end_headers()
                return
            raw_start, raw_end = match.groups()
            if raw_start == "" and raw_end:
                suffix_len = int(raw_end)
                start = max(0, file_size - suffix_len)
            elif raw_start:
                start = int(raw_start)
            if raw_end and raw_start:
                end = min(file_size - 1, int(raw_end))
            if start >= file_size or end < start:
                self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                self.send_header("Content-Range", f"bytes */{file_size}")
                self.send_header("Accept-Ranges", "bytes")
                self.end_headers()
                return
            status = HTTPStatus.PARTIAL_CONTENT

        length = end - start + 1
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        if status == HTTPStatus.PARTIAL_CONTENT:
            self.send_header("Content-Range", f"bytes {start}-{end}/{file_size}")
        disposition = "inline" if query.get("inline", ["0"])[0] == "1" else "attachment"
        self.send_header("Content-Disposition", f'{disposition}; filename="{target.name}"')
        self.end_headers()
        if head_only:
            return
        with target.open("rb") as file:
            file.seek(start)
            remaining = length
            while remaining > 0:
                chunk = file.read(min(1024 * 1024, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)

    def album_art(self, parsed, head_only=False):
        query = parse_qs(parsed.query)
        drive_key = query.get("drive", [""])[0]
        rel = query.get("path", [""])[0]
        if drive_key not in DRIVES:
            raise ValueError("Unknown drive.")
        target, _ = safe_join(DRIVES[drive_key]["root"], rel)
        if not target.is_file():
            raise ValueError("Album art path is not a file.")
        extracted = extract_album_art(target)
        if not extracted:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        content_type, image = extracted
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", guess_image_type(image, content_type))
        self.send_header("Content-Length", str(len(image)))
        self.send_header("Cache-Control", "private, max-age=3600")
        self.end_headers()
        if not head_only:
            self.wfile.write(image)

    def handle_upload(self):
        if not UPLOADS_ENABLED:
            raise ValueError("Uploads are disabled.")
        status = get_status()
        if not status["session_active"]:
            raise ValueError("Start a transfer session before uploading.")
        fields, files = parse_multipart(self.headers, self.rfile)
        target_key = fields.get("target", "")
        if target_key not in UPLOAD_TARGETS:
            raise ValueError("Unknown upload destination.")
        file_item = files.get("file")
        if file_item is None or not file_item.get("filename"):
            raise ValueError("No file uploaded.")
        target_info = UPLOAD_TARGETS[target_key]
        drive = DRIVES[target_info["drive"]]
        if not status["drives"][target_info["drive"]]["mounted"]:
            raise ValueError(f"{drive['label']} is not mounted.")
        filename = validate_filename(file_item["filename"], target_info["extensions"], target_info.get("fixed_name"))
        destination_dir, _ = safe_join(drive["root"], target_info["dir"])
        destination_dir.mkdir(parents=True, exist_ok=True)
        destination = destination_dir / filename
        with destination.open("wb") as output:
            output.write(file_item["data"])
        os.sync()
        self.send_json({
            "ok": True,
            "drive": target_info["drive"],
            "path": target_info["dir"],
            "filename": filename,
        })

    def read_json_body(self):
        length = int(self.headers.get("Content-Length", "0") or "0")
        body = self.rfile.read(length) if length else b""
        try:
            return json.loads(body or b"{}")
        except json.JSONDecodeError:
            raise ValueError("Request body must be JSON.")

    def handle_music_metadata_get(self, parsed):
        query = parse_qs(parsed.query)
        drive_key = query.get("drive", ["music"])[0]
        rel = query.get("path", [""])[0]
        if drive_key != "music":
            raise ValueError("Music metadata is only available for music files.")
        target, _ = safe_join(DRIVES["music"]["root"], rel)
        if not target.is_file():
            raise ValueError("Music path is not a file.")
        self.send_json(read_music_metadata(target))

    def handle_music_metadata_post(self):
        status = get_status()
        if not status["session_active"]:
            raise ValueError("Start a transfer session before editing music.")
        fields, files = parse_multipart(self.headers, self.rfile)
        drive_key = fields.get("drive", "music")
        rel = fields.get("path", "")
        if drive_key != "music" or not status["drives"].get("music", {}).get("mounted"):
            raise ValueError("Music drive is not mounted.")
        target, _ = safe_join(DRIVES["music"]["root"], rel)
        if not target.is_file():
            raise ValueError("Music path is not a file.")
        if target.suffix.lower() not in {".mp3", ".flac", ".m4a", ".aac"}:
            raise ValueError("Metadata editing is supported for MP3, FLAC, M4A, and AAC.")
        self.send_json(write_music_metadata(target, fields, files.get("art")))

    def handle_rename(self):
        status = get_status()
        if not status["session_active"]:
            raise ValueError("Start a transfer session before renaming.")
        payload = self.read_json_body()
        drive_key = payload.get("drive", "")
        rel = payload.get("path", "")
        new_name = payload.get("new_name", "")
        if not status["drives"].get(drive_key, {}).get("mounted"):
            raise ValueError("Drive is not mounted.")
        self.send_json(rename_file(drive_key, rel, new_name))

    def handle_delete(self):
        if not DELETES_ENABLED:
            raise ValueError("Deletes are disabled.")
        status = get_status()
        if not status["session_active"]:
            raise ValueError("Start a transfer session before deleting.")
        payload = self.read_json_body()
        drive_key = payload.get("drive", "")
        rel = payload.get("path", "")
        if not status["drives"].get(drive_key, {}).get("mounted"):
            raise ValueError("Drive is not mounted.")
        self.send_json(delete_path(drive_key, rel))

    def handle_media_delete(self):
        status = get_status()
        if not status["session_active"]:
            raise ValueError("Start a transfer session before deleting.")
        payload = self.read_json_body()
        drive_key = payload.get("drive", "")
        rel = payload.get("path", "")
        if not status["drives"].get(drive_key, {}).get("mounted"):
            raise ValueError("Drive is not mounted.")
        self.send_json(delete_media_path(drive_key, rel))

    def handle_hotspot_update(self):
        payload = self.read_json_body()
        ssid = payload.get("ssid", "")
        password = payload.get("password") or None
        self.send_json(update_hotspot_settings(ssid, password))


APP_HTML = r"""<!doctype html>
<html lang="en" data-page="home">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>TeslaDrive</title>
<style>
  :root {
    --bg: oklch(0.16 0.005 80);
    --surface: oklch(0.20 0.005 80);
    --surface-2: oklch(0.23 0.005 80);
    --text: oklch(0.94 0.005 80);
    --muted: oklch(0.66 0.005 80);
    --faint: oklch(0.44 0.005 80);
    --hairline: oklch(0.30 0.005 80);
    --hairline-2: oklch(0.36 0.005 80);
    --ok: oklch(0.78 0.09 145);
    --warn: oklch(0.78 0.12 70);
    --error: oklch(0.70 0.18 25);
    --accent: oklch(0.94 0.005 80);
    --on-accent: oklch(0.16 0.005 80);
    --radius: 10px;
    --radius-sm: 6px;
    --pad: 22px;
    --gap: 16px;
    font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  }
  * { box-sizing: border-box; }
  html, body { min-height: 100%; }
  body { margin: 0; background: var(--bg); color: var(--text); font-size: 14px; line-height: 1.45; -webkit-font-smoothing: antialiased; }
  button, input, select { font: inherit; }
  button { color: inherit; cursor: pointer; }
  a { color: inherit; text-decoration: none; }
  .mono { font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace; font-feature-settings: "tnum", "zero"; }
  .num-faint { color: var(--faint); }
  .hidden { display: none !important; }

  .app { min-height: 100vh; display: grid; grid-template-rows: auto 1fr; }

  /* Top bar */
  .topbar { display: flex; justify-content: space-between; align-items: center; gap: 16px; padding: 16px 40px; border-bottom: 1px solid var(--hairline); background: var(--bg); position: sticky; top: 0; z-index: 5; min-height: 64px; }
  .topbar-l { display: flex; align-items: center; gap: 14px; }
  .brand-name { font-size: 18px; font-weight: 600; letter-spacing: -0.01em; }
  .back-btn { display: inline-flex; align-items: center; gap: 8px; background: transparent; border: 1px solid var(--hairline-2); border-radius: 999px; padding: 8px 16px 8px 12px; color: var(--text); font-size: 13px; font-weight: 500; transition: background 120ms, border-color 120ms; }
  .back-btn:hover { background: var(--surface); border-color: var(--text); }

  .topbar-r { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; justify-content: flex-end; }
  .status-chip { display: inline-flex; align-items: center; gap: 8px; color: var(--muted); font-size: 12px; }
  .dot { width: 7px; height: 7px; border-radius: 50%; background: var(--faint); display: inline-block; }
  .dot.ok { background: var(--ok); box-shadow: 0 0 0 3px color-mix(in oklch, var(--ok) 22%, transparent); }
  .dot.warn { background: var(--warn); box-shadow: 0 0 0 3px color-mix(in oklch, var(--warn) 22%, transparent); }
  .dot.err { background: var(--error); box-shadow: 0 0 0 3px color-mix(in oklch, var(--error) 22%, transparent); }

  .gear-btn { width: 36px; height: 36px; border-radius: 999px; border: 1px solid var(--hairline-2); background: transparent; color: var(--muted); display: grid; place-items: center; transition: color 120ms, border-color 120ms; }
  .gear-btn:hover { color: var(--text); border-color: var(--text); }

  /* Main */
  .mn { width: 100%; max-width: 1400px; margin: 0 auto; padding: 36px 40px 80px; }

  .page-head { display: flex; justify-content: space-between; align-items: flex-end; gap: 24px; margin-bottom: 24px; flex-wrap: wrap; }
  .page-title { margin: 0; font-size: clamp(40px, 5.5vw, 56px); line-height: 1; font-weight: 500; letter-spacing: -0.02em; }
  .page-note { color: var(--muted); margin: 12px 0 0; max-width: 60ch; font-size: 14px; }
  .page-head-r { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
  .ph-r-info { font-size: 11.5px; color: var(--faint); }

  /* Buttons */
  .btn { display: inline-flex; align-items: center; justify-content: center; gap: 8px; border-radius: var(--radius-sm); border: 1px solid var(--hairline-2); background: transparent; color: var(--text); min-height: 38px; padding: 8px 14px; font-size: 13px; font-weight: 500; white-space: nowrap; transition: background 120ms, border-color 120ms; }
  .btn:hover { background: var(--surface-2); border-color: var(--text); }
  .btn-solid { background: var(--text); color: var(--bg); border-color: var(--text); }
  .btn-solid:hover { background: var(--accent); border-color: var(--accent); }
  .btn-danger { color: var(--error); border-color: color-mix(in oklch, var(--error) 35%, transparent); }
  .btn-danger:hover { background: color-mix(in oklch, var(--error) 12%, transparent); border-color: var(--error); }
  .btn-sm { min-height: 30px; padding: 5px 10px; font-size: 12px; }

  .icon-btn { width: 28px; height: 28px; border-radius: 5px; background: transparent; border: 0; display: inline-grid; place-items: center; color: var(--muted); }
  .icon-btn:hover { background: var(--surface-2); color: var(--text); }
  .icon-btn-danger:hover { background: color-mix(in oklch, var(--error) 14%, transparent); color: var(--error); }
  .menu-wrap { position: relative; display: inline-grid; place-items: center; }
  .file-menu { position: absolute; right: 0; top: calc(100% + 6px); z-index: 18; min-width: 150px; display: grid; padding: 6px; border: 1px solid var(--hairline-2); border-radius: 9px; background: var(--surface-2); box-shadow: 0 14px 34px rgba(0,0,0,.34); }
  .file-menu.hidden { display: none; }
  .file-menu-item { min-height: 36px; display: flex; align-items: center; gap: 9px; padding: 8px 10px; border: 0; border-radius: 6px; background: transparent; color: var(--text); text-align: left; font-size: 13px; }
  .file-menu-item:hover { background: var(--surface); }
  .file-menu-item.danger { color: var(--error); }

  /* Cards */
  .card { background: var(--surface); border: 1px solid var(--hairline); border-radius: var(--radius); }
  .card-pad { padding: var(--pad); }

  /* Home tiles */
  .home-head { margin-bottom: 28px; }
  .home-title { margin: 0; font-size: clamp(40px, 6vw, 68px); line-height: 1; letter-spacing: -0.025em; font-weight: 400; max-width: 720px; }
  .home-tiles { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 14px; }
  .home-tile { position: relative; background: var(--surface); border: 1px solid var(--hairline); border-radius: 12px; padding: 24px; text-align: left; display: flex; flex-direction: column; gap: 12px; min-height: 280px; color: var(--text); transition: border-color 180ms, transform 180ms, background 180ms; }
  .home-tile:hover { border-color: var(--text); transform: translateY(-1px); background: var(--surface-2); }
  .home-tile-icon { color: var(--muted); margin-bottom: 6px; }
  .home-tile:hover .home-tile-icon { color: var(--text); }
  .home-tile-num { font-size: 64px; line-height: 0.9; letter-spacing: -0.04em; font-weight: 400; }
  .home-tile-num-label { font-size: 10.5px; color: var(--faint); letter-spacing: 0.12em; text-transform: uppercase; }
  .home-tile-body { margin-top: auto; }
  .home-tile-label { font-size: 22px; line-height: 1.1; margin-bottom: 6px; font-weight: 500; }
  .home-tile-foot { display: flex; flex-direction: column; gap: 6px; padding-top: 16px; border-top: 1px solid var(--hairline); margin-top: 12px; }
  .home-tile-foot-t { display: flex; justify-content: space-between; font-size: 10.5px; color: var(--faint); letter-spacing: 0.04em; }
  .meter-track { height: 2px; background: var(--surface-2); border-radius: 999px; overflow: hidden; }
  .meter-fill { height: 100%; background: var(--text); transition: width 280ms; }
  .meter-fill.ok { background: var(--ok); }
  .meter-fill.err { background: var(--error); }

  /* Section banner — session state */
  .banner { display: grid; grid-template-columns: auto 1fr auto; gap: 14px; align-items: center; padding: 14px 18px; border-radius: 10px; border: 1px solid var(--hairline); background: var(--surface); margin-bottom: 22px; }
  .banner-warn { border-color: color-mix(in oklch, var(--warn) 30%, var(--hairline)); background: color-mix(in oklch, var(--warn) 8%, var(--surface)); }
  .banner-ok { border-color: color-mix(in oklch, var(--ok) 30%, var(--hairline)); background: color-mix(in oklch, var(--ok) 6%, var(--surface)); }
  .banner-icon { display: grid; place-items: center; color: var(--muted); }
  .banner-warn .banner-icon { color: var(--warn); }
  .banner-ok .banner-icon { color: var(--ok); }
  .banner-title { font-size: 13.5px; font-weight: 500; }
  .banner-sub { font-size: 11.5px; color: var(--muted); margin-top: 2px; }

  /* Upload zone */
  .uz { display: grid; grid-template-columns: auto 1fr auto; gap: 24px; align-items: center; padding: 22px 26px; border: 1.5px dashed var(--hairline-2); border-radius: 10px; background: color-mix(in oklch, var(--surface) 50%, transparent); margin-bottom: 22px; transition: border-color 160ms, background 160ms; }
  .uz-drag { border-color: var(--text); background: var(--surface-2); }
  .uz-icon { color: var(--muted); }
  .uz-title { font-size: 16px; font-weight: 500; }
  .uz-note { font-size: 12px; color: var(--muted); margin-top: 4px; line-height: 1.55; max-width: 60ch; }
  .uz-kinds { display: flex; gap: 6px; margin-top: 10px; flex-wrap: wrap; }
  .uz-chip { display: inline-block; padding: 2px 8px; font-size: 10px; color: var(--muted); background: var(--surface-2); border: 1px solid var(--hairline); border-radius: 999px; letter-spacing: 0.04em; }
  .uz-actions { display: flex; flex-direction: column; align-items: flex-end; gap: 14px; }
  .uz-file-input { display: none; }

  /* Rejection */
  .rj { display: flex; flex-direction: column; gap: 6px; margin: -10px 0 22px; }
  .rj-row { display: grid; grid-template-columns: auto 1fr auto; gap: 14px; align-items: center; padding: 12px 16px; background: color-mix(in oklch, var(--warn) 8%, var(--surface)); border: 1px solid color-mix(in oklch, var(--warn) 30%, var(--hairline)); border-radius: 8px; }
  .rj-icon { color: var(--warn); display: grid; place-items: center; }
  .rj-name { font-size: 13px; color: var(--text); }
  .rj-reason { font-size: 11.5px; color: var(--muted); margin-top: 3px; }
  .rj-dismiss { width: 26px; height: 26px; border: 0; background: transparent; color: var(--muted); border-radius: 4px; display: grid; place-items: center; }
  .rj-dismiss:hover { background: var(--surface-2); color: var(--text); }
  .upload-progress { width: 100%; min-width: 180px; }
  .upload-progress-label { display: flex; justify-content: space-between; gap: 10px; color: var(--muted); font-size: 11.5px; margin-bottom: 6px; }
  .upload-progress-track { height: 5px; border-radius: 999px; overflow: hidden; background: var(--surface-2); border: 1px solid var(--hairline); }
  .upload-progress-fill { height: 100%; width: 0; background: var(--text); transition: width 120ms; }

  /* Folder tabs */
  .folder-tabs { display: grid; grid-template-columns: repeat(auto-fit, minmax(120px, 1fr)); gap: 0; border: 1px solid var(--hairline); border-radius: 10px; background: var(--surface); overflow: hidden; margin-bottom: 18px; }
  .folder-tab { display: flex; flex-direction: column; gap: 4px; padding: 14px 18px; background: transparent; border: 0; border-right: 1px solid var(--hairline); color: var(--muted); text-align: left; transition: background 120ms, color 120ms; }
  .folder-tab:last-child { border-right: 0; }
  .folder-tab:hover { color: var(--text); background: var(--surface-2); }
  .folder-tab.on { color: var(--text); background: var(--surface-2); }
  .folder-tab-l { font-size: 13px; font-weight: 500; }
  .folder-tab-c { font-size: 11px; color: var(--faint); }

  /* File table */
  .file-tbl { width: 100%; border-collapse: collapse; font-size: 13px; background: var(--surface); }
  .file-tbl th { text-align: left; font-weight: 500; font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace; font-size: 10.5px; letter-spacing: 0.12em; text-transform: uppercase; color: var(--faint); padding: 12px 14px; border-bottom: 1px solid var(--hairline); }
  .file-tbl td { padding: 12px 14px; border-bottom: 1px solid var(--hairline); }
  .file-tbl tr { cursor: pointer; }
  .file-tbl tr:hover { background: var(--surface-2); }
  .clip-row, .dash-folder-row { touch-action: manipulation; }
  .file-tbl-actions { white-space: nowrap; text-align: right; min-width: 330px; }
  .file-tbl-icon { color: var(--faint); width: 30px; }
  .dash-thumb { width: 54px; height: 32px; object-fit: cover; border-radius: 5px; background: var(--bg); border: 1px solid var(--hairline); display: block; }
  .clip-stack { width: 72px; height: 46px; position: relative; display: block; }
  .clip-stack .dash-thumb { width: 64px; height: 38px; position: absolute; left: 0; top: 0; box-shadow: 0 0 0 1px var(--bg); }
  .clip-stack .dash-thumb:nth-child(2) { left: 4px; top: 4px; opacity: .82; }
  .clip-stack .dash-thumb:nth-child(3) { left: 8px; top: 8px; opacity: .68; }
  .dash-thumb-tile { gap: 2px; font-size: 9px; color: var(--muted); text-transform: uppercase; letter-spacing: .04em; }
  .dash-thumb-tile svg { color: var(--text); }
  .clip-name { display: flex; flex-direction: column; gap: 4px; }
  .clip-sub { color: var(--faint); font-size: 11.5px; }
  .clip-summary { display: flex; flex-direction: column; gap: 5px; min-width: 0; }
  .clip-title { font-size: 16px; line-height: 1.25; }
  .clip-expanded { background: color-mix(in oklch, var(--surface) 70%, var(--bg)); }
  .clip-expanded td { padding: 0 14px 14px; }
  .clip-files { display: grid; gap: 8px; padding: 10px 0 0 102px; }
  .clip-file { display: grid; grid-template-columns: 1fr auto auto; gap: 12px; align-items: center; padding: 8px 10px; border: 1px solid var(--hairline); border-radius: 7px; background: var(--bg); }
  .clip-file-actions { display: flex; gap: 4px; align-items: center; }
  .clip-pager { display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 10px; padding: 16px; }
  .clip-pager-info { color: var(--muted); font-size: 12px; text-align: center; order: 2; }
  .clip-pager-actions { display: flex; gap: 8px; align-items: center; justify-content: center; order: 1; }
  .next-arrow svg { transform: rotate(180deg); }
  .album-wrap { width: 46px; height: 46px; display: block; position: relative; overflow: hidden; border-radius: 7px; }
  .album-art { width: 46px; height: 46px; object-fit: cover; border-radius: 7px; background: var(--bg); border: 1px solid var(--hairline); display: block; }
  .album-fallback, .media-icon { width: 46px; height: 46px; border-radius: 7px; background: var(--surface-2); border: 1px solid var(--hairline); display: grid; place-items: center; color: var(--muted); }
  .album-wrap .album-fallback { display: none; position: absolute; inset: 0; }
  .album-wrap.art-missing .album-art { display: none; }
  .album-wrap.art-missing .album-fallback { display: grid; }
  .audio-preview { width: min(260px, 34vw); height: 30px; vertical-align: middle; }
  .audio-row-actions { display: inline-flex; align-items: center; justify-content: flex-end; gap: 8px; }
  .audio-note { display: flex; gap: 10px; align-items: flex-start; color: var(--muted); font-size: 12px; line-height: 1.45; padding: 12px 14px; margin: -4px 0 18px; border: 1px solid color-mix(in oklch, var(--warn) 22%, var(--hairline)); border-radius: 10px; background: color-mix(in oklch, var(--warn) 7%, var(--surface)); }
  .audio-note svg { flex: 0 0 auto; color: var(--warn); margin-top: 1px; }
  .file-name { color: var(--text); }
  .file-empty { padding: 60px; text-align: center; color: var(--muted); }
  .file-empty-h { font-size: 14px; color: var(--text); margin-bottom: 4px; }
  .file-empty-s { font-size: 11.5px; color: var(--faint); }

  /* Lock chime layout */
  .lc-grid { display: grid; grid-template-columns: 1.4fr 1fr; gap: var(--gap); }
  .lc-current { background: var(--surface); border: 1px solid var(--hairline); border-radius: 10px; padding: 32px; }
  .lc-current-h { font-size: 10px; color: var(--faint); letter-spacing: 0.16em; margin-bottom: 10px; text-transform: uppercase; }
  .lc-name { font-size: 36px; line-height: 1; letter-spacing: -0.02em; font-weight: 500; margin-bottom: 22px; }
  .lc-info { display: grid; grid-template-columns: 1fr 1fr; gap: 14px 24px; margin-bottom: 28px; }
  .lc-info-r { display: flex; flex-direction: column; gap: 3px; }
  .lc-info-k { font-size: 10px; color: var(--faint); letter-spacing: 0.12em; }
  .lc-info-v { font-size: 13px; }
  .lc-actions { display: flex; gap: 8px; flex-wrap: wrap; }
  .lc-side { display: flex; flex-direction: column; gap: var(--gap); }
  .lc-side .uz { margin-bottom: 0; }
  .lc-rules { background: var(--surface); border: 1px solid var(--hairline); border-radius: 10px; padding: 20px 22px; }
  .lc-rules-h { font-size: 10px; color: var(--faint); letter-spacing: 0.16em; margin-bottom: 12px; text-transform: uppercase; }
  .lc-rules-list { margin: 0; padding: 0; list-style: none; display: flex; flex-direction: column; gap: 8px; font-size: 12.5px; color: var(--muted); line-height: 1.5; }
  .lc-rules-list li { display: flex; gap: 8px; align-items: flex-start; }
  .lc-rules-list .dot-mark { color: var(--faint); }
  .lc-empty { padding: 40px; text-align: center; color: var(--muted); border: 1px dashed var(--hairline-2); border-radius: 10px; background: color-mix(in oklch, var(--surface) 40%, transparent); }
  .lc-empty-h { font-size: 16px; color: var(--text); margin-bottom: 4px; }

  /* Settings */
  .set-body { display: grid; grid-template-columns: 220px 1fr; gap: var(--gap); }
  .set-nav { display: flex; flex-direction: column; gap: 2px; align-self: start; position: sticky; top: 80px; }
  .set-nav-i { background: transparent; border: 0; padding: 11px 14px; border-radius: var(--radius-sm); text-align: left; color: var(--muted); }
  .set-nav-i:hover, .set-nav-i.on { color: var(--text); background: var(--surface); }
  .set-nav-l { font-size: 13px; font-weight: 500; }
  .set-nav-d { font-size: 10px; color: var(--faint); margin-top: 1px; letter-spacing: 0.06em; }
  .set-section-title { font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace; font-size: 10.5px; letter-spacing: 0.14em; text-transform: uppercase; color: var(--muted); margin: 0 0 14px; }
  .kv { display: flex; justify-content: space-between; gap: 16px; padding: 9px 0; border-bottom: 1px solid var(--hairline); align-items: baseline; }
  .kv:last-child { border-bottom: 0; }
  .kv-k { color: var(--muted); font-size: 12.5px; }
  .kv-v { font-size: 13px; text-align: right; }
  .settings-form { display: grid; gap: 12px; margin-top: 18px; padding-top: 18px; border-top: 1px solid var(--hairline); }
  .log-pre { white-space: pre-wrap; background: var(--bg); color: var(--muted); border: 1px solid var(--hairline); border-radius: 6px; padding: 14px; max-height: 320px; overflow: auto; font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace; font-size: 11px; line-height: 1.55; }
  .tg-row { display: flex; justify-content: space-between; align-items: center; padding: 11px 0; border: 0; background: transparent; width: 100%; border-bottom: 1px solid var(--hairline); color: inherit; text-align: left; }
  .tg-label { font-size: 13px; }
  .tg { width: 32px; height: 18px; border-radius: 999px; background: var(--hairline-2); position: relative; transition: background 160ms; flex-shrink: 0; }
  .tg-on { background: var(--ok); }
  .tg-knob { position: absolute; top: 2px; left: 2px; width: 14px; height: 14px; border-radius: 50%; background: var(--text); transition: left 160ms; }
  .tg-on .tg-knob { left: 16px; }

  /* Toast */
  .toast { position: fixed; left: 50%; bottom: 22px; transform: translateX(-50%); background: var(--text); color: var(--bg); border-radius: 999px; padding: 10px 18px; font-weight: 500; z-index: 20; box-shadow: 0 12px 40px rgba(0,0,0,.28); font-size: 13px; white-space: nowrap; max-width: calc(100vw - 42px); overflow: hidden; text-overflow: ellipsis; }
  .toast.err { background: var(--error); color: white; }
  .session-float { position: fixed; left: 50%; bottom: calc(env(safe-area-inset-bottom, 0px) + 12px); transform: translateX(-50%); z-index: 46; width: min(560px, calc(100vw - 32px)); display: grid; grid-template-columns: 1fr auto; gap: 12px; align-items: center; padding: 12px 14px; border-radius: 14px; border: 1px solid var(--text); background: color-mix(in srgb, var(--surface) 94%, black); box-shadow: 0 18px 60px rgba(0,0,0,.42); backdrop-filter: blur(12px); }
  .session-float.hidden { display: none; }
  .session-float.urgent { animation: session-border-pulse 1.8s ease-in-out infinite; }
  .session-float-title { font-size: 13px; color: var(--text); font-weight: 500; }
  .session-float-sub { font-size: 11.5px; color: var(--muted); margin-top: 2px; line-height: 1.35; }
  .session-float-actions { display: flex; gap: 8px; justify-content: flex-end; }
  @keyframes session-border-pulse {
    0%, 100% { border-color: var(--text); }
    50% { border-color: var(--error); }
  }
  .splash, .extend-modal { position: fixed; inset: 0; z-index: 30; display: grid; place-items: center; padding: 22px; background: color-mix(in srgb, var(--bg) 92%, black); }
  .extend-modal { z-index: 45; }
  .splash.hidden, .extend-modal.hidden { display: none; }
  .splash-card, .extend-card { width: min(560px, 100%); background: var(--surface); border: 1px solid var(--hairline-2); border-radius: var(--radius); padding: 28px; box-shadow: 0 24px 80px rgba(0,0,0,.3); }
  .splash-title, .extend-title { margin: 0; font-size: 36px; line-height: 1; letter-spacing: 0; }
  .splash-sub, .extend-sub { color: var(--muted); margin: 14px 0 22px; }
  .splash-actions, .extend-actions { display: flex; flex-wrap: wrap; gap: 10px; }
  .session-timer { color: var(--error); }
  .edit-modal { position: fixed; inset: 0; z-index: 48; display: grid; place-items: center; padding: 18px; background: rgba(0,0,0,.82); }
  .edit-modal.hidden { display: none; }
  .edit-card { width: min(520px, 100%); max-height: calc(100vh - 36px); overflow: auto; background: var(--surface); border: 1px solid var(--hairline-2); border-radius: 12px; padding: 20px; box-shadow: 0 24px 80px rgba(0,0,0,.36); }
  .edit-head { display: flex; justify-content: space-between; align-items: center; gap: 12px; margin-bottom: 16px; }
  .edit-title { font-size: 19px; font-weight: 500; line-height: 1.2; }
  .edit-form { display: grid; gap: 12px; }
  .edit-grid { display: grid; grid-template-columns: 1fr 120px; gap: 12px; }
  .field { display: grid; gap: 6px; }
  .field label { color: var(--muted); font-size: 11px; letter-spacing: .08em; text-transform: uppercase; }
  .field input { width: 100%; min-height: 38px; border-radius: 7px; border: 1px solid var(--hairline-2); background: var(--bg); color: var(--text); padding: 8px 10px; }
  .edit-actions { display: flex; justify-content: flex-end; gap: 8px; margin-top: 6px; }
  .video-modal { position: fixed; inset: 0; z-index: 35; display: grid; place-items: center; padding: 18px; background: rgba(0,0,0,.82); }
  .video-modal.hidden { display: none; }
  .video-shell { width: min(1040px, 100%); max-height: calc(100vh - 32px); display: grid; gap: 10px; }
  .video-top { display: flex; align-items: center; justify-content: space-between; gap: 10px; }
  .video-title { min-width: 0; font-size: 13px; color: var(--muted); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .video-modebar { display: grid; grid-template-columns: repeat(auto-fit, minmax(132px, 1fr)); gap: 8px; align-items: stretch; }
  .camera-btn { min-height: 44px; justify-content: center; font-size: 13px; }
  .camera-btn.on { background: var(--text); color: var(--bg); border-color: var(--text); }
  .video-actions { display: flex; justify-content: flex-end; gap: 8px; }
  .video-download-panel { display: grid; grid-template-columns: repeat(auto-fit, minmax(126px, 1fr)); gap: 8px; padding: 10px; border: 1px solid var(--hairline); border-radius: 10px; background: var(--surface); }
  .video-download-panel.hidden { display: none; }
  .download-angle { min-height: 40px; }
  .video-grid { display: grid; grid-template-columns: 1fr; gap: 8px; align-items: center; }
  .video-cell { min-width: 0; border: 1px solid var(--hairline-2); border-radius: 8px; overflow: hidden; background: black; }
  .video-cell-label { display: flex; justify-content: space-between; gap: 8px; padding: 7px 9px; background: var(--surface); color: var(--muted); font-size: 11px; }
  .video-player { width: 100%; aspect-ratio: 16 / 9; background: black; display: block; object-fit: contain; }
  .video-hint { display: flex; gap: 10px; align-items: flex-start; color: var(--muted); font-size: 12px; line-height: 1.45; padding: 12px 14px; border: 1px solid color-mix(in oklch, var(--warn) 22%, var(--hairline)); border-radius: 10px; background: color-mix(in oklch, var(--warn) 7%, var(--surface)); }
  .video-hint svg { flex: 0 0 auto; color: var(--warn); margin-top: 1px; }

  /* Responsive */
  @media (max-width: 900px) {
    .topbar { padding: 14px 20px; }
    .mn { padding: 24px 20px 56px; }
    .home-tiles { grid-template-columns: repeat(2, 1fr); }
    .home-tile { min-height: 178px; padding: 16px; gap: 9px; }
    .home-tile-icon { margin-bottom: 2px; }
    .home-tile-num { font-size: 44px; }
    .home-tile-label { font-size: 17px; margin-bottom: 4px; }
    .home-tile-foot { padding-top: 12px; margin-top: 8px; }
    .lc-grid { grid-template-columns: 1fr; }
    .set-body { grid-template-columns: 1fr; }
    .set-nav { position: static; flex-direction: row; flex-wrap: wrap; overflow-x: auto; }
    .uz { grid-template-columns: 1fr; text-align: left; gap: 14px; padding: 18px; }
    .uz-actions { align-items: flex-start; }
    .upload-progress { min-width: 0; }
    .topbar-r .status-chip:nth-child(n+2) { display: none; }
    .file-tbl-actions { min-width: 260px; }
    .audio-preview { width: min(220px, 38vw); }
  }
  @media (max-width: 560px) {
    .home-tiles { grid-template-columns: 1fr; }
    .home-head { margin-bottom: 22px; }
    .home-tile { min-height: 164px; }
    .folder-tabs { grid-template-columns: repeat(2, 1fr); }
    .folder-tab:nth-child(-n+2) { border-bottom: 1px solid var(--hairline); }
    .folder-tab:nth-child(2n) { border-right: 0; }
    .lc-info { grid-template-columns: 1fr; }
    .topbar { padding: 12px 16px; }
    .mn { padding: 20px 16px 60px; }
    .page-title { font-size: 36px; }
    .home-title { font-size: 36px; }
    .topbar { grid-template-columns: 1fr auto; gap: 10px; }
    .topbar-r { gap: 6px; }
    .topbar-r .status-chip { display: none; }
    .btn { padding: 9px 12px; }
    .folder-tab { padding: 12px; }
    .file-tbl, .file-tbl tbody, .file-tbl tr, .file-tbl td { display: block; width: 100%; }
    .file-tbl thead { display: none; }
    .file-tbl tr { display: grid; grid-template-columns: 46px minmax(0, 1fr) 34px; gap: 0 18px; padding: 14px 18px; border-bottom: 1px solid var(--hairline); }
    .file-tbl td { padding: 0; border-bottom: 0; }
    .file-tbl tr:has(.file-empty) { display: block; padding: 0; }
    .file-empty { padding: 34px 22px; }
    .file-tbl-icon { grid-row: 1 / span 3; width: auto; align-self: center; display: flex; align-items: center; }
    .file-name { overflow-wrap: anywhere; align-self: center; }
    .file-tbl-actions { min-width: 0; text-align: left; margin-top: 10px; grid-column: 2 / 4; }
    .clip-row .file-tbl-actions { display: none; }
    .clip-size-cell { display: none !important; }
    .audio-row-actions { width: 100%; display: grid; grid-template-columns: minmax(0, 1fr) 32px; gap: 8px; align-items: center; }
    .audio-preview { width: 100%; min-width: 0; }
    .clip-summary { gap: 4px; }
    .clip-title { font-size: 15px; }
    .clip-sub { font-size: 12px; overflow-wrap: anywhere; }
    .clip-expanded, .clip-pager-row { display: block !important; padding: 0; }
    .clip-expanded td { padding: 0 12px 12px; }
    .clip-pager-row td { padding: 0; }
    .clip-files { padding: 8px 0 0; }
    .clip-file { grid-template-columns: 1fr auto; }
    .clip-file-actions { grid-column: 1 / -1; justify-content: flex-start; }
    .clip-pager { align-items: center; flex-direction: column; }
    .clip-pager-actions { display: grid; grid-template-columns: 1fr 1fr; }
    .clip-pager-actions .btn { justify-content: center; }
    .splash-title, .extend-title { font-size: 30px; }
    .splash-card, .extend-card { padding: 22px; }
    .edit-card { padding: 18px; }
    .edit-grid { grid-template-columns: 1fr; }
    .edit-actions { display: grid; grid-template-columns: 1fr 1fr; }
    .video-modal { padding: 8px; align-items: start; }
    .video-shell { max-height: calc(100vh - 16px); overflow: auto; }
    .video-modebar { grid-template-columns: repeat(2, 1fr); }
    .video-actions { justify-content: stretch; }
    .video-actions .btn { width: 100%; }
    .camera-btn { min-height: 48px; }
    .session-float { grid-template-columns: 1fr; bottom: calc(env(safe-area-inset-bottom, 0px) + 10px); }
    .session-float-actions { display: grid; grid-template-columns: 1fr 1fr; }
  }

  svg { display: block; }
</style>
</head>
<body>
<div class="app">
  <header class="topbar">
    <div class="topbar-l">
      <div id="brandSlot"></div>
    </div>
    <div class="topbar-r">
      <span class="status-chip mono"><span id="usbDot" class="dot"></span><span id="usbText">usb unknown</span></span>
      <span class="status-chip mono"><span id="sessionDot" class="dot"></span><span id="sessionText">session unknown</span></span>
      <button id="sessionButton" class="btn btn-solid" type="button">Start session</button>
      <button class="gear-btn" type="button" title="Settings" onclick="showPage('settings')">
        <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
          <path d="M12.22 2h-.44a2 2 0 0 0-2 2v.18a2 2 0 0 1-1 1.73l-.43.25a2 2 0 0 1-2 0l-.15-.08a2 2 0 0 0-2.73.73l-.22.38a2 2 0 0 0 .73 2.73l.15.1a2 2 0 0 1 1 1.72v.51a2 2 0 0 1-1 1.74l-.15.09a2 2 0 0 0-.73 2.73l.22.38a2 2 0 0 0 2.73.73l.15-.08a2 2 0 0 1 2 0l.43.25a2 2 0 0 1 1 1.73V20a2 2 0 0 0 2 2h.44a2 2 0 0 0 2-2v-.18a2 2 0 0 1 1-1.73l.43-.25a2 2 0 0 1 2 0l.15.08a2 2 0 0 0 2.73-.73l.22-.39a2 2 0 0 0-.73-2.73l-.15-.08a2 2 0 0 1-1-1.74v-.5a2 2 0 0 1 1-1.74l.15-.09a2 2 0 0 0 .73-2.73l-.22-.38a2 2 0 0 0-2.73-.73l-.15.08a2 2 0 0 1-2 0l-.43-.25a2 2 0 0 1-1-1.73V4a2 2 0 0 0-2-2z"/>
          <circle cx="12" cy="12" r="3"/>
        </svg>
      </button>
    </div>
  </header>

  <main class="mn">
    <!-- HOME -->
    <section id="page-home">
      <div class="home-head">
        <h1 class="home-title">What would you like to manage?</h1>
      </div>
      <div id="homeTiles" class="home-tiles"></div>
    </section>

    <!-- DASH CAM -->
    <section id="page-dashcam" class="hidden">
      <div class="page-head">
        <div>
          <h1 class="page-title">Dash cam</h1>
        </div>
        <div class="page-head-r">
          <span id="dashcamInfo" class="ph-r-info mono"></span>
        </div>
      </div>
      <div id="dashcamBanner"></div>
      <div id="dashcamFolders" class="folder-tabs"></div>
      <div class="card" style="padding: 0; overflow: hidden;">
        <table class="file-tbl">
          <thead><tr><th class="file-tbl-icon"></th><th>Name</th><th>Size</th><th></th></tr></thead>
          <tbody id="dashcamTable"></tbody>
        </table>
      </div>
    </section>

    <!-- MUSIC -->
    <section id="page-music" class="hidden">
      <div class="page-head">
        <div><h1 class="page-title">Music</h1></div>
        <div class="page-head-r"><span id="musicInfo" class="ph-r-info mono"></span></div>
      </div>
      <div id="musicBanner"></div>
      <div id="musicUpload"></div>
      <div id="musicPlayer"></div>
      <div id="musicRejections" class="rj"></div>
      <div class="card" style="padding: 0; overflow: hidden;">
        <table class="file-tbl">
          <thead><tr><th class="file-tbl-icon"></th><th>Name</th><th>Size</th><th></th></tr></thead>
          <tbody id="musicTable"></tbody>
        </table>
      </div>
    </section>

    <!-- PHOTOBOOTH -->
    <section id="page-photobooth" class="hidden">
      <div class="page-head">
        <div><h1 class="page-title">Photobooth</h1></div>
        <div class="page-head-r"><span id="photoboothInfo" class="ph-r-info mono"></span></div>
      </div>
      <div id="photoboothBanner"></div>
      <div class="card" style="padding: 0; overflow: hidden;">
        <table class="file-tbl">
          <thead><tr><th class="file-tbl-icon"></th><th>Name</th><th>Size</th><th></th></tr></thead>
          <tbody id="photoboothTable"></tbody>
        </table>
      </div>
    </section>

    <!-- LIGHT SHOWS -->
    <section id="page-lightshow" class="hidden">
      <div class="page-head">
        <div><h1 class="page-title">Light shows</h1></div>
        <div class="page-head-r"><span id="lightshowInfo" class="ph-r-info mono"></span></div>
      </div>
      <div id="lightshowBanner"></div>
      <div id="lightshowUpload"></div>
      <div id="lightshowPlayer"></div>
      <div id="lightshowRejections" class="rj"></div>
      <div class="card" style="padding: 0; overflow: hidden;">
        <table class="file-tbl">
          <thead><tr><th class="file-tbl-icon"></th><th>Name</th><th>Size</th><th></th></tr></thead>
          <tbody id="lightshowTable"></tbody>
        </table>
      </div>
    </section>

    <!-- LOCK CHIME -->
    <section id="page-chime" class="hidden">
      <div class="page-head">
        <div><h1 class="page-title">Lock chime</h1></div>
      </div>
      <div id="chimeBanner"></div>
      <div id="chimeBody"></div>
    </section>

    <!-- SETTINGS -->
    <section id="page-settings" class="hidden">
      <div class="page-head">
        <div><h1 class="page-title">Settings</h1></div>
      </div>
      <div class="set-body">
        <nav class="set-nav" id="setNav"></nav>
        <div id="setMain"></div>
      </div>
    </section>
  </main>
</div>

<div id="toast" class="toast hidden"></div>
<div id="sessionFloat" class="session-float hidden"></div>
<div id="splash" class="splash">
  <div class="splash-card">
    <h2 class="splash-title">Connect to TeslaDrive</h2>
    <p class="splash-sub">Start a 5 minute session to browse, download, and add files. When time is up, TeslaDrive switches back to the car automatically.</p>
    <div class="splash-actions">
      <button class="btn btn-solid" type="button" onclick="startTimedSession()">Start 5 minute session</button>
      <button class="btn" type="button" onclick="hideSplash()">View only</button>
    </div>
  </div>
</div>
<div id="extendModal" class="extend-modal hidden">
  <div class="extend-card">
    <h2 class="extend-title">Need more time?</h2>
    <p class="extend-sub">This transfer session will automatically end in about 2 minutes so the Tesla can see the USB drives again.</p>
    <div class="extend-actions">
      <button class="btn btn-solid" type="button" onclick="extendSession()">Extend 5 minutes</button>
      <button class="btn btn-danger" type="button" onclick="endSessionNow()">End session now</button>
    </div>
  </div>
</div>
<div id="videoModal" class="video-modal hidden" onclick="closeVideo()">
  <div class="video-shell" onclick="event.stopPropagation()">
    <div class="video-top">
      <div id="videoTitle" class="video-title mono"></div>
      <button class="btn video-close" type="button" onclick="closeVideo()">Close</button>
    </div>
    <div id="videoGrid" class="video-grid"></div>
    <div id="videoModebar" class="video-modebar"></div>
    <div class="video-actions"><button class="btn" type="button" onclick="toggleVideoDownloads()"><span>Download clips</span></button></div>
    <div id="videoDownloadPanel" class="video-download-panel hidden"></div>
    <div class="video-hint"><svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3 2 21h20L12 3Zm0 6v6m0 3v.5"/></svg><span>Dashcam videos are large. The first play can take a moment on phones, especially outside hotspot mode.</span></div>
  </div>
</div>
<div id="editModal" class="edit-modal hidden" onclick="closeEditModal()">
  <div class="edit-card" onclick="event.stopPropagation()">
    <div class="edit-head">
      <div id="editTitle" class="edit-title">Edit file</div>
      <button class="btn btn-sm" type="button" onclick="closeEditModal()">Close</button>
    </div>
    <div id="editBody"></div>
  </div>
</div>
<script>
/* ============== icon paths ============== */
const ICONS = {
  videocam: "M2 7h13v10H2zM15 10l6-3v10l-6-3z",
  camera: "M4 7h3l1.5-2h7L17 7h3v12H4z M9 13a3 3 0 1 0 6 0a3 3 0 1 0-6 0",
  music: "M9 18V6l11-2v12M9 18a2 2 0 1 1-4 0 2 2 0 0 1 4 0Zm11-4a2 2 0 1 1-4 0 2 2 0 0 1 4 0Z",
  disco: "M12 3a7 7 0 0 1 7 7c0 5-7 11-7 11S5 15 5 10a7 7 0 0 1 7-7z M8 9h8M7 12h10M9 15h6M12 3v18",
  sparkles: "M10 6L11.5 10.5L16 12L11.5 13.5L10 18L8.5 13.5L4 12L8.5 10.5z M18 4L18.5 5.5L20 6L18.5 6.5L18 8L17.5 6.5L16 6L17.5 5.5z M19 16.5L19.5 17.5L20.5 18L19.5 18.5L19 19.5L18.5 18.5L17.5 18L18.5 17.5z",
  bell: "M6 8v5l-2 3h16l-2-3V8a6 6 0 0 0-12 0z M9 19a3 3 0 0 0 6 0",
  upload: "M12 16V4M6 10l6-6 6 6M4 20h16",
  download: "M12 4v12m-6-6 6 6 6-6M4 20h16",
  edit: "M4 20h4L19 9l-4-4L4 16v4zM13 7l4 4",
  dots: "M5 12h.01M12 12h.01M19 12h.01",
  trash: "M5 7h14M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3",
  back: "M15 6l-6 6 6 6",
  folder: "M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z",
  file: "M6 3h9l5 5v13H6z M14 3v6h6",
  warn: "M12 3 2 21h20L12 3Zm0 6v6m0 3v.5",
  check: "M4 12l5 5L20 6",
  play: "M6 4l14 8-14 8z",
  pause: "M8 5v14M16 5v14",
  x: "M6 6l12 12M18 6 6 18",
};
function svgIcon(name, size = 18, stroke = 1.5) {
  const d = ICONS[name];
  if (!d) return "";
  return `<svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="${stroke}" stroke-linecap="round" stroke-linejoin="round"><path d="${d}"/></svg>`;
}

/* ============== tile / folder config ============== */
const TILES = [
  { id: "dashcam",   drive: "cam",    icon: "videocam", label: "Dash cam",    countLabel: "files" },
  { id: "photobooth",drive: "cam",    icon: "camera",   label: "Photobooth",  countLabel: "photos" },
  { id: "music",     drive: "music",  icon: "music",    label: "Music",       countLabel: "tracks" },
  { id: "lightshow", drive: "sounds", icon: "sparkles", label: "Light shows", countLabel: "shows" },
  { id: "chime",     drive: "sounds", icon: "bell",     label: "Lock chime",  countLabel: "active" },
];

const DASHCAM_FOLDERS = [
  { key: "TeslaCam/SavedClips",     label: "Saved" },
  { key: "TeslaCam/SentryClips",    label: "Sentry" },
  { key: "TeslaCam/RecentClips",    label: "Recent" },
  { key: "TeslaCam/EncryptedClips", label: "Encrypted" },
];

const SETTINGS_SECTIONS = [
  { id: "connection",   label: "Connection",   desc: "Portal host" },
  { id: "activity",     label: "Activity",     desc: "Recent log" },
];

/* ============== state ============== */
let status = null;
let currentPage = "home";
let dashcamFolder = "TeslaCam/SavedClips";
let dashcamItems = [];
let dashcamTotalEntries = 0;
let expandedClipGroups = new Set();
let musicItems = [];
let lightshowItems = [];
let rejections = { music: [], lightshow: [] };
let settingsSection = "connection";
let sessionDeadline = 0;
let extendPromptShown = false;
let dashcamPage = 1;
let videoViewer = { playing: false, syncing: false, files: [], title: "", key: "", activeCamera: "" };
const DASHCAM_PAGE_SIZE = 8;
const SESSION_MS = 5 * 60 * 1000;
const EXTEND_PROMPT_MS = 60 * 1000;
const URGENT_PROMPT_MS = 30 * 1000;

/* ============== utilities ============== */
function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, ch => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch]));
}
function jsStr(value) { return JSON.stringify(String(value ?? "")); }
function jsAttr(value) { return esc(jsStr(value)); }
function fmtBytes(n) {
  if (n == null) return "—";
  const units = ["B","KB","MB","GB","TB"];
  let v = Number(n) || 0;
  for (const u of units) { if (v < 1024 || u === "TB") return u === "B" ? `${v} B` : `${v.toFixed(1)} ${u}`; v /= 1024; }
}
function pct(usage) { return usage && usage.total ? Math.max(0, Math.min(100, (usage.used / usage.total) * 100)) : 0; }
function extOf(name) { return String(name || "").split(".").pop().toLowerCase(); }
function isAudio(item) { return !item.is_dir && ["mp3","wav","m4a","aac","flac"].includes(extOf(item.name)); }
function isVideo(item) { return !item.is_dir && ["mp4","mov","m4v"].includes(extOf(item.name)); }
function isImage(item) { return !item.is_dir && ["jpg","jpeg","png","webp"].includes(extOf(item.name)); }
function inlineUrl(url) { return `${url}${String(url).includes("?") ? "&" : "?"}inline=1`; }
function toast(msg, kind) {
  const el = document.getElementById("toast");
  el.textContent = msg;
  el.className = "toast" + (kind === "err" ? " err" : "");
  setTimeout(() => el.classList.add("hidden"), 3000);
  el.classList.remove("hidden");
}
async function api(path, options) {
  const res = await fetch(path, options);
  const ct = res.headers.get("content-type") || "";
  const data = ct.includes("application/json") ? await res.json() : { error: await res.text() };
  if (!res.ok) throw new Error(data.error || `Request failed (${res.status})`);
  return data;
}

function uploadWithProgress(path, body, onProgress) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", path);
    xhr.upload.onprogress = event => {
      if (event.lengthComputable && onProgress) onProgress(Math.round((event.loaded / event.total) * 100));
    };
    xhr.onload = () => {
      let data = {};
      try { data = JSON.parse(xhr.responseText || "{}"); } catch (_) { data = { error: xhr.responseText || "Upload failed" }; }
      if (xhr.status >= 200 && xhr.status < 300) resolve(data);
      else reject(new Error(data.error || `Request failed (${xhr.status})`));
    };
    xhr.onerror = () => reject(new Error("Network error during upload."));
    xhr.send(body);
  });
}

function pauseAllMedia() {
  document.querySelectorAll("audio, video:not(.dash-thumb)").forEach(el => {
    try { el.pause(); } catch (_) { /* ignore */ }
  });
}

function audioItemsFor(pageId) {
  const items = pageId === "lightshow" ? lightshowItems : musicItems;
  return (items || []).filter(isAudio);
}

function renderAudioNote(pageId) {
  const wrap = document.getElementById(`${pageId}Player`);
  if (!wrap) return;
  wrap.innerHTML = `<div class="audio-note">${svgIcon("warn", 15, 1.7)}<span>Large lossless files can lag on phones. MP3 or AAC previews usually play smoother, especially away from hotspot mode.</span></div>`;
}

function viewerVideos() {
  return Array.from(document.querySelectorAll("#videoGrid video"));
}

function updateVideoViewerUI() {
  renderVideoModebar();
}

function buildVideoCell(slot, file, single = false) {
  const key = file ? cameraKey(file.name) : slot;
  const label = file ? cameraLabel(file.name) : cameraSlotLabel(slot);
  const fileName = file ? file.name : "Missing";
  if (!file) {
    return `<div class="video-cell video-cell-${slot} ${single ? "video-cell-single" : ""}">
      <div class="video-cell-label"><span>${esc(label)}</span><span class="mono">not found</span></div>
      <div class="video-player"></div>
    </div>`;
  }
  return `<div class="video-cell video-cell-${slot} ${single ? "video-cell-single" : ""}">
    <div class="video-cell-label"><span>${esc(label)}</span><span class="mono">${esc(fileName)}</span></div>
    <video class="video-player" data-camera="${esc(key)}" src="${inlineUrl(file.download)}" playsinline controls preload="metadata"></video>
  </div>`;
}

function videoFilesByCamera(files) {
  return Object.fromEntries((files || []).map(file => [cameraKey(file.name), file]));
}

function renderVideoModebar() {
  const bar = document.getElementById("videoModebar");
  const files = (videoViewer.files || []).slice().sort((a, b) => cameraRank(a.name) - cameraRank(b.name) || cameraLabel(a.name).localeCompare(cameraLabel(b.name)));
  if (files.length <= 1) {
    bar.innerHTML = "";
    return;
  }
  bar.innerHTML = files.map(file => {
    const key = cameraKey(file.name) || file.name;
    return `<button class="btn camera-btn ${videoViewer.activeCamera === key ? "on" : ""}" type="button" onclick="setVideoCamera(${jsAttr(key)})">${esc(cameraLabel(file.name))}</button>`;
  }).join("");
}

function renderVideoDownloadPanel(forceShow = null) {
  const panel = document.getElementById("videoDownloadPanel");
  if (!panel) return;
  const wasOpen = !panel.classList.contains("hidden");
  const show = forceShow == null ? wasOpen : forceShow;
  const files = (videoViewer.files || []).slice().sort((a, b) => cameraRank(a.name) - cameraRank(b.name) || cameraLabel(a.name).localeCompare(cameraLabel(b.name)));
  if (!show || !files.length) {
    panel.classList.add("hidden");
    panel.innerHTML = "";
    return;
  }
  panel.innerHTML = files.map(file => `
    <a class="btn download-angle" href="${file.download}" onclick="event.stopPropagation()">
      ${svgIcon("download", 14)}<span>${esc(cameraLabel(file.name))}</span>
    </a>
  `).join("");
  panel.classList.remove("hidden");
}

function toggleVideoDownloads() {
  const panel = document.getElementById("videoDownloadPanel");
  renderVideoDownloadPanel(panel?.classList.contains("hidden"));
}

function renderVideoGrid() {
  const grid = document.getElementById("videoGrid");
  const files = videoViewer.files;
  const byCamera = videoFilesByCamera(files);
  renderVideoModebar();
  grid.className = "video-grid";
  const active = byCamera[videoViewer.activeCamera] || byCamera.front || files[0];
  grid.innerHTML = buildVideoCell(cameraKey(active?.name) || "front", active, true);
  renderVideoDownloadPanel(false);
  wireViewerVideos();
  updateVideoViewerUI();
}

function setVideoCamera(camera) {
  const current = viewerVideos()[0]?.currentTime || 0;
  videoViewer.activeCamera = camera;
  videoViewer.playing = false;
  renderVideoGrid();
  const video = viewerVideos()[0];
  if (video && current) {
    video.currentTime = current;
  }
}

function wireViewerVideos() {
  for (const video of viewerVideos()) {
    video.addEventListener("loadedmetadata", updateVideoViewerUI);
    video.addEventListener("timeupdate", updateVideoViewerUI);
    video.addEventListener("play", () => {
      videoViewer.playing = true;
      updateVideoViewerUI();
    });
    video.addEventListener("pause", () => {
      if (viewerVideos().every(v => v.paused)) {
        videoViewer.playing = false;
        updateVideoViewerUI();
      }
    });
  }
}

function openVideoViewer(files, title, key = "") {
  pauseAllMedia();
  const videoFiles = (Array.isArray(files) ? files : []).slice().sort((a, b) => cameraRank(a.name) - cameraRank(b.name) || cameraLabel(a.name).localeCompare(cameraLabel(b.name)));
  const front = videoFiles.find(file => cameraKey(file.name) === "front");
  videoViewer = { playing: false, syncing: false, files: videoFiles, title: title || "Dashcam viewer", key, activeCamera: cameraKey(front?.name || videoFiles[0]?.name) || "" };
  const modal = document.getElementById("videoModal");
  const videoTitle = document.getElementById("videoTitle");
  videoTitle.textContent = videoViewer.title;
  renderVideoGrid();
  modal.classList.remove("hidden");
  updateVideoViewerUI();
}

function playVideo(url, title) {
  openVideoViewer([{ name: title || "Video", download: url }], title || "Video");
}

function closeVideo() {
  const modal = document.getElementById("videoModal");
  for (const video of viewerVideos()) {
    video.pause();
    video.removeAttribute("src");
    video.load();
  }
  document.getElementById("videoGrid").innerHTML = "";
  document.getElementById("videoModebar").innerHTML = "";
  document.getElementById("videoDownloadPanel").innerHTML = "";
  document.getElementById("videoDownloadPanel").classList.add("hidden");
  videoViewer = { playing: false, syncing: false, files: [], title: "", key: "", activeCamera: "" };
  modal.classList.add("hidden");
}

/* ============== status refresh ============== */
async function refresh() {
  try {
    status = await api("/api/status");
    renderBrand();
    renderTopbarStatus();
    reconcileSessionTimer();
    if (currentPage === "home")      renderHome();
    if (currentPage === "dashcam")   await loadDashcam();
    if (currentPage === "photobooth") await loadPhotobooth();
    if (currentPage === "music")     await loadFolder("music",     "music",  "Music",     "musicTable", "musicInfo", v => musicItems = v);
    if (currentPage === "lightshow") await loadFolder("lightshow", "sounds", "LightShow", "lightshowTable", "lightshowInfo", v => lightshowItems = v);
    if (currentPage === "chime")     await loadChime();
    if (currentPage === "settings")  renderSettings();
  } catch (e) {
    toast(e.message, "err");
  }
}

async function refreshStatusOnly() {
  try {
    status = await api("/api/status");
    renderTopbarStatus();
    reconcileSessionTimer();
    if (currentPage === "music" && status.drives?.music?.mounted && !musicItems.length && (status.home_counts?.music || 0) > 0) {
      await loadFolder("music", "music", "Music", "musicTable", "musicInfo", v => musicItems = v);
    }
    if (currentPage === "lightshow" && status.drives?.sounds?.mounted && !lightshowItems.length && (status.home_counts?.lightshow || 0) > 0) {
      await loadFolder("lightshow", "sounds", "LightShow", "lightshowTable", "lightshowInfo", v => lightshowItems = v);
    }
    if (currentPage === "home") renderHome();
    if (currentPage === "settings") renderSettings();
  } catch (e) {
    toast(e.message, "err");
  }
}

/* ============== brand / topbar ============== */
function renderBrand() {
  const slot = document.getElementById("brandSlot");
  if (currentPage === "home") {
    slot.innerHTML = `<div class="brand-name">TeslaDrive</div>`;
  } else {
    slot.innerHTML = `<button class="back-btn" type="button" onclick="showPage('home')">${svgIcon("back", 16)}<span>Home</span></button>`;
  }
}

function renderTopbarStatus() {
  const usb = status.usb || "unknown";
  const usbConnected = usb === "connected";
  document.getElementById("usbDot").className = `dot ${usbConnected ? "ok" : "warn"}`;
  document.getElementById("usbText").textContent = `usb ${usb}`;

  document.getElementById("sessionDot").className = `dot ${status.session_active ? "warn" : "ok"}`;
  document.getElementById("sessionText").textContent = status.session_active ? sessionLabel() : "car mode";

  const btn = document.getElementById("sessionButton");
  btn.textContent = status.session_active ? "End session" : "Start session";
  btn.className = "btn " + (status.session_active ? "btn-danger" : "btn-solid");
  btn.onclick = status.session_active ? endSessionNow : startTimedSession;
}

async function toggleSession() {
  return status?.session_active ? endSessionNow() : startTimedSession();
}

function hideSplash() {
  document.getElementById("splash").classList.add("hidden");
}

function showSplashIfNeeded() {
  const splash = document.getElementById("splash");
  if (!status?.session_active) splash.classList.remove("hidden");
}

function renderSessionFloat(show = null) {
  const el = document.getElementById("sessionFloat");
  if (!el) return;
  const remaining = sessionDeadline ? sessionDeadline - Date.now() : 0;
  if (show == null) show = status?.session_active && extendPromptShown && remaining <= EXTEND_PROMPT_MS;
  if (!show) {
    el.classList.add("hidden");
    el.classList.remove("urgent");
    el.innerHTML = "";
    return;
  }
  const urgent = remaining <= URGENT_PROMPT_MS;
  const remainingLabel = sessionLabel().replace("session ", "");
  el.className = `session-float ${urgent ? "urgent" : ""}`;
  el.innerHTML = `
    <div>
      <div class="session-float-title">Need more time? ${esc(remainingLabel)}</div>
      <div class="session-float-sub">${urgent ? "TeslaDrive is switching back soon." : "Your transfer session ends soon."}</div>
    </div>
    <div class="session-float-actions">
      <button class="btn btn-solid btn-sm" type="button" onclick="extendSession()">Extend 5 minutes</button>
      <button class="btn btn-danger btn-sm" type="button" onclick="endSessionNow()">End session now</button>
    </div>`;
}

function sessionLabel() {
  if (!sessionDeadline) return "transfer session";
  const remaining = Math.max(0, sessionDeadline - Date.now());
  const mins = Math.floor(remaining / 60000);
  const secs = Math.floor((remaining % 60000) / 1000);
  return `session ${mins}:${String(secs).padStart(2, "0")}`;
}

function reconcileSessionTimer() {
  if (status?.session_active) {
    hideSplash();
    if (status.session_expires_at) {
      sessionDeadline = status.session_expires_at * 1000;
    } else if (!sessionDeadline || sessionDeadline < Date.now()) {
      sessionDeadline = Date.now() + ((status.session_timeout_seconds || 300) * 1000);
      extendPromptShown = false;
    }
  } else {
    sessionDeadline = 0;
    extendPromptShown = false;
    document.getElementById("extendModal").classList.add("hidden");
    renderSessionFloat(false);
    showSplashIfNeeded();
  }
}

async function startTimedSession() {
  try {
    hideSplash();
    const nextStatus = await api("/session/start", { method: "POST" });
    sessionDeadline = nextStatus.session_expires_at ? nextStatus.session_expires_at * 1000 : Date.now() + SESSION_MS;
    extendPromptShown = false;
    toast("Transfer session started");
    await refresh();
    setTimeout(() => refresh().catch(() => {}), 1200);
  } catch (e) { toast(e.message, "err"); }
}

async function endSessionNow() {
  hideSplash();
  try {
    await api("/session/stop", { method: "POST" });
    sessionDeadline = 0;
    extendPromptShown = false;
    document.getElementById("extendModal").classList.add("hidden");
    renderSessionFloat(false);
    toast("Transfer session ended");
    await refresh();
  } catch (e) { toast(e.message, "err"); }
}

async function extendSession() {
  hideSplash();
  try {
    const nextStatus = await api("/session/extend", { method: "POST" });
    sessionDeadline = nextStatus.session_expires_at ? nextStatus.session_expires_at * 1000 : Date.now() + SESSION_MS;
    extendPromptShown = false;
    document.getElementById("extendModal").classList.add("hidden");
    renderSessionFloat(false);
    toast("Session extended 5 minutes");
    await refresh();
  } catch (e) { toast(e.message, "err"); }
}

function timerTick() {
  if (!status?.session_active || !sessionDeadline) return;
  const remaining = sessionDeadline - Date.now();
  renderTopbarStatus();
  renderSessionFloat();
  if (remaining <= 0) {
    endSessionNow();
    return;
  }
  if (remaining <= EXTEND_PROMPT_MS && !extendPromptShown) {
    extendPromptShown = true;
    hideSplash();
    document.getElementById("extendModal").classList.add("hidden");
    renderSessionFloat(true);
  }
}

/* ============== HOME ============== */
function renderHome() {
  const wrap = document.getElementById("homeTiles");
  wrap.innerHTML = TILES.map(t => {
    const drive = status.drives[t.drive];
    let count;
    if (t.id === "chime") {
      count = drive?.mounted ? String(status.home_counts?.chime ?? 0) : "—";
    } else if (status.home_counts && status.home_counts[t.id] != null) {
      count = drive?.mounted ? String(status.home_counts[t.id]) : "—";
    } else {
      count = drive?.mounted ? (drive.files || 0) : "—";
    }
    const usage = drive?.usage;
    const meterTone = t.id === "chime" ? (drive?.mounted ? "ok" : "err") : "";
    const meterWidth = t.id === "chime" ? (drive?.mounted ? 100 : 0) : pct(usage);
    let footRight = t.id === "chime"
      ? (drive?.mounted ? (status.home_counts?.chime ? "LockChime.wav" : "no chime") : "not mounted")
      : (usage ? `${fmtBytes(usage.used)} / ${fmtBytes(usage.total)}` : "not mounted");
    if (t.id === "photobooth" && status.home_folder_sizes?.photobooth != null) {
      footRight = `${fmtBytes(status.home_folder_sizes.photobooth)} used`;
    }
    return `<button class="home-tile" type="button" onclick="showPage('${t.id}')">
      <div class="home-tile-icon">${svgIcon(t.icon, 26, 1.2)}</div>
      <div class="home-tile-num">${esc(String(count))}</div>
      <div class="home-tile-num-label">${esc(t.countLabel)}</div>
      <div class="home-tile-body">
        <div class="home-tile-label">${esc(t.label)}</div>
      </div>
      <div class="home-tile-foot">
        <div class="meter-track"><div class="meter-fill ${meterTone}" style="width:${meterWidth}%"></div></div>
        <div class="home-tile-foot-t mono"><span>${esc(drive?.label || "")}</span><span>${esc(footRight)}</span></div>
      </div>
    </button>`;
  }).join("");
}

/* ============== session banner ============== */
function sessionBanner(message) {
  if (status.session_active) return "";
  return `<div class="banner banner-warn">
    <div class="banner-icon">${svgIcon("warn", 18, 1.5)}</div>
    <div>
      <div class="banner-title">Transfer session not active</div>
      <div class="banner-sub">${esc(message || "Start a session to mount the drives and enable file actions.")}</div>
    </div>
    <button class="btn btn-solid btn-sm" onclick="toggleSession()">Start session</button>
  </div>`;
}

/* ============== folder rendering shared ============== */
function previewCell(item, drive) {
  if (drive === "music" && isAudio(item)) {
    return item.art
      ? `<span class="album-wrap"><img class="album-art" src="${item.art}" alt="" loading="lazy" onerror="this.parentElement.classList.add('art-missing')"><span class="album-fallback">${svgIcon("music", 19)}</span></span>`
      : `<span class="album-fallback">${svgIcon("music", 19)}</span>`;
  }
  if (drive === "sounds" && extOf(item.name) === "fseq") return `<span class="media-icon">${svgIcon("sparkles", 21, 1.35)}</span>`;
  if (drive === "sounds" && isAudio(item)) return `<span class="media-icon">${svgIcon("music", 21, 1.35)}</span>`;
  if (item.is_dir && item.thumbnail) return `<img class="dash-thumb" src="${item.thumbnail}" alt="" loading="lazy">`;
  if (isVideo(item)) return `<button class="dash-thumb icon-btn" type="button" title="Play video" onclick="event.stopPropagation(); playVideo(${jsAttr(inlineUrl(item.download))}, ${jsAttr(item.name)})">${svgIcon("play", 17)}</button>`;
  if (isImage(item)) return `<img class="dash-thumb" src="${inlineUrl(item.download)}" alt="" loading="lazy">`;
  return svgIcon(item.is_dir ? "folder" : "file", 15, 1.4);
}

function audioAction(item) {
  return isAudio(item) ? `<audio class="audio-preview" controls preload="none" src="${inlineUrl(item.download)}" onclick="event.stopPropagation()"></audio>` : "";
}

function canUseMediaMenu(item, drive) {
  if (item.is_dir) return false;
  return (drive === "music" && posixpathParent(item.path) === "Music")
    || (drive === "sounds" && posixpathParent(item.path) === "LightShow");
}

function posixpathParent(path) {
  const parts = String(path || "").split("/");
  parts.pop();
  return parts.join("/");
}

function mediaMenu(item, drive) {
  if (!canUseMediaMenu(item, drive)) return "";
  const edit = drive === "music" && ["mp3", "flac", "m4a", "aac"].includes(extOf(item.name))
    ? `<button class="file-menu-item" type="button" onclick="openMusicEditor(${jsAttr(item.path)})">${svgIcon("edit", 14)}<span>Edit</span></button>`
    : drive === "sounds"
      ? `<button class="file-menu-item" type="button" onclick="openRenameEditor(${jsAttr(item.path)}, ${jsAttr(item.name)})">${svgIcon("edit", 14)}<span>Edit</span></button>`
      : "";
  const id = `menu-${Math.random().toString(36).slice(2)}`;
  return `<span class="menu-wrap">
    <button class="icon-btn" title="More" type="button" onclick="event.stopPropagation(); toggleFileMenu(${jsAttr(id)})">${svgIcon("dots", 16, 2.4)}</button>
    <span id="${id}" class="file-menu hidden" onclick="event.stopPropagation()">
      <a class="file-menu-item" href="${item.download}" onclick="closeFileMenus()">${svgIcon("download", 14)}<span>Download</span></a>
      ${edit}
      <button class="file-menu-item danger" type="button" onclick="deleteMediaFile(${jsAttr(drive)}, ${jsAttr(item.path)}, ${jsAttr(item.name)})">${svgIcon("trash", 14)}<span>Delete</span></button>
    </span>
  </span>`;
}

function fileRow(item, drive, onDelete, onOpen) {
  const playAction = isVideo(item)
    ? `<button class="icon-btn" title="Play" onclick="event.stopPropagation(); playVideo(${jsAttr(inlineUrl(item.download))}, ${jsAttr(item.name)})">${svgIcon("play", 14)}</button>`
    : "";
  const menuAction = mediaMenu(item, drive);
  const actions = item.is_dir
    ? ""
    : `<span class="audio-row-actions">${audioAction(item)}
       ${playAction}
       ${menuAction || `<a class="icon-btn" href="${item.download}" title="Download" onclick="event.stopPropagation()">${svgIcon("download", 14)}</a>`}
       ${status.deletes_enabled && status.session_active ? `<button class="icon-btn icon-btn-danger" title="Delete" onclick="event.stopPropagation(); ${onDelete}">${svgIcon("trash", 14)}</button>` : ""}</span>`;
  const click = item.is_dir
    ? `onclick="${onOpen || ""}"`
    : isVideo(item)
      ? `onclick="playVideo(${jsAttr(inlineUrl(item.download))}, ${jsAttr(item.name)})"`
      : `onclick="window.location.href='${item.download}'"`;
  return `<tr ${click}>
    <td class="file-tbl-icon">${previewCell(item, drive)}</td>
    <td class="file-name">${esc(item.name)}</td>
    <td class="mono num-faint">${esc(item.size_label || (item.is_dir ? "folder" : ""))}</td>
    <td class="file-tbl-actions">${actions}</td>
  </tr>`;
}

function emptyRow(message, sub) {
  return `<tr><td colspan="4" class="file-empty"><div class="file-empty-h">${esc(message)}</div><div class="file-empty-s mono">${esc(sub || "")}</div></td></tr>`;
}

function clipGroupKey(item) {
  const match = String(item.name || "").match(/^(.+?)-(back|front|left_pillar|left_repeater|right_pillar|right_repeater)\.mp4$/i);
  return match ? match[1] : "";
}

function cameraLabel(name) {
  const match = String(name || "").match(/-(back|front|left_pillar|left_repeater|right_pillar|right_repeater)\.mp4$/i);
  if (!match) return "Clip";
  return match[1].replace(/_/g, " ").replace(/\b\w/g, ch => ch.toUpperCase());
}

function cameraSlotLabel(slot) {
  return {
    front: "Front",
    back: "Back",
    left: "Left repeater",
    right: "Right repeater",
    left_repeater: "Left repeater",
    right_repeater: "Right repeater",
    left_pillar: "Left pillar",
    right_pillar: "Right pillar",
  }[slot] || "Camera";
}

function cameraKey(name) {
  const match = String(name || "").match(/-(back|front|left_pillar|left_repeater|right_pillar|right_repeater)\.mp4$/i);
  return match ? match[1].toLowerCase() : "";
}

function cameraRank(name) {
  const order = ["front", "back", "left_repeater", "right_repeater", "left_pillar", "right_pillar"];
  const index = order.indexOf(cameraKey(name));
  return index === -1 ? order.length : index;
}

function formatClipTime(key) {
  const match = String(key || "").match(/^(\d{4})-(\d{2})-(\d{2})_(\d{2})-(\d{2})-(\d{2})$/);
  if (!match) return key;
  const [, y, mo, d, h, mi, s] = match;
  const date = new Date(`${y}-${mo}-${d}T${h}:${mi}:${s}`);
  return Number.isNaN(date.getTime()) ? key : date.toLocaleString([], { dateStyle: "medium", timeStyle: "short" });
}

function groupDashcamItems(items) {
  const grouped = new Map();
  const passthrough = [];
  for (const item of items) {
    if (item.is_dir) {
      passthrough.push({ type: "item", item });
      continue;
    }
    const key = clipGroupKey(item);
    if (!key) {
      passthrough.push({ type: "item", item });
      continue;
    }
    if (!grouped.has(key)) grouped.set(key, []);
    grouped.get(key).push(item);
  }
  const groups = Array.from(grouped.entries())
    .map(([key, files]) => ({ type: "group", key, files: files.sort((a, b) => cameraRank(a.name) - cameraRank(b.name) || cameraLabel(a.name).localeCompare(cameraLabel(b.name))) }))
    .sort((a, b) => b.key.localeCompare(a.key));
  return [...groups, ...passthrough];
}

function clipStack(files) {
  const front = files.find(file => cameraKey(file.name) === "front");
  const file = front || files[0];
  return `<span class="clip-stack"><video class="dash-thumb" src="${inlineUrl(file.download)}#t=0.1" muted preload="metadata" playsinline></video></span>`;
}

function clipGroupRow(group) {
  const totalSize = group.files.reduce((sum, file) => sum + (Number(file.size) || 0), 0);
  const expanded = expandedClipGroups.has(group.key);
  const summary = `${group.files.length} clips · ${fmtBytes(totalSize)}`;
  const filesHtml = expanded ? `<tr class="clip-expanded"><td colspan="4"><div class="clip-files">
    ${group.files.map(file => `<div class="clip-file">
      <span>${esc(cameraLabel(file.name))}</span>
      <span class="mono num-faint">${esc(file.size_label)}</span>
      <span class="clip-file-actions">
        <button class="icon-btn" title="Play" onclick="event.stopPropagation(); playVideo(${jsAttr(inlineUrl(file.download))}, ${jsAttr(file.name)})">${svgIcon("play", 14)}</button>
        <a class="icon-btn" href="${file.download}" title="Download" onclick="event.stopPropagation()">${svgIcon("download", 14)}</a>
        ${status.deletes_enabled && status.session_active ? `<button class="icon-btn icon-btn-danger" title="Delete" onclick="event.stopPropagation(); deleteItem('cam', ${jsAttr(file.path)})">${svgIcon("trash", 14)}</button>` : ""}
      </span>
    </div>`).join("")}
  </div></td></tr>` : "";
  return `<tr class="clip-row" data-clip-key="${esc(group.key)}" role="button" tabindex="0">
    <td class="file-tbl-icon">${clipStack(group.files)}</td>
    <td class="file-name"><span class="clip-summary"><span class="clip-title">${esc(formatClipTime(group.key))}</span><span class="clip-sub mono">${esc(group.key)}</span><span class="clip-sub mono">${esc(summary)}</span></span></td>
    <td class="mono num-faint clip-size-cell">${esc(summary)}</td>
    <td class="file-tbl-actions"></td>
  </tr>${filesHtml}`;
}

function dashcamFolderRow(item) {
  return `<tr class="dash-folder-row" data-dash-folder="${esc(encodeURIComponent(item.path))}" role="button" tabindex="0">
    <td class="file-tbl-icon">${previewCell(item, "cam")}</td>
    <td class="file-name"><span class="clip-summary"><span class="clip-title">${esc(item.name)}</span><span class="clip-sub mono">${esc(item.size_label || "folder")}</span></span></td>
    <td class="mono num-faint clip-size-cell">${esc(item.size_label || "folder")}</td>
    <td class="file-tbl-actions"></td>
  </tr>`;
}

function toggleClipGroup(key) {
  if (expandedClipGroups.has(key)) expandedClipGroups.delete(key);
  else expandedClipGroups.add(key);
  renderDashcamRows();
}

function filesForClipGroup(key) {
  return dashcamItems.filter(item => clipGroupKey(item) === key);
}

function openClipGroupViewer(key) {
  const files = filesForClipGroup(key);
  if (!files.length) return;
  openVideoViewer(files, formatClipTime(key), key);
}

function openDashcamFolder(encodedPath) {
  dashcamFolder = decodeURIComponent(encodedPath);
  dashcamItems = [];
  dashcamTotalEntries = 0;
  expandedClipGroups.clear();
  dashcamPage = 1;
  loadDashcam();
}

function setDashcamPage(page) {
  dashcamPage = Math.max(1, Number(page) || 1);
  expandedClipGroups.clear();
  loadDashcam();
}

function renderDashcamRows() {
  const tbody = document.getElementById("dashcamTable");
  if (!dashcamItems.length) {
    tbody.innerHTML = emptyRow("No clips here", "The car writes here when it records.");
    return;
  }
  const entries = groupDashcamItems(dashcamItems);
  const totalEntries = entries.length;
  const totalPages = Math.max(1, Math.ceil(totalEntries / DASHCAM_PAGE_SIZE));
  dashcamPage = Math.min(Math.max(1, dashcamPage), totalPages);
  const start = (dashcamPage - 1) * DASHCAM_PAGE_SIZE;
  const visible = entries.slice(start, start + DASHCAM_PAGE_SIZE);
  let rows = visible.map(entry => {
    if (entry.type === "group") return clipGroupRow(entry);
    const it = entry.item;
    if (it.is_dir) return dashcamFolderRow(it);
    return fileRow(it, "cam", `deleteItem('cam', ${jsStr(it.path)})`, "");
  }).join("");
  if (totalPages > 1) {
    const first = ((dashcamPage - 1) * DASHCAM_PAGE_SIZE) + 1;
    const last = Math.min(totalEntries, ((dashcamPage - 1) * DASHCAM_PAGE_SIZE) + visible.length);
    rows += `<tr class="clip-pager-row"><td colspan="4">
      <div class="clip-pager">
        <div class="clip-pager-actions">
          <button class="btn" type="button" ${dashcamPage <= 1 ? "disabled" : ""} onclick="setDashcamPage(${dashcamPage - 1})">${svgIcon("back", 14)}<span>Previous</span></button>
          <button class="btn" type="button" ${dashcamPage >= totalPages ? "disabled" : ""} onclick="setDashcamPage(${dashcamPage + 1})"><span>Next</span><span class="next-arrow">${svgIcon("back", 14)}</span></button>
        </div>
        <div class="clip-pager-info mono">Showing ${first}-${last} of ${totalEntries}<br>Page ${dashcamPage} of ${totalPages}</div>
      </div>
    </td></tr>`;
  }
  tbody.innerHTML = rows;
  wireDashcamRowClicks();
}

function wireDashcamRowClicks() {
  const tbody = document.getElementById("dashcamTable");
  tbody.querySelectorAll("[data-dash-folder]").forEach(row => {
    row.onclick = () => openDashcamFolder(row.dataset.dashFolder);
    row.onkeydown = event => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        openDashcamFolder(row.dataset.dashFolder);
      }
    };
  });
  tbody.querySelectorAll("[data-clip-key]").forEach(row => {
    row.onclick = () => openClipGroupViewer(row.dataset.clipKey);
    row.onkeydown = event => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        openClipGroupViewer(row.dataset.clipKey);
      }
    };
  });
}

/* ============== DASH CAM ============== */
async function loadDashcam() {
  document.getElementById("dashcamBanner").innerHTML = sessionBanner("The car writes here. Start a transfer session to browse and download clips.");
  const drive = status.drives.cam;
  document.getElementById("dashcamInfo").textContent = drive?.mounted ? `${drive.files} files on ${drive.label}` : "TESLADRIVE not mounted";

  // folder tabs
  document.getElementById("dashcamFolders").innerHTML = DASHCAM_FOLDERS.map(f => `
    <button class="folder-tab ${dashcamFolder === f.key ? "on" : ""}" onclick="dashcamFolder='${f.key}'; dashcamItems = []; dashcamTotalEntries = 0; expandedClipGroups.clear(); dashcamPage = 1; loadDashcam();">
      <div class="folder-tab-l">${esc(f.label)}</div>
    </button>
  `).join("");

  const tbody = document.getElementById("dashcamTable");
  if (!drive?.mounted) {
    tbody.innerHTML = emptyRow("Drive not mounted", "Start a transfer session to view clips.");
    return;
  }
  try {
    const list = await api(`/api/list?drive=cam&path=${encodeURIComponent(dashcamFolder)}`);
    dashcamItems = list.items || [];
    dashcamTotalEntries = list.total || dashcamItems.length;
    renderDashcamRows();
  } catch (e) {
    tbody.innerHTML = emptyRow("Could not load clips", e.message);
  }
}

async function loadPhotobooth() {
  document.getElementById("photoboothBanner").innerHTML = sessionBanner("Start a transfer session to browse and download Photobooth images.");
  const drive = status.drives.cam;
  const photoboothSize = status.home_folder_sizes?.photobooth;
  document.getElementById("photoboothInfo").textContent = drive?.mounted
    ? `${status.home_counts?.photobooth ?? 0} photos · ${photoboothSize != null ? fmtBytes(photoboothSize) : "0 B"}`
    : "Photobooth not mounted";
  const tbody = document.getElementById("photoboothTable");
  if (!drive?.mounted) {
    tbody.innerHTML = emptyRow("Drive not mounted", "Start a transfer session to view Photobooth images.");
    return;
  }
  try {
    const list = await api("/api/list?drive=cam&path=TeslaCam%2FPhotobooth");
    const items = list.items || [];
    if (items.length === 0) {
      tbody.innerHTML = emptyRow("No Photobooth files", "Photos will appear here after the car saves them.");
    } else {
      tbody.innerHTML = items.map(it => fileRow(
        it,
        "cam",
        `deleteItem('cam', ${jsStr(it.path)})`,
        ``
      )).join("");
    }
  } catch (e) {
    tbody.innerHTML = emptyRow("Could not load Photobooth", e.message);
  }
}

/* ============== MUSIC / LIGHT SHOWS — shared loader ============== */
async function loadFolder(pageId, drive, path, tableId, infoId, setItems) {
  const driveInfo = status.drives[drive];
  document.getElementById(`${pageId}Banner`).innerHTML = sessionBanner();
  const countLabel = pageId === "lightshow"
    ? `${status.home_counts?.lightshow ?? 0} shows`
    : `${status.home_counts?.music ?? driveInfo?.files ?? 0} tracks`;
  document.getElementById(infoId).textContent = driveInfo?.mounted
    ? `${countLabel} · ${driveInfo.usage ? fmtBytes(driveInfo.usage.used) + " / " + fmtBytes(driveInfo.usage.total) : ""}`
    : `${driveInfo?.label || ""} not mounted`;

  renderUploadZone(pageId);
  renderRejections(pageId);

  const tbody = document.getElementById(tableId);
  if (!driveInfo?.mounted) {
    tbody.innerHTML = emptyRow("Drive not mounted", "Start a transfer session to manage files.");
    setItems([]);
    return;
  }
  try {
    const list = await api(`/api/list?drive=${drive}&path=${encodeURIComponent(path)}`);
    const items = list.items || [];
    setItems(items);
    if (pageId === "music" || pageId === "lightshow") renderAudioNote(pageId);
    if (items.length === 0) {
      tbody.innerHTML = emptyRow("No files yet", "Drop files into the zone above to add them.");
    } else {
      tbody.innerHTML = items.map(it => fileRow(
        it,
        drive,
        `deleteItem(${jsStr(drive)}, ${jsStr(it.path)})`,
        ``
      )).join("");
    }
  } catch (e) {
    tbody.innerHTML = emptyRow("Could not load files", e.message);
  }
}

/* ============== UPLOAD ZONE ============== */
function uploadConfigFor(pageId) {
  if (pageId === "music") {
    return { target: "music", title: "Drop music here", note: "FLAC for lossless. MP3, WAV, M4A, AAC also accepted.", kinds: [".flac", ".mp3", ".wav", ".m4a", ".aac"], accept: "audio/*,.flac,.mp3,.wav,.m4a,.aac" };
  }
  if (pageId === "lightshow") {
    return { target: "lightshow", title: "Drop .fseq + paired audio", note: "Filenames must match — e.g. Funky Town.fseq + Funky Town.mp3.", kinds: [".fseq", ".mp3", ".wav"], accept: ".fseq,.mp3,.wav" };
  }
  if (pageId === "chime") {
    return { target: "lockchime", title: "Replace lock chime", note: "Drop a .wav file. The car uses the new chime after the next lock event.", kinds: [".wav"], accept: ".wav" };
  }
  return null;
}

function renderUploadZone(pageId) {
  const wrap = document.getElementById(`${pageId}Upload`);
  if (!wrap) return;
  if (!status.uploads_enabled || !status.session_active) {
    wrap.innerHTML = "";
    return;
  }
  const cfg = uploadConfigFor(pageId);
  if (!cfg) { wrap.innerHTML = ""; return; }
  wrap.innerHTML = `<div class="uz" id="uz-${pageId}">
    <div class="uz-icon">${svgIcon("upload", 22, 1.3)}</div>
    <div class="uz-body">
      <div class="uz-title">${esc(cfg.title)}</div>
      <div class="uz-note">${esc(cfg.note)}</div>
      <div class="uz-kinds mono">
        ${cfg.kinds.map(k => `<span class="uz-chip">${esc(k.replace(".",""))}</span>`).join("")}
      </div>
    </div>
    <div class="uz-actions">
      <button class="btn btn-solid" onclick="document.getElementById('uz-file-${pageId}').click()">${svgIcon("upload", 15)}<span>Choose files</span></button>
      <div id="uz-progress-${pageId}" class="upload-progress hidden">
        <div class="upload-progress-label"><span id="uz-progress-name-${pageId}">Uploading</span><span id="uz-progress-pct-${pageId}">0%</span></div>
        <div class="upload-progress-track"><div id="uz-progress-fill-${pageId}" class="upload-progress-fill"></div></div>
      </div>
    </div>
    <input id="uz-file-${pageId}" class="uz-file-input" type="file" accept="${cfg.accept}" multiple onchange="handleUpload('${pageId}', this.files)">
  </div>`;

  const uz = document.getElementById(`uz-${pageId}`);
  uz.addEventListener("dragover", e => { e.preventDefault(); uz.classList.add("uz-drag"); });
  uz.addEventListener("dragleave", () => uz.classList.remove("uz-drag"));
  uz.addEventListener("drop", e => {
    e.preventDefault();
    uz.classList.remove("uz-drag");
    handleUpload(pageId, e.dataTransfer.files);
  });
}

async function handleUpload(pageId, fileList) {
  const cfg = uploadConfigFor(pageId);
  if (!cfg) return;
  const files = Array.from(fileList || []);
  const progress = document.getElementById(`uz-progress-${pageId}`);
  const progressName = document.getElementById(`uz-progress-name-${pageId}`);
  const progressPct = document.getElementById(`uz-progress-pct-${pageId}`);
  const progressFill = document.getElementById(`uz-progress-fill-${pageId}`);
  for (const file of files) {
    const body = new FormData();
    body.append("target", cfg.target);
    body.append("file", file);
    try {
      if (progress) progress.classList.remove("hidden");
      if (progressName) progressName.textContent = file.name;
      if (progressPct) progressPct.textContent = "0%";
      if (progressFill) progressFill.style.width = "0%";
      const data = await uploadWithProgress("/upload", body, pct => {
        if (progressPct) progressPct.textContent = `${pct}%`;
        if (progressFill) progressFill.style.width = `${pct}%`;
      });
      toast(`Uploaded ${data.filename}`);
    } catch (e) {
      const reasonText = e.message || "Upload failed";
      const suggest = file.name
        .normalize("NFKD")
        .replace(/[\u2018\u2019\u201A\u201B\u2032]/g, "'")
        .replace(/[\u201C\u201D\u201E\u201F\u2033]/g, '"')
        .replace(/[\u2010-\u2015]/g, "-")
        .replace(/[^\x00-\x7F]/g, "");
      const bucket = pageId === "lightshow" ? "lightshow" : "music";
      rejections[bucket] = rejections[bucket] || [];
      rejections[bucket].push({ id: Date.now() + Math.random(), name: file.name, reason: reasonText, suggest: suggest !== file.name ? suggest : null });
      renderRejections(pageId);
    }
  }
  if (progress) progress.classList.add("hidden");
  await refresh();
}

function renderRejections(pageId) {
  const wrap = document.getElementById(`${pageId}Rejections`);
  if (!wrap) return;
  const bucket = pageId === "lightshow" ? "lightshow" : pageId === "music" ? "music" : null;
  if (!bucket || !rejections[bucket] || rejections[bucket].length === 0) { wrap.innerHTML = ""; return; }
  wrap.innerHTML = rejections[bucket].map(r => `
    <div class="rj-row">
      <span class="rj-icon">${svgIcon("warn", 14, 1.7)}</span>
      <div>
        <div class="rj-name mono">${esc(r.name)}</div>
        <div class="rj-reason">${esc(r.reason)}${r.suggest ? ` · try renaming to <span class="mono">${esc(r.suggest)}</span>` : ""}</div>
      </div>
      <button class="rj-dismiss" onclick="dismissRejection('${bucket}', ${r.id})">${svgIcon("x", 13)}</button>
    </div>
  `).join("");
}

function dismissRejection(bucket, id) {
  rejections[bucket] = (rejections[bucket] || []).filter(r => r.id !== id);
  renderRejections(bucket);
}

function closeEditModal() {
  document.getElementById("editModal").classList.add("hidden");
  document.getElementById("editBody").innerHTML = "";
}

function closeFileMenus() {
  document.querySelectorAll(".file-menu").forEach(menu => menu.classList.add("hidden"));
}

function toggleFileMenu(id) {
  const menu = document.getElementById(id);
  const wasHidden = menu?.classList.contains("hidden");
  closeFileMenus();
  if (menu && wasHidden) menu.classList.remove("hidden");
}

document.addEventListener("click", closeFileMenus);

async function openMusicEditor(path) {
  const item = musicItems.find(it => it.path === path) || { name: path.split("/").pop(), path };
  document.getElementById("editTitle").textContent = "Edit music";
  document.getElementById("editModal").classList.remove("hidden");
  document.getElementById("editBody").innerHTML = `<div class="file-empty-s mono">Loading metadata…</div>`;
  let meta = {};
  try {
    meta = await api(`/api/music-metadata?drive=music&path=${encodeURIComponent(path)}`);
  } catch (e) {
    meta = {};
    toast(e.message, "err");
  }
  document.getElementById("editBody").innerHTML = `
    <form class="edit-form" onsubmit="saveMusicMetadata(event, ${jsAttr(path)})">
      <div class="field"><label>File</label><input value="${esc(item.name)}" disabled></div>
      <div class="field"><label>Title</label><input name="title" value="${esc(meta.title || "")}" autocomplete="off"></div>
      <div class="field"><label>Artist</label><input name="artist" value="${esc(meta.artist || "")}" autocomplete="off"></div>
      <div class="field"><label>Album</label><input name="album" value="${esc(meta.album || "")}" autocomplete="off"></div>
      <div class="edit-grid">
        <div class="field"><label>Album art</label><input name="art" type="file" accept="image/png,image/jpeg"></div>
        <div class="field"><label>Track #</label><input name="track" value="${esc(meta.track || "")}" inputmode="numeric" autocomplete="off"></div>
      </div>
      <div class="edit-actions">
        <button class="btn" type="button" onclick="closeEditModal()">Cancel</button>
        <button class="btn btn-solid" type="submit">Save changes</button>
      </div>
    </form>`;
}

async function saveMusicMetadata(event, path) {
  event.preventDefault();
  const form = event.currentTarget;
  const body = new FormData(form);
  body.append("drive", "music");
  body.append("path", path);
  try {
    await uploadWithProgress("/api/music-metadata", body);
    toast("Music details updated");
    closeEditModal();
    await refresh();
  } catch (e) {
    toast(e.message, "err");
  }
}

function openRenameEditor(path, name) {
  document.getElementById("editTitle").textContent = "Rename light show file";
  document.getElementById("editModal").classList.remove("hidden");
  document.getElementById("editBody").innerHTML = `
    <form class="edit-form" onsubmit="saveRename(event, ${jsAttr(path)})">
      <div class="field"><label>File name</label><input name="new_name" value="${esc(name)}" autocomplete="off" required></div>
      <div class="file-empty-s">Keep the extension the same, and keep paired .fseq/audio names matching.</div>
      <div class="edit-actions">
        <button class="btn" type="button" onclick="closeEditModal()">Cancel</button>
        <button class="btn btn-solid" type="submit">Rename</button>
      </div>
    </form>`;
}

async function saveRename(event, path) {
  event.preventDefault();
  const newName = new FormData(event.currentTarget).get("new_name");
  try {
    await api("/api/rename", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ drive: "sounds", path, new_name: newName })
    });
    toast("Renamed");
    closeEditModal();
    await refresh();
  } catch (e) {
    toast(e.message, "err");
  }
}

/* ============== DELETE ============== */
function confirmDeleteMedia(drive, path, name) {
  document.getElementById("editTitle").textContent = "Delete file?";
  document.getElementById("editModal").classList.remove("hidden");
  document.getElementById("editBody").innerHTML = `
    <div class="edit-form">
      <div class="file-empty-s">This removes the file from TeslaDrive.</div>
      <div class="field"><label>File</label><input value="${esc(name || path)}" disabled></div>
      <div class="edit-actions">
        <button class="btn" type="button" onclick="closeEditModal()">Cancel</button>
        <button class="btn btn-danger" type="button" onclick="deleteMediaFileConfirmed(${jsAttr(drive)}, ${jsAttr(path)})">Delete</button>
      </div>
    </div>`;
}

function deleteMediaFile(drive, path, name) {
  closeFileMenus();
  confirmDeleteMedia(drive, path, name);
}

async function deleteMediaFileConfirmed(drive, path) {
  try {
    await api("/api/media-delete", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ drive, path }) });
    toast("Deleted");
    closeEditModal();
    await refresh();
  } catch (e) { toast(e.message, "err"); }
}

async function deleteItem(drive, path) {
  if (!status.deletes_enabled) { toast("Deletes are disabled", "err"); return; }
  if (!confirm(`Delete "${path}"? This cannot be undone.`)) return;
  try {
    await api("/api/delete", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ drive, path }) });
    toast("Deleted");
    await refresh();
  } catch (e) { toast(e.message, "err"); }
}

/* ============== LOCK CHIME ============== */
async function loadChime() {
  document.getElementById("chimeBanner").innerHTML = sessionBanner();
  const drive = status.drives.sounds;
  const wrap = document.getElementById("chimeBody");

  if (!drive?.mounted) {
    wrap.innerHTML = `<div class="lc-empty">
      <div class="lc-empty-h">TeslaExtras not mounted</div>
      <div class="file-empty-s mono">Start a transfer session to view or replace the lock chime.</div>
    </div>`;
    return;
  }

  let chime = null;
  try {
    const list = await api("/api/list?drive=sounds&path=");
    chime = (list.items || []).find(it => !it.is_dir && it.name.toLowerCase() === "lockchime.wav");
  } catch (e) { /* ignore */ }

  const upload = uploadConfigFor("chime");
  const uploadHtml = (status.uploads_enabled && status.session_active) ? `
    <div class="uz" id="uz-chime">
      <div class="uz-icon">${svgIcon("upload", 22, 1.3)}</div>
      <div class="uz-body">
        <div class="uz-title">${esc(upload.title)}</div>
        <div class="uz-note">${esc(upload.note)}</div>
        <div class="uz-kinds mono">${upload.kinds.map(k => `<span class="uz-chip">${esc(k.replace(".",""))}</span>`).join("")}</div>
      </div>
      <div class="uz-actions">
        <button class="btn btn-solid" onclick="document.getElementById('uz-file-chime').click()">${svgIcon("upload", 15)}<span>Choose file</span></button>
      </div>
      <input id="uz-file-chime" class="uz-file-input" type="file" accept=".wav" onchange="handleUpload('chime', this.files)">
    </div>` : "";

  const rulesHtml = `<div class="lc-rules">
    <div class="lc-rules-h">Rules</div>
    <ul class="lc-rules-list">
      <li><span class="dot-mark mono">•</span> Filename must be exactly <span class="mono">LockChime.wav</span></li>
      <li><span class="dot-mark mono">•</span> Only one chime is active at a time</li>
      <li><span class="dot-mark mono">•</span> Keep under 4 seconds — long files get clipped</li>
      <li><span class="dot-mark mono">•</span> 16-bit PCM WAV works best · MP3 is rejected</li>
    </ul>
  </div>`;

  if (!chime) {
    wrap.innerHTML = `<div class="lc-grid">
      <div class="lc-empty">
        <div class="lc-empty-h">No lock chime yet</div>
        <div class="file-empty-s mono">Upload a .wav file to set one.</div>
      </div>
      <div class="lc-side">${uploadHtml}${rulesHtml}</div>
    </div>`;
    bindUz("chime");
    return;
  }

  wrap.innerHTML = `<div class="lc-grid">
    <div class="lc-current">
      <div class="lc-current-h">Currently on car</div>
      <div class="lc-name">${esc(chime.name)}</div>
      <div class="lc-info">
        <div class="lc-info-r"><span class="lc-info-k">SIZE</span><span class="lc-info-v mono">${esc(chime.size_label)}</span></div>
        <div class="lc-info-r"><span class="lc-info-k">UPDATED</span><span class="lc-info-v mono">${new Date(chime.modified * 1000).toLocaleString()}</span></div>
      </div>
      <div class="lc-actions">
        <audio class="audio-preview" controls preload="none" src="${inlineUrl(chime.download)}"></audio>
        <a class="btn" href="${chime.download}">${svgIcon("download", 15)}<span>Download</span></a>
        ${status.deletes_enabled && status.session_active ? `<button class="btn btn-danger" onclick="deleteItem('sounds',${jsStr(chime.path)})">${svgIcon("trash", 15)}<span>Remove from car</span></button>` : ""}
      </div>
    </div>
    <div class="lc-side">${uploadHtml}${rulesHtml}</div>
  </div>`;
  bindUz("chime");
}

function bindUz(pageId) {
  const uz = document.getElementById(`uz-${pageId}`);
  if (!uz) return;
  uz.addEventListener("dragover", e => { e.preventDefault(); uz.classList.add("uz-drag"); });
  uz.addEventListener("dragleave", () => uz.classList.remove("uz-drag"));
  uz.addEventListener("drop", e => {
    e.preventDefault();
    uz.classList.remove("uz-drag");
    handleUpload(pageId, e.dataTransfer.files);
  });
}

/* ============== SETTINGS ============== */
function renderSettings() {
  document.getElementById("setNav").innerHTML = SETTINGS_SECTIONS.map(s => `
    <button class="set-nav-i ${settingsSection === s.id ? "on" : ""}" onclick="settingsSection='${s.id}'; renderSettings();">
      <div class="set-nav-l">${esc(s.label)}</div>
      <div class="set-nav-d mono">${esc(s.desc)}</div>
    </button>
  `).join("");

  const main = document.getElementById("setMain");
  if (settingsSection === "connection") {
    const mounted = Object.values(status.drives || {}).filter(d => d.mounted).length;
    const hotspot = status.hotspot || {};
    main.innerHTML = `<div class="card card-pad">
      <h2 class="set-section-title">Portal</h2>
      <div class="kv"><span class="kv-k">Host</span><span class="kv-v mono">teslausb.local</span></div>
      <div class="kv"><span class="kv-k">Fallback</span><span class="kv-v mono">192.168.50.1</span></div>
      <div class="kv"><span class="kv-k">Pi serial</span><span class="kv-v mono">${esc(hotspot.serial || "unknown")}</span></div>
      <div class="kv"><span class="kv-k">Hotspot</span><span class="kv-v mono">${esc(hotspot.ssid || "unknown")}</span></div>
      <div class="kv"><span class="kv-k">USB gadget</span><span class="kv-v mono">${esc(status.usb || "unknown")}</span></div>
      <div class="kv"><span class="kv-k">Transfer session</span><span class="kv-v mono">${status.session_active ? "active" : "inactive"}</span></div>
      <div class="kv"><span class="kv-k">Drives mounted</span><span class="kv-v mono">${mounted} of ${Object.keys(status.drives || {}).length}</span></div>
      <div class="kv"><span class="kv-k">Uploads</span><span class="kv-v mono">${status.uploads_enabled ? "enabled" : "disabled"}</span></div>
      <form class="settings-form" onsubmit="saveHotspotSettings(event)">
        <h2 class="set-section-title">Hotspot Wi-Fi</h2>
        <div class="field"><label>Network name</label><input name="ssid" value="${esc(hotspot.ssid || hotspot.default_ssid || "")}" maxlength="32" autocomplete="off"></div>
        <div class="field"><label>New password</label><input name="password" type="password" placeholder="Leave blank to keep current password" minlength="8" maxlength="63" autocomplete="new-password"></div>
        <div class="file-empty-s">Default name: <span class="mono">${esc(hotspot.default_ssid || "Glovebox")}</span>. WPA2 passwords must be at least 8 characters, so the serial fallback is <span class="mono">${esc(hotspot.default_password_hint || "")}</span>.</div>
        <div class="edit-actions">
          <button class="btn btn-solid" type="submit">Save hotspot</button>
        </div>
      </form>
    </div>`;
  } else if (settingsSection === "activity") {
    main.innerHTML = `<div class="card card-pad">
      <h2 class="set-section-title">Recent log</h2>
      <pre class="log-pre">${esc(status.logs || "No portal activity yet.")}</pre>
    </div>`;
  }
}

async function saveHotspotSettings(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const data = new FormData(form);
  const ssid = String(data.get("ssid") || "").trim();
  const password = String(data.get("password") || "");
  try {
    const hotspot = await api("/api/hotspot", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ssid, password })
    });
    status.hotspot = hotspot;
    toast("Hotspot updated");
    renderSettings();
  } catch (e) {
    toast(e.message, "err");
  }
}

/* ============== nav ============== */
function showPage(page) {
  if (page !== currentPage) {
    pauseAllMedia();
    closeVideo();
  }
  currentPage = page;
  document.querySelectorAll("main > section").forEach(el => el.classList.add("hidden"));
  const target = document.getElementById(`page-${page}`);
  if (target) target.classList.remove("hidden");
  document.documentElement.dataset.page = page;
  renderBrand();
  refresh();
}

/* boot */
refresh();
setInterval(timerTick, 1000);
setInterval(() => {
  const mediaPages = ["dashcam", "photobooth", "music", "lightshow", "chime"];
  if (mediaPages.includes(currentPage)) refreshStatusOnly().catch(() => {});
  else refresh().catch(() => {});
}, 15000);
</script>
</body>
</html>"""


def main():
    load_session_deadline()
    threading.Thread(target=session_watchdog, daemon=True).start()
    server = ThreadingHTTPServer((HOST, PORT), PortalHandler)
    print(f"TeslaUSB portal listening on {HOST}:{PORT}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
