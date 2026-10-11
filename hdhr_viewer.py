#!/usr/bin/env python3
"""HDHomeRun Viewer: a small Linux channel browser and player for HDHomeRun tuners.

Channel list and TV guide grid with what's on now and next, a program info
panel, and mpv for playback. Channels change in place without restarting the
player (double-click or Enter in the list/guide, or Up/Down in the mpv window).
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
from collections import namedtuple
from datetime import datetime, timedelta, timezone
import tkinter as tk
from tkinter import font as tkfont
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
    "--cache-pause-initial=no",            # start playing immediately on a channel change
    "--demuxer-lavf-analyzeduration=1",    # don't over-analyse the stream before starting
    "--video-sync=audio",
    "--force-window=immediate",
]

INPUT_CONF = ("UP playlist-prev\nDOWN playlist-next\nPGUP playlist-prev\nPGDWN playlist-next\n"
              "TAB show-text ${playlist} 8000\n")

Prog = namedtuple("Prog", "start stop title sub desc cat")

# guide grid geometry
ROW_H, HEAD_H, LABEL_W, PX_PER_MIN = 36, 28, 170, 4


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
    """Return (name_index, programmes). Programmes that ended over an hour ago are dropped."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=1)
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
                if stop > cutoff:
                    progs.setdefault(el.get("channel"), []).append(Prog(
                        parse_ts(el.get("start")), stop,
                        (el.findtext("title") or "").strip(),
                        (el.findtext("sub-title") or "").strip(),
                        (el.findtext("desc") or "").strip(),
                        (el.findtext("category") or "").strip()))
            except (TypeError, ValueError):
                pass
            el.clear()
    for lst in progs.values():
        lst.sort(key=lambda p: p.start)
    return names, progs


def now_next(progs, cid, now=None):
    now = now or datetime.now(timezone.utc)
    cur = nxt = None
    for p in progs.get(cid, []):
        if p.start <= now < p.stop:
            cur = p
        elif p.start >= now and nxt is None:
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
    return f"at {t}" if local.date() == datetime.now().date() else f"{local.strftime('%a')} {t}"


def when(p, now):
    if p.start <= now < p.stop:
        return f"On now ({time_left(p.stop, now)})"
    if p.start > now:
        return f"Starts {starts_in(p.start, now)}"
    return f"Ended {_dur(_mins(now - p.stop))} ago"


# ---------------------------------------------------------------- mpv control
def mpv_can_edit_playlist():
    try:
        out = subprocess.run(["mpv", "--version"], capture_output=True, text=True, timeout=5).stdout
        m = re.search(r"mpv[ -]v?(\d+)\.(\d+)", out)
        return bool(m) and (int(m.group(1)), int(m.group(2))) >= (0, 38)
    except (OSError, subprocess.SubprocessError):
        return False


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
            self.command("observe_property", 1, "path")
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
                if msg.get("event") == "property-change" and msg.get("name") == "path":
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


# ---------------------------------------------------------------- guide grid
class GuideGrid(ttk.Frame):
    """Scrollable channel x time grid. Click a programme for details, double-click to play."""

    def __init__(self, parent, app):
        super().__init__(parent)
        self.app = app
        self.font = tkfont.nametofont("TkDefaultFont")
        self.t0 = None
        self.total_w = self.height = 0
        self.rect_prog = {}  # rectangle id -> (iid, Prog), recoloured as time passes
        self.hit = {}        # rectangle/text id -> (iid, Prog), for clicks

        bar = ttk.Frame(self)
        bar.grid(row=0, column=0, columnspan=3, sticky="ew", pady=(0, 4))
        ttk.Button(bar, text="Jump to now", command=self.build).pack(side=tk.LEFT)

        bg = "#f4f4f4"
        self.corner = tk.Canvas(self, width=LABEL_W, height=HEAD_H, highlightthickness=0, bg="#dcdcdc")
        self.head = tk.Canvas(self, height=HEAD_H, highlightthickness=0, bg="#dcdcdc")
        self.labels = tk.Canvas(self, width=LABEL_W, highlightthickness=0, bg=bg)
        self.canvas = tk.Canvas(self, highlightthickness=0, bg=bg)
        self.vbar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.hbar = ttk.Scrollbar(self, orient="horizontal", command=self.canvas.xview)
        self.canvas.configure(yscrollcommand=self._yscroll, xscrollcommand=self._xscroll)
        self.corner.grid(row=1, column=0)
        self.head.grid(row=1, column=1, sticky="ew")
        self.labels.grid(row=2, column=0, sticky="ns")
        self.canvas.grid(row=2, column=1, sticky="nsew")
        self.vbar.grid(row=2, column=2, sticky="ns")
        self.hbar.grid(row=3, column=1, sticky="ew")
        self.rowconfigure(2, weight=1)
        self.columnconfigure(1, weight=1)

        self.canvas.bind("<Button-1>", self._click)
        self.canvas.bind("<Double-Button-1>", self._double)
        self.labels.bind("<Button-1>", self._label_click)
        self.labels.bind("<Double-Button-1>", self._label_double)
        for w in (self.canvas, self.labels):
            w.bind("<Button-4>", lambda e: self.canvas.yview_scroll(-2, "units"))
            w.bind("<Button-5>", lambda e: self.canvas.yview_scroll(2, "units"))
            w.bind("<Shift-Button-4>", lambda e: self.canvas.xview_scroll(-3, "units"))
            w.bind("<Shift-Button-5>", lambda e: self.canvas.xview_scroll(3, "units"))

    # scrolling: the time ruler follows horizontally, channel names follow vertically
    def _xscroll(self, first, last):
        self.hbar.set(first, last)
        self.head.xview_moveto(first)

    def _yscroll(self, first, last):
        self.vbar.set(first, last)
        self.labels.yview_moveto(first)

    def _x(self, t):
        return (t - self.t0).total_seconds() / 60 * PX_PER_MIN

    def _fit(self, text, px):
        if px < 12:
            return ""
        if self.font.measure(text) <= px:
            return text
        while text and self.font.measure(text + "…") > px:
            text = text[:-1]
        return text + "…" if text else ""

    def build(self):
        app = self.app
        now = datetime.now(timezone.utc)
        local = now.astimezone()
        base = local.replace(minute=local.minute // 30 * 30, second=0, microsecond=0)
        self.t0 = (base - timedelta(minutes=30)).astimezone(timezone.utc)
        end = max((p.stop for lst in app.guide_progs.values() for p in lst),
                  default=self.t0 + timedelta(hours=4))
        end = min(end, self.t0 + timedelta(hours=48))
        self.total_w = int(self._x(end)) + 20
        self.height = ROW_H * len(app.rows)

        for c in (self.canvas, self.head, self.labels):
            c.delete("all")
        self.rect_prog.clear()
        self.hit.clear()
        self.canvas.configure(scrollregion=(0, 0, self.total_w, self.height))
        self.head.configure(scrollregion=(0, 0, self.total_w, HEAD_H))
        self.labels.configure(scrollregion=(0, 0, LABEL_W, self.height))

        t = self.t0
        while t < end:  # time ruler: the only place clock times are unavoidable
            x = self._x(t)
            lt = t.astimezone()
            text = lt.strftime("%I:%M%p").lstrip("0").lower()
            if lt.hour == 0 and lt.minute == 0:
                text = f"{lt.strftime('%a')} {text}"
            self.head.create_line(x, HEAD_H - 8, x, HEAD_H, fill="#888")
            self.head.create_text(x + 3, HEAD_H // 2, text=text, anchor="w", fill="#333")
            self.canvas.create_line(x, 0, x, self.height, fill="#d6d6d6")
            t += timedelta(minutes=30)

        for r, iid in enumerate(app.rows):
            y = r * ROW_H
            self.canvas.create_line(0, y + ROW_H, self.total_w, y + ROW_H, fill="#c8c8c8")
            for p in app.guide_progs.get(app.cid.get(iid), []):
                if p.stop <= self.t0:
                    continue
                x1, x2 = max(self._x(p.start), 0), self._x(p.stop)
                if x2 - x1 < 3:
                    continue
                rect = self.canvas.create_rectangle(x1 + 1, y + 2, x2 - 1, y + ROW_H - 2,
                                                    fill="#e6ebf0", outline="#8a96a3")
                self.rect_prog[rect] = (iid, p)
                self.hit[rect] = (iid, p)
                txt = self._fit(p.title, x2 - x1 - 10)
                if txt:
                    self.hit[self.canvas.create_text(x1 + 5, y + ROW_H // 2, text=txt,
                                                     anchor="w", fill="#111")] = (iid, p)

        self.canvas.create_line(0, 0, 0, self.height, fill="#d33", width=2, tags="nowline")
        self.head.create_line(0, 0, 0, HEAD_H, fill="#d33", width=2, tags="nowline")
        self.draw_labels()
        self.refresh(now)
        self.after(60, self.scroll_to_now)

    def scroll_to_now(self):
        if self.t0 and self.total_w:
            x = self._x(datetime.now(timezone.utc))
            self.canvas.xview_moveto(max(0.0, (x - 120) / self.total_w))

    def refresh(self, now):
        """Recolour what's on now and move the red now-line. Cheap enough to run every tick."""
        if not self.t0:
            return
        for rect, (_, p) in self.rect_prog.items():
            self.canvas.itemconfigure(rect, fill="#b9d7f5" if p.start <= now < p.stop else "#e6ebf0")
        x = self._x(now)
        self.canvas.coords("nowline", x, 0, x, self.height)
        self.head.coords("nowline", x, 0, x, HEAD_H)

    def draw_labels(self):
        c = self.labels
        c.delete("all")
        for r, iid in enumerate(self.app.rows):
            y = r * ROW_H
            playing = iid == self.app.playing
            num, name = self.app.meta[iid]
            c.create_rectangle(0, y, LABEL_W, y + ROW_H, outline="#aaa",
                               fill="#2f7fd1" if playing else "#dcdcdc")
            c.create_text(8, y + ROW_H // 2, anchor="w", fill="#fff" if playing else "#111",
                          text=self._fit(("▶ " if playing else "") + f"{num} {name}", LABEL_W - 14))

    # ---- clicks
    def _hit(self):
        item = self.canvas.find_withtag("current")
        return self.hit.get(item[0]) if item else None

    def _click(self, e):
        h = self._hit()
        if h:
            self.app.show_info(h[0], h[1])

    def _double(self, e):
        h = self._hit()
        if h:
            self.app.play_iid(h[0])

    def _row_iid(self, e):
        r = int(self.labels.canvasy(e.y) // ROW_H)
        return self.app.rows[r] if 0 <= r < len(self.app.rows) else None

    def _label_click(self, e):
        iid = self._row_iid(e)
        if iid:
            self.app.show_info(iid, None)

    def _label_double(self, e):
        iid = self._row_iid(e)
        if iid:
            self.app.play_iid(iid)


# ---------------------------------------------------------------- main window
class Viewer(tk.Tk):
    def __init__(self, cfg, channels):
        super().__init__()
        self.cfg = cfg
        self.title("HDHomeRun Viewer")
        self.geometry("1000x700")
        self.proc = None
        self.ipc = None
        self.events = queue.Queue()
        self.guide_q = queue.Queue()  # results from the guide thread, handled on the Tk thread
        self.guide_progs = {}
        self.rows, self.urls, self.meta, self.cid = [], {}, {}, {}
        self.playing = None        # iid of the channel mpv is on
        self.info_target = None    # (iid, Prog or None) shown in the info panel
        self._info_key = None
        self._title_sent = None
        self.entry_titles = {}     # iid -> title currently in mpv's playlist
        self.can_edit_playlist = mpv_can_edit_playlist()
        self.status = tk.StringVar(value="Guide: loading...")
        self.clock = tk.StringVar()
        self.banner = tk.StringVar(value="Not playing")

        base = tkfont.nametofont("TkDefaultFont")
        bold = base.copy()
        bold.configure(weight="bold")
        big = base.copy()
        big.configure(weight="bold", size=base.cget("size") + 3)

        ttk.Label(self, textvariable=self.banner, font=big).pack(side=tk.TOP, anchor="w", padx=10, pady=(10, 2))

        bar = ttk.Frame(self)
        bar.pack(side=tk.BOTTOM, fill=tk.X, padx=10, pady=(0, 10))
        ttk.Button(bar, text="Play", command=self.play_channel).pack(side=tk.LEFT)
        ttk.Button(bar, text="Stop", command=self.stop_stream).pack(side=tk.LEFT, padx=5)
        ttk.Label(bar, textvariable=self.status).pack(side=tk.LEFT, padx=15)
        ttk.Label(bar, textvariable=self.clock).pack(side=tk.RIGHT)

        self.info = tk.Text(self, height=12, wrap="word", state="disabled", relief="flat",
                            padx=10, pady=6, font=base, background=self.cget("background"))
        self.info.tag_configure("title", font=bold)
        self.info.tag_configure("dim", foreground="#666666")
        self.info.pack(side=tk.BOTTOM, fill=tk.X, padx=10, pady=(0, 6))

        self.nb = ttk.Notebook(self)
        self.nb.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)
        chan_tab = ttk.Frame(self.nb)
        self.nb.add(chan_tab, text="Channels")

        cols = ("channel", "now", "next")
        self.tree = ttk.Treeview(chan_tab, columns=cols, show="headings", selectmode="browse")
        for col, text, width, stretch in (("channel", "Channel", 200, False),
                                          ("now", "Now", 380, True),
                                          ("next", "Next", 300, True)):
            self.tree.heading(col, text=text)
            self.tree.column(col, width=width, stretch=stretch)
        sb = ttk.Scrollbar(chan_tab, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        self.tree.pack(fill=tk.BOTH, expand=True)
        self.tree.tag_configure("playing", background="#cfe6ff", foreground="#000000")

        for c in channels:
            if not c.get("URL"):
                continue
            num, name = c.get("GuideNumber", ""), c.get("GuideName", "")
            iid = self.tree.insert("", tk.END, values=(f"{num} - {name}", "", ""))
            self.rows.append(iid)
            self.urls[iid] = c["URL"]
            self.meta[iid] = (num, name)
        self.url_index = {self.urls[i]: n for n, i in enumerate(self.rows)}
        if self.rows:
            self.tree.selection_set(self.rows[0])
            self.tree.focus(self.rows[0])
            self.info_target = (self.rows[0], None)
        self.tree.bind("<Double-Button-1>", self.play_channel)
        self.tree.bind("<Return>", self.play_channel)
        self.tree.bind("<<TreeviewSelect>>", self.on_select)

        self.guide_view = GuideGrid(self.nb, self)
        self.nb.add(self.guide_view, text="Guide")

        self.protocol("WM_DELETE_WINDOW", self.on_close)
        threading.Thread(target=self.guide_worker, daemon=True).start()
        self.tick()
        self.poll_events()

    # ---- selection / info panel
    def select_channel(self, iid):
        self.tree.selection_set(iid)
        self.tree.see(iid)

    def on_select(self, event=None):
        sel = self.tree.selection()
        if sel and (not self.info_target or self.info_target[0] != sel[0]):
            self.info_target = (sel[0], None)
            self.render_info()

    def show_info(self, iid, prog):
        self.info_target = (iid, prog)
        self.select_channel(iid)
        self.render_info()

    def render_info(self):
        if not self.info_target:
            return
        iid, prog = self.info_target
        now = datetime.now(timezone.utc)
        num, name = self.meta[iid]
        cid = self.cid.get(iid)
        if prog is None:
            prog, _ = now_next(self.guide_progs, cid, now)
        seg = [(f"{num} {name}\n", "dim")]
        if prog is None:
            seg.append(("No guide data for this channel right now.\n", ""))
        else:
            seg.append((prog.title + "\n", "title"))
            if prog.sub:
                seg.append((prog.sub + "\n", ""))
            meta = [when(prog, now), f"Runs {_dur(_mins(prog.stop - prog.start))}"]
            if prog.cat:
                meta.append(prog.cat)
            seg.append(("  ·  ".join(meta) + "\n", "dim"))
            if prog.desc:
                seg.append(("\n" + prog.desc + "\n", ""))
            more = [p for p in self.guide_progs.get(cid, []) if p.start >= prog.stop][:3]
            if more:
                seg.append(("\nComing up\n", "dim"))
                for p in more:
                    seg.append((f"{p.title}  ({starts_in(p.start, now)})\n", ""))
        key = tuple(seg)
        if key == self._info_key:  # unchanged: leave the panel (and its scroll position) alone
            return
        self._info_key = key
        self.info.configure(state="normal")
        self.info.delete("1.0", "end")
        for text, tag in seg:
            self.info.insert("end", text, tag)
        self.info.configure(state="disabled")

    # ---- guide
    def guide_worker(self):
        try:
            self.guide_q.put(("ok",) + load_guide(fetch_guide(self.cfg)))
        except Exception as e:
            self.guide_q.put(("err", f"Guide unavailable: {e}"))

    def guide_ready(self, names, progs):
        self.guide_progs = progs
        for iid, (num, name) in self.meta.items():
            cid = names.get(norm(name)) or names.get(norm(num))
            if cid:
                self.cid[iid] = cid
        self.status.set(f"Guide: {len(self.cid)}/{len(self.rows)} channels matched")
        self.guide_view.build()
        self.refresh_all()

    def refresh_row(self, iid, now=None):
        now = now or datetime.now(timezone.utc)
        num, name = self.meta[iid]
        playing = iid == self.playing
        now_txt = next_txt = ""
        cur, nxt = now_next(self.guide_progs, self.cid.get(iid), now)
        if cur:
            now_txt = f"{cur.title}  ({time_left(cur.stop, now)})"
        if nxt:
            next_txt = f"{nxt.title}  ({starts_in(nxt.start, now)})"
        self.tree.item(iid, values=(("▶ " if playing else "") + f"{num} - {name}", now_txt, next_txt),
                       tags=("playing",) if playing else ())

    def refresh_all(self):
        now = datetime.now(timezone.utc)
        for iid in self.rows:
            self.refresh_row(iid, now)
        self.update_banner(now)
        self.render_info()
        self.guide_view.refresh(now)
        self.sync_playlist_titles(now)

    def tick(self):
        self.clock.set(datetime.now().strftime("%I:%M%p").lstrip("0").lower())
        self.refresh_all()
        self.after(15_000, self.tick)

    # ---- playing state
    def media_title(self, now=None):
        if not self.playing:
            return "", None
        now = now or datetime.now(timezone.utc)
        num, name = self.meta[self.playing]
        cur, _ = now_next(self.guide_progs, self.cid.get(self.playing), now)
        return f"{num} {name}" + (f" - {cur.title}" if cur else ""), cur

    def playlist_title(self, iid, now):
        num, name = self.meta[iid]
        cur, _ = now_next(self.guide_progs, self.cid.get(iid), now)
        return f"{num} {name}" + (f" - {cur.title}" if cur else "")

    def sync_playlist_titles(self, now):
        """Keep mpv's own playlist (F8) showing each channel's current programme."""
        if not (self.can_edit_playlist and self.running()):
            return
        for idx, iid in enumerate(self.rows):
            if iid == self.playing:  # the playing entry can't be swapped; refreshed once you leave it
                continue
            title = self.playlist_title(iid, now)
            if title == self.entry_titles.get(iid):
                continue
            entry = os.path.join(CACHE_DIR, f"entry_{idx}.m3u")
            with open(entry, "w") as f:
                f.write(f"#EXTM3U\n#EXTINF:-1,{title}\n{self.urls[iid]}\n")
            # insert the retitled entry first, then drop the old one (now at idx + 1),
            # so a failed insert can never leave a hole in the playlist
            self.ipc.command("loadlist", entry, "insert-at", idx)
            self.ipc.command("playlist-remove", idx + 1)
            self.entry_titles[iid] = title

    def update_banner(self, now=None):
        if not self.playing:
            self.banner.set("Not playing")
            return
        now = now or datetime.now(timezone.utc)
        title, cur = self.media_title(now)
        self.banner.set("▶  " + title + (f"  ({time_left(cur.stop, now)})" if cur else ""))
        if title != self._title_sent and self.ipc and self.ipc.connected:
            self._title_sent = title  # the mpv window title follows the programme
            self.ipc.command("set_property", "force-media-title", title)

    def set_playing(self, iid):
        if iid == self.playing:
            return
        old, self.playing = self.playing, iid
        for i in (old, iid):
            if i:
                self.refresh_row(i)
        self._title_sent = None
        self.update_banner()
        self.guide_view.draw_labels()

    # ---- playback
    def running(self):
        return (self.proc is not None and self.proc.poll() is None
                and self.ipc is not None and self.ipc.connected)

    def play_iid(self, iid):
        self.select_channel(iid)
        self.play_channel()

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
            now = datetime.now(timezone.utc)
            self.entry_titles = {}
            for iid in self.rows:
                self.entry_titles[iid] = self.playlist_title(iid, now)
                f.write(f"#EXTINF:-1,{self.entry_titles[iid]}\n{self.urls[iid]}\n")
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
                url = self.events.get_nowait()
                pos = self.url_index.get(url) if isinstance(url, str) else None
                if pos is not None:
                    self.on_channel_changed(pos)
        except queue.Empty:
            pass
        try:
            msg = self.guide_q.get_nowait()
            if msg[0] == "ok":
                self.guide_ready(msg[1], msg[2])
            else:
                self.status.set(msg[1])
        except queue.Empty:
            pass
        if self.proc is not None and self.proc.poll() is not None:
            self.proc = None
            if self.ipc:
                self.ipc.close()
            self.set_playing(None)
        self.after(150, self.poll_events)

    def on_channel_changed(self, pos):
        iid = self.rows[pos]
        self.set_playing(iid)
        self.select_channel(iid)
        now = datetime.now(timezone.utc)
        num, name = self.meta[iid]
        lines = [f"{num} {name}"]
        cur, nxt = now_next(self.guide_progs, self.cid.get(iid), now)
        if cur:
            lines.append(f"Now: {cur.title} ({time_left(cur.stop, now)})")
        if nxt:
            lines.append(f"Next: {nxt.title} ({starts_in(nxt.start, now)})")
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
        self.set_playing(None)

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
