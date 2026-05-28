#!/usr/bin/env python3
import cgi
import html
import json
import mimetypes
import os
import posixpath
import re
import shutil
import subprocess
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse


SESSION_HELPER = os.environ.get("PORTAL_SESSION_HELPER", "/usr/local/bin/teslausb-portal-session")
HOST = os.environ.get("PORTAL_BIND_HOST", "0.0.0.0")
PORT = int(os.environ.get("PORTAL_PORT", "80"))
UPLOADS_ENABLED = os.environ.get("PORTAL_UPLOADS_ENABLED", "true").lower() == "true"
LOG_FILES = ["/mutable/portal.log"]

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
        raise ValueError("Filename uses unsupported characters.")
    suffix = Path(base).suffix.lower()
    if suffix not in allowed_extensions:
        raise ValueError(f"Unsupported file type: {suffix or '(none)'}")
    return base


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


def get_status():
    try:
        raw = run_helper("status")
        status = json.loads(raw)
    except Exception as exc:
        status = {"session_active": False, "usb": "unknown", "mounts": {}, "error": str(exc)}

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


def list_directory(drive_key, rel):
    if drive_key not in DRIVES:
        raise ValueError("Unknown drive.")
    root = DRIVES[drive_key]["root"]
    target, rel = safe_join(root, rel)
    if not target.exists():
        target = root
        rel = ""
    if not target.is_dir():
        raise ValueError("Browse path is not a directory.")

    items = []
    for child in sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
        try:
            stat = child.stat()
        except OSError:
            continue
        child_rel = posixpath.join(rel, child.name) if rel else child.name
        items.append({
            "name": child.name,
            "path": child_rel,
            "is_dir": child.is_dir(),
            "size": 0 if child.is_dir() else stat.st_size,
            "size_label": "" if child.is_dir() else format_bytes(stat.st_size),
            "modified": int(stat.st_mtime),
            "download": "" if child.is_dir() else f"/download?drive={quote(drive_key)}&path={quote(child_rel)}",
        })
    parent = posixpath.dirname(rel) if rel else ""
    return {"drive": drive_key, "path": rel, "parent": parent, "items": items}


class PortalHandler(BaseHTTPRequestHandler):
    server_version = "TeslaUSBPortal/2.0"

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
                self.send_json(list_directory(query.get("drive", ["cam"])[0], query.get("path", [""])[0]))
            elif parsed.path == "/download":
                self.download(parsed)
            else:
                self.send_error(HTTPStatus.NOT_FOUND)
        except Exception as exc:
            self.send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)

    def do_POST(self):
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/session/start":
                run_helper("start")
                self.send_json(get_status())
            elif parsed.path == "/session/stop":
                run_helper("stop")
                self.send_json(get_status())
            elif parsed.path == "/upload":
                self.handle_upload()
            else:
                self.send_error(HTTPStatus.NOT_FOUND)
        except Exception as exc:
            self.send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)

    def download(self, parsed):
        query = parse_qs(parsed.query)
        drive_key = query.get("drive", [""])[0]
        rel = query.get("path", [""])[0]
        if drive_key not in DRIVES:
            raise ValueError("Unknown drive.")
        target, _ = safe_join(DRIVES[drive_key]["root"], rel)
        if not target.is_file():
            raise ValueError("Download path is not a file.")
        content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(target.stat().st_size))
        self.send_header("Content-Disposition", f'attachment; filename="{target.name}"')
        self.end_headers()
        with target.open("rb") as file:
            shutil.copyfileobj(file, self.wfile)

    def handle_upload(self):
        if not UPLOADS_ENABLED:
            raise ValueError("Uploads are disabled.")
        status = get_status()
        if not status["session_active"]:
            raise ValueError("Start a transfer session before uploading.")
        form = cgi.FieldStorage(fp=self.rfile, headers=self.headers, environ={
            "REQUEST_METHOD": "POST",
            "CONTENT_TYPE": self.headers.get("Content-Type"),
        })
        target_key = form.getfirst("target", "")
        if target_key not in UPLOAD_TARGETS:
            raise ValueError("Unknown upload destination.")
        file_item = form["file"] if "file" in form else None
        if file_item is None or not getattr(file_item, "filename", ""):
            raise ValueError("No file uploaded.")

        target_info = UPLOAD_TARGETS[target_key]
        drive = DRIVES[target_info["drive"]]
        if not status["drives"][target_info["drive"]]["mounted"]:
            raise ValueError(f"{drive['label']} is not mounted.")
        filename = validate_filename(file_item.filename, target_info["extensions"], target_info.get("fixed_name"))
        destination_dir, _ = safe_join(drive["root"], target_info["dir"])
        destination_dir.mkdir(parents=True, exist_ok=True)
        destination = destination_dir / filename
        with destination.open("wb") as output:
            shutil.copyfileobj(file_item.file, output)
        os.sync()
        self.send_json({
            "ok": True,
            "drive": target_info["drive"],
            "path": target_info["dir"],
            "filename": filename,
        })


APP_HTML = r"""<!doctype html>
<html lang="en" data-page="home">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>TeslaDrive</title>
  <style>
    :root {
      --bg: #25231f;
      --surface: #302e2a;
      --surface-2: #3a3732;
      --text: #f2f0ea;
      --muted: #aaa49a;
      --faint: #777168;
      --hairline: #46423c;
      --hairline-2: #5b554d;
      --ok: #74d59a;
      --warn: #e4b65e;
      --error: #f07562;
      --accent: #f2f0ea;
      --on-accent: #25231f;
      --radius: 8px;
      --radius-sm: 6px;
      --pad: 22px;
      --gap: 16px;
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }
    * { box-sizing: border-box; }
    html, body { min-height: 100%; }
    body { margin: 0; background: var(--bg); color: var(--text); font-size: 14px; line-height: 1.45; -webkit-font-smoothing: antialiased; }
    button, input, select { font: inherit; }
    button { color: inherit; cursor: pointer; }
    a { color: inherit; }
    .mono { font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace; font-feature-settings: "tnum", "zero"; }
    .app { min-height: 100vh; display: grid; grid-template-rows: auto 1fr; }
    .topbar { display: flex; justify-content: space-between; align-items: center; gap: 16px; padding: 16px 40px; border-bottom: 1px solid var(--hairline); background: var(--bg); position: sticky; top: 0; z-index: 5; }
    .brand { display: inline-flex; align-items: baseline; gap: 10px; }
    .brand-name { font-size: 20px; font-weight: 650; letter-spacing: -0.01em; }
    .brand-sub { color: var(--faint); font-size: 11px; letter-spacing: 0.12em; text-transform: uppercase; }
    .topbar-r { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; justify-content: flex-end; }
    .status-chip { display: inline-flex; align-items: center; gap: 8px; border: 1px solid var(--hairline); border-radius: 999px; padding: 7px 11px; color: var(--muted); background: rgba(255,255,255,0.02); font-size: 12px; }
    .dot { width: 7px; height: 7px; border-radius: 50%; background: var(--faint); display: inline-block; }
    .dot.ok { background: var(--ok); box-shadow: 0 0 0 3px color-mix(in srgb, var(--ok) 18%, transparent); }
    .dot.warn { background: var(--warn); box-shadow: 0 0 0 3px color-mix(in srgb, var(--warn) 18%, transparent); }
    .mn { width: 100%; max-width: 1280px; margin: 0 auto; padding: 34px 40px 72px; }
    .page-head { display: flex; justify-content: space-between; align-items: flex-end; gap: 18px; margin-bottom: 24px; flex-wrap: wrap; }
    .page-title { margin: 0; font-size: clamp(36px, 6vw, 62px); line-height: 0.95; font-weight: 520; letter-spacing: 0; }
    .page-note { color: var(--muted); margin: 10px 0 0; max-width: 620px; }
    .btn { display: inline-flex; align-items: center; justify-content: center; gap: 8px; border-radius: var(--radius-sm); border: 1px solid var(--hairline-2); background: transparent; color: var(--text); min-height: 38px; padding: 8px 14px; font-weight: 620; white-space: nowrap; }
    .btn:hover { border-color: var(--text); background: var(--surface-2); }
    .btn-solid { background: var(--text); color: var(--bg); border-color: var(--text); }
    .btn-danger { color: var(--error); border-color: color-mix(in srgb, var(--error) 45%, transparent); }
    .btn-sm { min-height: 30px; padding: 5px 10px; font-size: 12px; }
    .icon-btn { width: 34px; height: 34px; display: inline-grid; place-items: center; border: 1px solid var(--hairline); background: transparent; border-radius: 999px; color: var(--muted); }
    .icon-btn:hover { color: var(--text); border-color: var(--text); background: var(--surface); }
    .card { background: var(--surface); border: 1px solid var(--hairline); border-radius: var(--radius); }
    .card-pad { padding: var(--pad); }
    .home-grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: var(--gap); }
    .home-tile { text-align: left; min-height: 260px; border-radius: var(--radius); border: 1px solid var(--hairline); background: var(--surface); color: var(--text); padding: 22px; display: flex; flex-direction: column; justify-content: space-between; transition: transform 120ms, border-color 120ms, background 120ms; }
    .home-tile:hover { transform: translateY(-2px); border-color: var(--text); background: var(--surface-2); }
    .home-icon { width: 44px; height: 44px; display: grid; place-items: center; border: 1px solid var(--hairline-2); border-radius: 8px; color: var(--muted); }
    .home-num { font-size: 58px; line-height: 0.95; font-weight: 540; margin-top: auto; }
    .home-num-label { color: var(--faint); font-size: 11px; letter-spacing: 0.12em; text-transform: uppercase; }
    .home-label { font-size: 25px; font-weight: 560; margin-top: 16px; }
    .meter { margin-top: 18px; }
    .meter-track { height: 3px; border-radius: 999px; overflow: hidden; background: var(--surface-2); }
    .meter-fill { height: 100%; background: var(--text); }
    .meter-row { margin-top: 8px; display: flex; justify-content: space-between; gap: 10px; color: var(--muted); font-size: 11px; }
    .panel-grid { display: grid; grid-template-columns: 1fr 320px; gap: var(--gap); align-items: start; }
    .folder-tabs { display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 16px; }
    .tab { border: 1px solid var(--hairline); background: transparent; color: var(--muted); border-radius: 999px; padding: 8px 12px; }
    .tab.on, .tab:hover { color: var(--text); border-color: var(--text); background: var(--surface); }
    .toolbar { display: flex; justify-content: space-between; align-items: center; gap: 14px; flex-wrap: wrap; margin-bottom: 14px; color: var(--muted); }
    .files { display: grid; grid-template-columns: repeat(auto-fill, minmax(210px, 1fr)); gap: 12px; }
    .file-card { text-align: left; border: 1px solid var(--hairline); background: var(--surface); color: var(--text); border-radius: var(--radius); padding: 14px; min-height: 136px; display: flex; flex-direction: column; gap: 10px; }
    .file-card:hover { border-color: var(--text); background: var(--surface-2); }
    .file-thumb { height: 64px; border-radius: 6px; border: 1px solid var(--hairline); display: grid; place-items: center; color: var(--faint); background: repeating-linear-gradient(45deg, rgba(255,255,255,0.03), rgba(255,255,255,0.03) 4px, transparent 4px, transparent 8px); }
    .file-name { overflow-wrap: anywhere; font-weight: 620; }
    .file-meta { margin-top: auto; color: var(--muted); font-size: 12px; display: flex; justify-content: space-between; gap: 10px; }
    .file-list { width: 100%; border-collapse: collapse; }
    .file-list th, .file-list td { border-bottom: 1px solid var(--hairline); padding: 11px 12px; text-align: left; }
    .file-list th { color: var(--muted); font-size: 11px; letter-spacing: 0.10em; text-transform: uppercase; font-weight: 500; }
    .file-list tr:hover td { background: rgba(255,255,255,0.025); }
    .upload-card { padding: 16px; }
    .upload-form { display: grid; gap: 12px; }
    .upload-zone { border: 1px dashed var(--hairline-2); border-radius: var(--radius); padding: 18px; background: rgba(255,255,255,0.025); }
    .upload-zone input, .upload-zone select { width: 100%; color: var(--text); background: var(--surface-2); border: 1px solid var(--hairline); border-radius: 6px; padding: 9px; }
    .upload-zone label { display: grid; gap: 6px; color: var(--muted); font-size: 12px; }
    .section-title { color: var(--muted); font-size: 11px; letter-spacing: 0.14em; text-transform: uppercase; margin: 0 0 12px; }
    .kv { display: flex; justify-content: space-between; gap: 16px; padding: 10px 0; border-bottom: 1px solid var(--hairline); }
    .kv:last-child { border-bottom: 0; }
    .kv span:first-child { color: var(--muted); }
    .log { white-space: pre-wrap; background: #171613; color: var(--muted); border: 1px solid var(--hairline); border-radius: 6px; padding: 12px; max-height: 260px; overflow: auto; }
    .empty { border: 1px dashed var(--hairline); border-radius: var(--radius); padding: 34px; color: var(--muted); text-align: center; grid-column: 1 / -1; }
    .hidden { display: none !important; }
    .toast { position: fixed; left: 50%; bottom: 22px; transform: translateX(-50%); background: var(--text); color: var(--bg); border-radius: 999px; padding: 10px 16px; font-weight: 650; z-index: 20; box-shadow: 0 12px 40px rgba(0,0,0,.28); }
    svg { display: block; }
    @media (max-width: 900px) {
      .topbar { padding: 14px 18px; align-items: flex-start; }
      .brand-sub { display: none; }
      .mn { padding: 24px 18px 56px; }
      .home-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
      .panel-grid { grid-template-columns: 1fr; }
    }
    @media (max-width: 560px) {
      .topbar { display: grid; }
      .topbar-r { justify-content: flex-start; }
      .home-grid { grid-template-columns: 1fr; }
      .home-tile { min-height: 190px; }
      .files { grid-template-columns: 1fr; }
      .file-list-wrap { overflow-x: auto; }
      .page-head { align-items: flex-start; }
    }
  </style>
</head>
<body>
  <div class="app">
    <header class="topbar">
      <div class="brand">
        <div class="brand-name">TeslaDrive</div>
        <div class="brand-sub mono">local portal</div>
      </div>
      <div class="topbar-r">
        <span class="status-chip mono"><span id="usbDot" class="dot"></span><span id="usbText">USB unknown</span></span>
        <span class="status-chip mono"><span id="sessionDot" class="dot"></span><span id="sessionText">session unknown</span></span>
        <button id="sessionButton" class="btn btn-solid" type="button">Start transfer session</button>
        <button class="icon-btn" type="button" title="Settings" onclick="showPage('settings')">⚙</button>
      </div>
    </header>
    <main class="mn">
      <section id="page-home">
        <div class="page-head">
          <div>
            <h1 class="page-title">What would you like to manage?</h1>
            <p class="page-note">Join the Pi hotspot, start a transfer session, then browse, download, or upload files directly on the Tesla-visible drives.</p>
          </div>
        </div>
        <div id="homeGrid" class="home-grid"></div>
      </section>

      <section id="page-browser" class="hidden">
        <div class="page-head">
          <div>
            <button class="btn btn-sm" type="button" onclick="showPage('home')">Home</button>
            <h1 id="browserTitle" class="page-title">Files</h1>
            <p id="browserNote" class="page-note"></p>
          </div>
        </div>
        <div class="panel-grid">
          <div>
            <div id="folderTabs" class="folder-tabs"></div>
            <div class="toolbar">
              <span id="pathLabel" class="mono"></span>
              <button id="parentButton" class="btn btn-sm" type="button">Up one folder</button>
            </div>
            <div id="fileGrid" class="files"></div>
            <div class="file-list-wrap card" style="margin-top: 16px;">
              <table class="file-list">
                <thead><tr><th>Name</th><th>Type</th><th>Size</th><th></th></tr></thead>
                <tbody id="fileTable"></tbody>
              </table>
            </div>
          </div>
          <aside>
            <div class="card card-pad" id="uploadPanel"></div>
            <div class="card card-pad" style="margin-top: 16px;">
              <h2 class="section-title">Transfer session</h2>
              <div class="kv"><span>USB gadget</span><strong id="sideUsb" class="mono">unknown</strong></div>
              <div class="kv"><span>Drives mounted</span><strong id="sideMounts" class="mono">0</strong></div>
            </div>
          </aside>
        </div>
      </section>

      <section id="page-settings" class="hidden">
        <div class="page-head">
          <div>
            <button class="btn btn-sm" type="button" onclick="showPage('home')">Home</button>
            <h1 class="page-title">Settings</h1>
            <p class="page-note">Local portal status and diagnostics. The old network-share workflow has been removed.</p>
          </div>
        </div>
        <div class="panel-grid">
          <div class="card card-pad">
            <h2 class="section-title">Connection</h2>
            <div class="kv"><span>Portal host</span><strong class="mono">teslausb.local</strong></div>
            <div class="kv"><span>Fallback address</span><strong class="mono">192.168.50.1</strong></div>
            <div class="kv"><span>Uploads</span><strong id="uploadsStatus" class="mono">unknown</strong></div>
            <div class="kv"><span>Transfer session</span><strong id="settingsSession" class="mono">unknown</strong></div>
            <div class="kv"><span>USB gadget</span><strong id="settingsUsb" class="mono">unknown</strong></div>
          </div>
          <div class="card card-pad">
            <h2 class="section-title">Recent activity</h2>
            <pre id="logs" class="log"></pre>
          </div>
        </div>
      </section>
    </main>
  </div>
  <div id="toast" class="toast hidden"></div>
  <script>
    const icons = {
      cam: "▦",
      sounds: "✦",
      music: "♪",
      folder: "▣",
      file: "□"
    };
    const homeOrder = [
      { id: "cam", title: "Dash cam", countLabel: "files", path: "TeslaCam", note: "Sentry, Saved, Recent, Photobooth" },
      { id: "music", title: "Music", countLabel: "tracks", path: "Music", note: "FLAC, MP3, WAV, M4A, AAC" },
      { id: "sounds", title: "Light shows", countLabel: "extras", path: "LightShow", note: ".fseq with matching audio" },
      { id: "lockchime", title: "Lock chime", countLabel: "active", special: "lockchime", note: "Replace LockChime.wav" }
    ];
    const tabs = {
      cam: ["TeslaCam", "TeslaCam/RecentClips", "TeslaCam/SavedClips", "TeslaCam/SentryClips", "TeslaCam/Photobooth", "TeslaCam/EncryptedClips"],
      sounds: ["", "LightShow", "Wraps", "LicensePlate", "Boombox"],
      music: ["Music"]
    };
    let status = null;
    let currentDrive = "cam";
    let currentPath = "";

    function esc(value) {
      return String(value ?? "").replace(/[&<>"']/g, ch => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch]));
    }
    function pct(usage) {
      if (!usage || !usage.total) return 0;
      return Math.max(0, Math.min(100, (usage.used / usage.total) * 100));
    }
    function fmtUsage(usage) {
      if (!usage) return "not mounted";
      return `${bytes(usage.used)} / ${bytes(usage.total)}`;
    }
    function bytes(value) {
      const units = ["B", "KB", "MB", "GB", "TB"];
      let n = Number(value || 0);
      for (const unit of units) {
        if (n < 1024 || unit === units[units.length - 1]) return unit === "B" ? `${n} B` : `${n.toFixed(1)} ${unit}`;
        n /= 1024;
      }
    }
    function toast(message) {
      const el = document.getElementById("toast");
      el.textContent = message;
      el.classList.remove("hidden");
      setTimeout(() => el.classList.add("hidden"), 2800);
    }
    async function api(path, options) {
      const res = await fetch(path, options);
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || "Request failed");
      return data;
    }
    async function refresh() {
      status = await api("/api/status");
      renderTopbar();
      renderHome();
      renderSettings();
      renderUploadPanel();
      if (!document.getElementById("page-browser").classList.contains("hidden")) {
        await loadList(currentDrive, currentPath);
      }
    }
    function renderTopbar() {
      const usbOk = status.usb === "connected";
      document.getElementById("usbDot").className = `dot ${usbOk ? "ok" : "warn"}`;
      document.getElementById("usbText").textContent = `USB ${status.usb}`;
      document.getElementById("sessionDot").className = `dot ${status.session_active ? "warn" : "ok"}`;
      document.getElementById("sessionText").textContent = status.session_active ? "transfer active" : "car mode";
      const button = document.getElementById("sessionButton");
      button.textContent = status.session_active ? "End transfer session" : "Start transfer session";
      button.className = status.session_active ? "btn btn-danger" : "btn btn-solid";
      button.onclick = toggleSession;
    }
    function renderHome() {
      const grid = document.getElementById("homeGrid");
      grid.innerHTML = homeOrder.map(tile => {
        const driveId = tile.special === "lockchime" ? "sounds" : tile.id;
        const drive = status.drives[driveId];
        const count = tile.special === "lockchime" ? (drive?.mounted ? "1" : "0") : (drive?.files || 0);
        const mounted = drive?.mounted;
        const usage = drive?.usage;
        return `<button class="home-tile" type="button" onclick="${tile.special ? "openLockChime()" : `openDrive('${tile.id}','${tile.path}')`}">
          <div class="home-icon">${icons[tile.id] || icons.file}</div>
          <div class="home-num">${mounted ? count : "—"}</div>
          <div class="home-num-label mono">${esc(mounted ? tile.countLabel : "not mounted")}</div>
          <div>
            <div class="home-label">${esc(tile.title)}</div>
            <div class="page-note">${esc(tile.note)}</div>
          </div>
          <div class="meter">
            <div class="meter-track"><div class="meter-fill" style="width:${pct(usage)}%"></div></div>
            <div class="meter-row mono"><span>${esc(drive?.label || "")}</span><span>${esc(fmtUsage(usage))}</span></div>
          </div>
        </button>`;
      }).join("");
    }
    function renderSettings() {
      document.getElementById("uploadsStatus").textContent = status.uploads_enabled ? "enabled" : "disabled";
      document.getElementById("settingsSession").textContent = status.session_active ? "active" : "inactive";
      document.getElementById("settingsUsb").textContent = status.usb;
      document.getElementById("logs").textContent = status.logs || "No portal activity yet.";
      document.getElementById("sideUsb").textContent = status.usb;
      document.getElementById("sideMounts").textContent = Object.values(status.drives || {}).filter(d => d.mounted).length;
    }
    function renderUploadPanel() {
      const panel = document.getElementById("uploadPanel");
      if (!panel) return;
      const targets = Object.entries(status.upload_targets || {}).map(([key, target]) => `<option value="${esc(key)}">${esc(target.label)}</option>`).join("");
      panel.innerHTML = `<h2 class="section-title">Upload</h2>
        ${status.session_active && status.uploads_enabled ? `<form id="uploadForm" class="upload-form">
          <div class="upload-zone">
            <label>Destination<select name="target">${targets}</select></label>
          </div>
          <div class="upload-zone">
            <label>File<input name="file" type="file" required></label>
          </div>
          <button class="btn btn-solid" type="submit">Upload file</button>
        </form>` : `<p class="page-note">Start a transfer session to upload files.</p>`}`;
      const form = document.getElementById("uploadForm");
      if (form) form.onsubmit = uploadFile;
    }
    async function uploadFile(event) {
      event.preventDefault();
      const form = event.currentTarget;
      const body = new FormData(form);
      const data = await api("/upload", { method: "POST", body });
      toast(`Uploaded ${data.filename}`);
      form.reset();
      await refresh();
      await loadList(data.drive, data.path || "");
    }
    async function toggleSession() {
      const action = status.session_active ? "/session/stop" : "/session/start";
      await api(action, { method: "POST" });
      toast(status.session_active ? "Transfer session ended" : "Transfer session started");
      await refresh();
    }
    function showPage(page) {
      document.querySelectorAll("main > section").forEach(el => el.classList.add("hidden"));
      document.getElementById(`page-${page}`).classList.remove("hidden");
      document.documentElement.dataset.page = page;
      if (page === "settings") renderSettings();
    }
    function openLockChime() {
      openDrive("sounds", "");
      setTimeout(() => {
        const select = document.querySelector("#uploadForm select[name='target']");
        if (select) select.value = "lockchime";
      }, 0);
    }
    async function openDrive(drive, path) {
      showPage("browser");
      await loadList(drive, path || "");
    }
    async function loadList(drive, path) {
      currentDrive = drive;
      currentPath = path || "";
      const info = status.drives[drive];
      document.getElementById("browserTitle").textContent = info?.title || "Files";
      document.getElementById("browserNote").textContent = `${info?.label || ""} ${info?.mounted ? "is mounted for this transfer session." : "is not mounted. Start a transfer session first."}`;
      renderTabs(drive);
      const pathLabel = document.getElementById("pathLabel");
      pathLabel.textContent = currentPath || "/";
      const parentButton = document.getElementById("parentButton");
      parentButton.disabled = !currentPath;
      parentButton.onclick = () => loadList(currentDrive, currentPath.split("/").slice(0, -1).join("/"));
      if (!info?.mounted) {
        renderItems([]);
        return;
      }
      const list = await api(`/api/list?drive=${encodeURIComponent(drive)}&path=${encodeURIComponent(currentPath)}`);
      renderItems(list.items);
    }
    function renderTabs(drive) {
      document.getElementById("folderTabs").innerHTML = (tabs[drive] || [""]).map(path => `<button type="button" class="tab ${path === currentPath ? "on" : ""}" onclick="loadList('${drive}', '${path.replace(/'/g, "\\'")}')">${esc(path || "Root")}</button>`).join("");
    }
    function renderItems(items) {
      const grid = document.getElementById("fileGrid");
      const table = document.getElementById("fileTable");
      if (!items.length) {
        grid.innerHTML = `<div class="empty">No files here yet.</div>`;
        table.innerHTML = `<tr><td colspan="4" class="mono">No files here yet.</td></tr>`;
        return;
      }
      grid.innerHTML = items.map(item => {
        const action = item.is_dir ? `loadList('${currentDrive}', '${item.path.replace(/'/g, "\\'")}')` : `window.location.href='${item.download}'`;
        return `<button class="file-card" type="button" onclick="${action}">
          <div class="file-thumb mono">${item.is_dir ? icons.folder : icons.file}</div>
          <div class="file-name">${esc(item.name)}</div>
          <div class="file-meta mono"><span>${item.is_dir ? "folder" : "file"}</span><span>${esc(item.size_label)}</span></div>
        </button>`;
      }).join("");
      table.innerHTML = items.map(item => {
        const open = item.is_dir ? `loadList('${currentDrive}', '${item.path.replace(/'/g, "\\'")}')` : `window.location.href='${item.download}'`;
        return `<tr>
          <td><button class="link" type="button" onclick="${open}">${esc(item.name)}</button></td>
          <td class="mono">${item.is_dir ? "folder" : "file"}</td>
          <td class="mono">${esc(item.size_label)}</td>
          <td>${item.is_dir ? "" : `<a class="btn btn-sm" href="${item.download}">Download</a>`}</td>
        </tr>`;
      }).join("");
    }
    refresh().catch(err => toast(err.message));
    setInterval(() => refresh().catch(() => {}), 15000);
  </script>
</body>
</html>"""


def main():
    server = ThreadingHTTPServer((HOST, PORT), PortalHandler)
    print(f"TeslaUSB portal listening on {HOST}:{PORT}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
