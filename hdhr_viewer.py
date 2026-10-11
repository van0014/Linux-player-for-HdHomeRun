#!/usr/bin/env python3
"""HDHomeRun Viewer: a small Linux channel browser and player for HDHomeRun tuners.

Lists your channels with what's on now and next, plays them in mpv, and lets you
change channel without restarting the player (click/Enter in the list, or
Up/Down inside the mpv window).
"""
import gzip
import json
import math
import os
import queue
import re
import socket
import subprocess
import threading
import time
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
import tkinter as tk
from tkinter import messagebox, ttk

APP = "hdhr-viewer"
CONFIG_DIR = os.path.expanduser(f"~/.config/{APP}")
CACHE_DIR = os.path.expanduser(f"~/.cache/{APP}")
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")
GUIDE_CACHE = os.path.join(CACHE_DIR, "guide.xml")
GUIDE_MAX_AGE = 24 * 3600  # SiliconDust asks for roughly one guide download a day
SOCK = os.path.join(os.environ.get("XDG_RUNTIME_DIR", "/tmp"), f"{APP}-mpv.sock")

DEFAULT_CONFIG = {
    "host": "hdhomerun.local",  # tuner hostname or IP
    "xmltv_source": "",         # URL or file path; blank = HDHomeRun's own guide
    "mpv_args": [],             # extra mpv flags, appended last so they win
}

BASE_MPV_ARGS = [
    "--ao=pipewire,",  # trailing comma = fall back to other audio drivers
    "--audio-buffer=1",
    "--vo=gpu",
    "--hwdec=auto-safe",  # on AMD/Intel, "--hwdec=vaapi" in mpv_args may be better
    "--cache=yes",
    "--cache-secs=10",
    "--demuxer-max-bytes=50M",
    "--demuxer-readahead-secs=10",
    "--cache-pause=no",
    "--cache-pause-initial=yes",
    "--cache-pause-wait=2",
    "--video-sync=audio",
    "--force-window=immediate",
]

INPUT_CONF = "UP playlist-prev\nDOWN playlist-next\nPGUP playlist-prev\nPGDWN playlist-next\n"


# ---------------------------------------------------------------- config / http
def load_config():
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_FILE) as f:
            cfg.update(json.load(f))
    except FileNotFoundError:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(CONFIG_FILE, "w") as f:
            json.dump(DEFAULT_CONFIG, f, indent=2)
    except (OSError, ValueError) as e:
        messagebox.showwarning("Config", f"Ignoring unreadable {CONFIG_FILE}: {e}")
    return cfg


def http_get(url, timeout=10):
    req = urllib.request.Request(url, headers={"Accept-Encoding": "gzip"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read()
    if data[:2] == b"\x1f\x8b":
        data = gzip.decompress(data)
    return data


def get_channels(host):
    try:
        return json.loads(http_get(f"http://{host}/lineup.json", 5))
    except Exception as e:
        messagebox.showerror(
            "Connection error",
            f"Could not reach the HDHomeRun at '{host}':\n{e}\n\n"
            f"Set \"host\" in {CONFIG_FILE} (an IP address works too).")
        return []


# ---------------------------------------------------------------- guide data
def fetch_guide(cfg):
    """Return a path to an XMLTV file, downloading only if the cache is stale."""
    src = cfg["xmltv_source"]
    if src and not src.startswith("http"):
        return os.path.expanduser(src)
    if os.path.exists(GUIDE_CACHE) and time.time() - os.path.getmtime(GUIDE_CACHE) < GUIDE_MAX_AGE:
        return GUIDE_CACHE
    if not src:
        auth = json.loads(http_get(f"http://{cfg['host']}/discover.json", 5))["DeviceAuth"]
        src = f"https://api.hdhomerun.com/api/xmltv?DeviceAuth={auth}"
    data = http_get(src, 90)
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(GUIDE_CACHE, "wb") as f:
        f.write(data)
    return GUIDE_CACHE


def parse_ts(s):
    s = s.strip()
    d = datetime.strptime(s, "%Y%m%d%H%M%S %z" if " " in s else "%Y%m%d%H%M%S")
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def norm(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def load_guide(path):
    """Return (name_index, programmes); programmes already finished are dropped."""
    now = datetime.now(timezone.utc)
    names, progs = {}, {}
    for _, el in ET.iterparse(path, events=("end",)):
        if el.tag == "channel":
            for dn in el.findall("display-name"):
                if dn.text:
                    names.setdefault(norm(dn.text), el.get("id"))
            el.clear()
        elif el.tag == "programme":
            try:
                stop = parse_ts(el.get("stop"))
                if stop > now:
                    progs.setdefault(el.get("channel"), []).append(
                        (parse_ts(el.get("start")), stop, (el.findtext("title") or "").strip()))
            except (TypeError, ValueError):
                pass
            el.clear()
    for lst in progs.values():
        lst.sort()
    return names, progs


def now_next(progs, cid, now=None):
    now = now or datetime.now(timezone.utc)
    cur = nxt = None
    for p in progs.get(cid, []):
        if p[0] <= now < p[1]:
            cur = p
        elif p[0] >= now and nxt is None:
            nxt = p
    return cur, nxt


# ---------------------------------------------------------------- time wording
def _mins(td):
    return max(1, math.ceil(td.total_seconds() / 60))


def _dur(m):
    if m < 60:
        return f"{m} min" + ("" if m == 1 else "s")
    h, r = divmod(m, 60)
    return f"{h}h {r}m" if r else f"{h}h"


def time_left(stop, now):
    return f"{_dur(_mins(stop - now))} left"


def starts_in(start, now):
    """Relative for the next few hours; a clock time (with weekday) only beyond that."""
    d = start - now
    if d.total_seconds() <= 0:
        return "starting"
    if d.total_seconds() <= 3 * 3600:
        return f"in {_dur(_mins(d))}"
    local = start.astimezone()
    t = local.strftime("%I:%M%p").lstrip("0").lower()
    return t if local.date() == datetime.now().date() else f"{local.strftime('%a')} {t}"


# ---------------------------------------------------------------- mpv control
class MpvIPC:
    """Minimal client for mpv's JSON IPC socket."""

    def __init__(self, path, events):
        self.path, self.events = path, events
        self.sock = None
        self.connected = False

    def connect(self, timeout=8):
        deadline = time.time() + timeout
        while time.time() < deadline:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                s.connect(self.path)
            except OSError:
                s.close()
                time.sleep(0.1)
                continue
            self.sock, self.connected = s, True
            threading.Thread(target=self._reader, args=(s,), daemon=True).start()
            self.command("observe_property", 1, "playlist-pos")
            return

    def command(self, *args):
        if not self.sock:
            return
        try:
            self.sock.sendall((json.dumps({"command": list(args)}) + "\n").encode())
        except OSError:
            self.connected = False

    def _reader(self, s):
        buf = b""
        while True:
            try:
                chunk = s.recv(4096)
            except OSError:
                break
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if msg.get("event") == "property-change" and msg.get("name") == "playlist-pos":
                    self.events.put(msg.get("data"))
        self.connected = False

    def close(self):
        self.connected = False
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass
        self.sock = None


# ---------------------------------------------------------------- UI
class Viewer(tk.Tk):
    def __init__(self, cfg, channels):
        super().__init__()
        self.cfg = cfg
        self.title("HDHomeRun Viewer")
        self.geometry("900x520")
        self.proc = None
        self.ipc = None
        self.events = queue.Queue()
        self.guide_names, self.guide_progs = {}, {}
        self.rows, self.urls, self.meta, self.cid = [], {}, {}, {}
        self.status = tk.StringVar(value="Guide: loading...")
        self.clock = tk.StringVar()

        cols = ("channel", "now", "next")
        self.tree = ttk.Treeview(self, columns=cols, show="headings", selectmode="browse")
        for col, text, width, stretch in (("channel", "Channel", 190, False),
                                          ("now", "Now", 380, True),
                                          ("next", "Next", 290, True)):
            self.tree.heading(col, text=text)
            self.tree.column(col, width=width, stretch=stretch)
        self.tree.pack(fill=tk.BOTH, expand=True, padx=10, pady=(10, 5))

        for c in channels:
            if not c.get("URL"):
                continue
            num, name = c.get("GuideNumber", ""), c.get("GuideName", "")
            iid = self.tree.insert("", tk.END, values=(f"{num} - {name}", "", ""))
            self.rows.append(iid)
            self.urls[iid] = c["URL"]
            self.meta[iid] = (num, name)
        if self.rows:
            self.tree.selection_set(self.rows[0])
            self.tree.focus(self.rows[0])
        self.tree.bind("<Double-Button-1>", self.play_channel)
        self.tree.bind("<Return>", self.play_channel)

        bar = ttk.Frame(self)
        bar.pack(fill=tk.X, padx=10, pady=(0, 10))
        ttk.Button(bar, text="Play", command=self.play_channel).pack(side=tk.LEFT)
        ttk.Button(bar, text="Stop", command=self.stop_stream).pack(side=tk.LEFT, padx=5)
        ttk.Label(bar, textvariable=self.status).pack(side=tk.LEFT, padx=15)
        ttk.Label(bar, textvariable=self.clock).pack(side=tk.RIGHT)

        self.protocol("WM_DELETE_WINDOW", self.on_close)
        threading.Thread(target=self.guide_worker, daemon=True).start()
        self.tick()
        self.poll_events()

    # ---- guide
    def guide_worker(self):
        try:
            names, progs = load_guide(fetch_guide(self.cfg))
            self.after(0, self.guide_ready, names, progs)
        except Exception as e:
            self.after(0, self.status.set, f"Guide unavailable: {e}")

    def guide_ready(self, names, progs):
        self.guide_names, self.guide_progs = names, progs
        for iid, (num, name) in self.meta.items():
            cid = names.get(norm(name)) or names.get(norm(num))
            if cid:
                self.cid[iid] = cid
        self.status.set(f"Guide: {len(self.cid)}/{len(self.rows)} channels matched")
        self.refresh_guide()

    def describe(self, iid, now):
        cur, nxt = now_next(self.guide_progs, self.cid.get(iid), now)
        now_txt = f"{cur[2]}  ({time_left(cur[1], now)})" if cur else ""
        next_txt = f"{nxt[2]}  ({starts_in(nxt[0], now)})" if nxt else ""
        return cur, nxt, now_txt, next_txt

    def refresh_guide(self):
        now = datetime.now(timezone.utc)
        for iid in self.cid:
            _, _, now_txt, next_txt = self.describe(iid, now)
            label = self.tree.item(iid, "values")[0]
            self.tree.item(iid, values=(label, now_txt, next_txt))

    def tick(self):
        self.clock.set(datetime.now().strftime("%I:%M%p").lstrip("0").lower())
        self.refresh_guide()
        self.after(15_000, self.tick)

    # ---- playback
    def running(self):
        return (self.proc is not None and self.proc.poll() is None
                and self.ipc is not None and self.ipc.connected)

    def play_channel(self, event=None):
        sel = self.tree.selection()
        if not sel:
            return
        idx = self.rows.index(sel[0])
        if self.running():
            self.ipc.command("playlist-play-index", idx)  # switch channel in place
            return
        self.stop_stream()
        self.launch(idx)

    def launch(self, idx):
        os.makedirs(CACHE_DIR, exist_ok=True)
        m3u = os.path.join(CACHE_DIR, "channels.m3u")
        with open(m3u, "w") as f:
            f.write("#EXTM3U\n")
            for iid in self.rows:
                num, name = self.meta[iid]
                f.write(f"#EXTINF:-1,{num} {name}\n{self.urls[iid]}\n")
        conf = os.path.join(CACHE_DIR, "input.conf")
        with open(conf, "w") as f:
            f.write(INPUT_CONF)
        try:
            os.unlink(SOCK)
        except FileNotFoundError:
            pass
        log = open(os.path.join(CACHE_DIR, "mpv.log"), "w")
        cmd = ["mpv", *BASE_MPV_ARGS, *self.cfg["mpv_args"],
               f"--input-ipc-server={SOCK}", f"--input-conf={conf}",
               f"--playlist-start={idx}", f"--playlist={m3u}"]
        try:
            self.proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT)
        except FileNotFoundError:
            messagebox.showerror("mpv not found", "Install mpv: sudo apt install mpv")
            return
        self.ipc = MpvIPC(SOCK, self.events)
        threading.Thread(target=self.ipc.connect, daemon=True).start()

    def poll_events(self):
        try:
            while True:
                pos = self.events.get_nowait()
                if isinstance(pos, int) and 0 <= pos < len(self.rows):
                    self.on_channel_changed(pos)
        except queue.Empty:
            pass
        if self.proc is not None and self.proc.poll() is not None:
            self.proc = None
            if self.ipc:
                self.ipc.close()
        self.after(150, self.poll_events)

    def on_channel_changed(self, pos):
        iid = self.rows[pos]
        self.tree.selection_set(iid)
        self.tree.see(iid)
        num, name = self.meta[iid]
        lines = [f"{num} {name}"]
        if iid in self.cid:
            now = datetime.now(timezone.utc)
            cur, nxt, _, _ = self.describe(iid, now)
            if cur:
                lines.append(f"Now: {cur[2]} ({time_left(cur[1], now)})")
            if nxt:
                lines.append(f"Next: {nxt[2]} ({starts_in(nxt[0], now)})")
        self.ipc.command("show-text", "\n".join(lines), 5000)

    def stop_stream(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc = None
        if self.ipc:
            self.ipc.close()

    def on_close(self):
        self.stop_stream()
        self.destroy()


def main():
    root = tk.Tk()
    root.withdraw()
    cfg = load_config()
    channels = get_channels(cfg["host"])
    root.destroy()
    if channels:
        Viewer(cfg, channels).mainloop()


if __name__ == "__main__":
    main()
