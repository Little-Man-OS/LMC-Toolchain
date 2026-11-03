#!/usr/bin/env python3
"""
LMC IDE: an editor for LMC source with live syntax checking, one key build,
and a built in console that runs your program on an emulated Raspberry Pi 3B.

    lmc-ide [folder | file.lmc ...]

Needs Python 3 with Tk (included in the normal Windows and macOS installers).
QEMU is only needed for the Run button.
"""
import json
import os
import queue
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, font as tkfont, messagebox, simpledialog, ttk

import traceback

try:
    from . import compiler as lmcc
except ImportError:          # run as a plain script, not as part of the package
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from lmc import compiler as lmcc

CONFIG_PATH = os.path.join(os.path.expanduser("~"), ".lmc_ide.json")
LOG_PATH = os.path.join(os.path.expanduser("~"), ".lmc_ide_error.log")
PROJECT_FILE = ".lmcproject"
HIDDEN_NAMES = {"__pycache__", "node_modules"}

T = dict(
    bg="#1b1d26", fg="#d7dae0", panel="#232634", gutter="#171922", gutter_fg="#5c6370",
    select="#34456b", curline="#222737", comment="#6b7385", mnemonic="#7aa2f7",
    datatype="#bb9af7", label_def="#e0af68", label_ref="#c9b37e", number="#ff9e64",
    string="#9ece6a", error="#f7768e", errline="#3b2230", info="#7f8aa3", accent="#7aa2f7",
)

TEMPLATE = "; new LMC program\nmain:\n        hlt\n"

HELP_TEXT = """LMC quick reference

Instructions
  HLT            stop
  LDA x          accumulator = x
  STA x          x = accumulator
  ADD x / SUB x  add or subtract x
  BRA label      branch always
  BRZ label      branch if the accumulator is zero
  BRP label      branch if zero or positive
  INP            read a number from the serial console
  OUT [x]        print a number (the accumulator if no operand)
  OTC [x]        print a character or a CHA string
  IMP "file"     include another .lmc file

Operands
  42             slot 42 (an 8 byte cell)
  name           a data label
  #5  #0x10  #'A'   an immediate value (not for STA)

Data (name: TYPE value, or the classic  name TYPE value)
  DAT 8 bytes   INT 4 bytes   CHA 1 byte or "string"   FLO   DUB

The program starts at the label  main:
Comments start with ; or //

Keys
  F5 build    F6 build and run    Shift+F6 stop    F8 machine code listing
  Ctrl+N/O/S/W new, open, save, close    Ctrl+F find    F3 find next    Ctrl+G go to line
"""

TAGS = ["comment", "mnemonic", "datatype", "label_def", "label_ref", "number", "string"]


def load_config():
    try:
        with open(CONFIG_PATH) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def save_config(cfg):
    try:
        with open(CONFIG_PATH, "w") as fh:
            json.dump(cfg, fh)
    except OSError:
        pass


def is_hidden(name):
    return name.startswith(".") or name in HIDDEN_NAMES


def read_project(root):
    try:
        with open(os.path.join(root, PROJECT_FILE), encoding="utf-8") as fh:
            data = json.load(fh)
            return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write_project(root, data):
    try:
        with open(os.path.join(root, PROJECT_FILE), "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
    except OSError:
        pass


def find_main(root):
    """Pick the program's main file: main.lmc in the root, else the shallowest file with a main: label."""
    top = os.path.join(root, "main.lmc")
    if os.path.isfile(top):
        return top
    found, scanned = [], 0
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if not is_hidden(d))
        depth = os.path.relpath(dirpath, root).count(os.sep)
        if depth > 5:
            dirs[:] = []
            continue
        for name in sorted(files):
            if not name.lower().endswith(".lmc"):
                continue
            scanned += 1
            if scanned > 500:
                break
            try:
                with open(os.path.join(dirpath, name), encoding="utf-8", errors="replace") as fh:
                    if re.search(r"^\s*main\s*:", fh.read(65536), re.M):
                        found.append((depth, os.path.join(dirpath, name)))
            except OSError:
                pass
    return min(found)[1] if found else None


def looks_like_text(path):
    try:
        if os.path.getsize(path) > 4 * 1024 * 1024:
            return False
        with open(path, "rb") as fh:
            return b"\0" not in fh.read(4096)
    except OSError:
        return False


def find_qemu(cfg):
    path = cfg.get("qemu")
    if path and os.path.isfile(path):
        return path
    found = shutil.which("qemu-system-aarch64")
    if found:
        return found
    for cand in (r"C:\Program Files\qemu\qemu-system-aarch64.exe",
                 r"C:\Program Files (x86)\qemu\qemu-system-aarch64.exe",
                 "/opt/homebrew/bin/qemu-system-aarch64", "/usr/local/bin/qemu-system-aarch64"):
        if os.path.isfile(cand):
            return cand
    return None


class ChangeText(tk.Text):
    """A Text widget that sends <<Change>> whenever its contents change."""

    def __init__(self, *args, **kw):
        super().__init__(*args, **kw)
        self._orig = self._w + "_orig"
        self.tk.call("rename", self._w, self._orig)
        self.tk.createcommand(self._w, self._proxy)

    def _proxy(self, *args):
        result = self.tk.call((self._orig,) + args)
        if args and (args[0] in ("insert", "replace", "delete")
                     or (args[0] == "edit" and len(args) > 1 and args[1] in ("undo", "redo"))):
            self.event_generate("<<Change>>", when="tail")
        return result


class Runner:
    """Starts QEMU and connects to its emulated serial port over TCP."""

    def __init__(self, emit):
        self.emit = emit          # emit(kind, text), safe to call from any thread
        self.proc = None
        self.sock = None
        self.stopping = False

    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def start(self, qemu, image):
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        args = [qemu, "-M", "raspi3b", "-kernel", image, "-display", "none",
                "-serial", "null", "-serial", f"tcp:127.0.0.1:{port},server"]
        flags = 0x08000000 if os.name == "nt" else 0
        self.stopping = False
        self.proc = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT, creationflags=flags)
        threading.Thread(target=self._log, args=(self.proc,), daemon=True).start()
        threading.Thread(target=self._pump, args=(self.proc, port), daemon=True).start()

    def _log(self, proc):
        for line in iter(proc.stdout.readline, b""):
            text = line.decode("utf-8", "replace")
            if "QEMU waiting for connection" not in text:
                self.emit("log", text)

    def _pump(self, proc, port):
        sock = None
        for _ in range(60):
            if proc.poll() is not None:
                break
            try:
                sock = socket.create_connection(("127.0.0.1", port), timeout=0.5)
                break
            except OSError:
                time.sleep(0.1)
        if sock is not None:
            self.sock = sock
            sock.settimeout(None)
            try:
                while True:
                    data = sock.recv(4096)
                    if not data:
                        break
                    self.emit("data", data.decode("utf-8", "replace"))
            except OSError:
                pass
            self.sock = None
        else:
            self.stop()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
        self.emit("exit", "stopped" if self.stopping else "QEMU exited")

    def send(self, text):
        if self.sock:
            try:
                self.sock.sendall(text.encode("utf-8"))
            except OSError:
                pass

    def stop(self):
        self.stopping = True
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()


class Doc:
    """One open file: a tab with a line number gutter and an editor."""

    _untitled = 0

    def __init__(self, ide, path=None, text=""):
        self.ide = ide
        self.path = os.path.abspath(path) if path else None
        if self.path is None:
            Doc._untitled += 1
            self.untitled_name = f"untitled-{Doc._untitled}.lmc"
        self.modified = False
        self.err_lines = {}
        self.frame = ttk.Frame(ide.notebook)
        self.gutter = tk.Canvas(self.frame, width=58, highlightthickness=0, bg=T["gutter"])
        self.text = ChangeText(
            self.frame, wrap="none", undo=True, maxundo=-1, bg=T["bg"], fg=T["fg"],
            insertbackground=T["fg"], selectbackground=T["select"], selectforeground=T["fg"],
            relief="flat", borderwidth=0, highlightthickness=0, font=ide.font,
            padx=8, pady=4, tabs=(ide.font.measure("0" * 8),), insertwidth=2)
        self.vsb = ttk.Scrollbar(self.frame, orient="vertical", command=self.text.yview)
        self.hsb = ttk.Scrollbar(self.frame, orient="horizontal", command=self.text.xview)
        self.text.configure(yscrollcommand=self._yscroll, xscrollcommand=self.hsb.set)
        self.gutter.grid(row=0, column=0, sticky="ns")
        self.text.grid(row=0, column=1, sticky="nsew")
        self.vsb.grid(row=0, column=2, sticky="ns")
        self.hsb.grid(row=1, column=1, sticky="ew")
        self.frame.rowconfigure(0, weight=1)
        self.frame.columnconfigure(1, weight=1)

        t = self.text
        t.tag_configure("curline", background=T["curline"])
        t.tag_configure("errline", background=T["errline"])
        t.tag_configure("comment", foreground=T["comment"])
        t.tag_configure("mnemonic", foreground=T["mnemonic"], font=ide.bold_font)
        t.tag_configure("datatype", foreground=T["datatype"], font=ide.bold_font)
        t.tag_configure("label_def", foreground=T["label_def"], font=ide.bold_font)
        t.tag_configure("label_ref", foreground=T["label_ref"])
        t.tag_configure("number", foreground=T["number"])
        t.tag_configure("string", foreground=T["string"])
        t.tag_raise("sel")
        t.insert("1.0", text)
        t.edit_reset()
        t.edit_modified(False)
        t.mark_set("insert", "1.0")
        t.bind("<<Change>>", lambda e: ide.on_change(self))
        t.bind("<<Modified>>", lambda e: self._on_modified())
        t.bind("<KeyRelease>", lambda e: ide.update_cursor(self))
        t.bind("<ButtonRelease-1>", lambda e: ide.update_cursor(self))
        t.bind("<Return>", self._on_return)
        t.bind("<Tab>", self._on_tab)
        t.bind("<Configure>", lambda e: self.redraw_gutter())
        t.bind("<MouseWheel>", lambda e: self.frame.after_idle(self.redraw_gutter))
        ide.bind_keys(t)

    # --- identity -----------------------------------------------------------------
    @property
    def key(self):
        """The absolute path used to match this buffer to compiler errors."""
        return self.path or os.path.join(os.getcwd(), self.untitled_name)

    @property
    def name(self):
        return os.path.basename(self.path) if self.path else self.untitled_name

    def get_text(self):
        return self.text.get("1.0", "end-1c")

    def _on_modified(self):
        if self.text.edit_modified():
            self.modified = True
            self.ide.update_tab_title(self)

    # --- editing helpers --------------------------------------------------------------
    def _on_return(self, event):
        t = self.text
        before = t.get("insert linestart", "insert")
        indent = re.match(r"[ \t]*", before).group()
        code = lmcc.strip_comment(before).strip()
        if code.endswith(":") and " " not in code and not indent:
            indent = " " * 8
        t.insert("insert", "\n" + indent)
        t.see("insert")
        return "break"

    def _on_tab(self, event):
        t = self.text
        if t.tag_ranges("sel"):
            return None
        col = int(t.index("insert").split(".")[1])
        t.insert("insert", " " * (8 - col % 8))
        return "break"

    # --- gutter --------------------------------------------------------------------------
    def _yscroll(self, first, last):
        self.vsb.set(first, last)
        self.redraw_gutter()

    def redraw_gutter(self):
        c, t = self.gutter, self.text
        c.delete("all")
        width = int(c["width"])
        index = t.index("@0,0")
        previous = None
        while index != previous:
            info = t.dlineinfo(index)
            if info is None:
                break
            y = info[1]
            line = int(index.split(".")[0])
            if line in self.err_lines:
                c.create_oval(8, y + 4, 16, y + 12, fill=T["error"], outline="")
            c.create_text(width - 8, y, anchor="ne", text=str(line), fill=T["gutter_fg"],
                          font=self.ide.font)
            previous = index
            index = t.index(f"{index}+1line")

    # --- syntax colouring --------------------------------------------------------------
    def highlight(self):
        t = self.text
        ranges = {tag: [] for tag in TAGS}
        for tag in TAGS:
            t.tag_remove(tag, "1.0", "end")
        number = re.compile(r"[+-]?(\d+|0[xX][0-9a-fA-F]+)$")
        for ln, line in enumerate(self.get_text().split("\n"), 1):
            code = lmcc.strip_comment(line)
            if len(code) < len(line):
                ranges["comment"] += [f"{ln}.{len(code)}", f"{ln}.{len(line)}"]
            toks = [(m.start(), m.end(), m.group()) for m in lmcc.TOKEN.finditer(code)]

            def mark(tag, tok):
                ranges[tag] += [f"{ln}.{tok[0]}", f"{ln}.{tok[1]}"]

            j = 0
            while j < len(toks) and toks[j][2].endswith(":") and len(toks[j][2]) > 1:
                mark("label_def", toks[j])
                j += 1
            if j >= len(toks):
                continue
            up = toks[j][2].upper()
            if up in lmcc.DATA_KW:
                mark("datatype", toks[j])
                j += 1
            elif up in lmcc.MNEMONICS:
                mark("mnemonic", toks[j])
                j += 1
            elif j + 1 < len(toks) and toks[j + 1][2].upper() in lmcc.DATA_KW \
                    and lmcc.IDENT.fullmatch(toks[j][2]):
                mark("label_def", toks[j])
                mark("datatype", toks[j + 1])
                j += 2
            else:
                continue
            for tok in toks[j:]:
                text = tok[2]
                body = text[1:] if text.startswith("#") else text
                if body[:1] in ("'", '"'):
                    mark("string", tok)
                elif text.startswith("#") or number.match(text):
                    mark("number", tok)
                elif lmcc.IDENT.fullmatch(text):
                    mark("label_ref", tok)
        for tag, rng in ranges.items():
            if rng:
                t.tag_add(tag, *rng)

    def apply_errors(self):
        t = self.text
        t.tag_remove("errline", "1.0", "end")
        for ln in self.err_lines:
            t.tag_add("errline", f"{ln}.0", f"{ln + 1}.0")
        self.redraw_gutter()


class IDE(tk.Tk):
    def __init__(self, files=(), folder=None):
        super().__init__()
        self.cfg = load_config()
        self.title("LMC IDE")
        self.geometry("1100x760")
        self.configure(bg=T["bg"])
        families = set(tkfont.families())
        family = next((f for f in ("Cascadia Mono", "Consolas", "Menlo", "DejaVu Sans Mono",
                                   "Liberation Mono", "Courier New") if f in families), "TkFixedFont")
        self.font = tkfont.Font(family=family, size=12)
        self.bold_font = tkfont.Font(family=family, size=12, weight="bold")
        self.docs = []
        self.errors = []
        self.main_path = None
        self.root_dir = None          # the open project folder, if any
        self.seen_errors = set()
        self._poll_job = None
        self.check_job = None
        self.highlight_jobs = {}
        self.runner = Runner(self._emit)
        self.events = queue.Queue()
        self.last_find = ""
        self._style()
        self._build_ui()
        self._menus()
        self.bind_keys(self)
        self.protocol("WM_DELETE_WINDOW", self.quit_app)
        if folder is None and not files:
            last = self.cfg.get("last_folder")
            if last and os.path.isdir(last):
                folder = last
        if folder:
            self.open_folder(folder)
        for f in files:
            self.open_path(f)
        if not self.docs:
            self.new_doc()
        self._poll_job = self.after(60, self._poll_events)

    def destroy(self):
        jobs = [self._poll_job, self.check_job] + list(self.highlight_jobs.values())
        for job in jobs:
            if job:
                try:
                    self.after_cancel(job)
                except tk.TclError:
                    pass
        super().destroy()

    def report_callback_exception(self, exc, val, tb):
        text = "".join(traceback.format_exception(exc, val, tb))
        try:
            with open(LOG_PATH, "a", encoding="utf-8") as fh:
                fh.write(time.strftime("%Y-%m-%d %H:%M:%S") + "\n" + text + "\n")
        except OSError:
            pass
        sys.stderr.write(text)
        try:
            self._console_write(text, "err")
            self.set_status(f"Internal error: {val}", error=True)
            if str(val) not in self.seen_errors:
                self.seen_errors.add(str(val))
                messagebox.showerror("LMC IDE", f"Something went wrong:\n\n{val}\n\n"
                                     f"Details were saved to {LOG_PATH}")
        except Exception:
            pass

    # --- construction ------------------------------------------------------------------
    def _style(self):
        s = ttk.Style(self)
        s.theme_use("clam")
        s.configure(".", background=T["panel"], foreground=T["fg"], fieldbackground=T["bg"],
                    bordercolor=T["panel"], lightcolor=T["panel"], darkcolor=T["panel"])
        s.configure("TNotebook", background=T["panel"], borderwidth=0)
        s.configure("TNotebook.Tab", background=T["panel"], foreground=T["info"],
                    padding=(14, 5), borderwidth=0)
        s.map("TNotebook.Tab", background=[("selected", T["bg"])],
              foreground=[("selected", T["fg"])])
        s.configure("Treeview", background=T["bg"], fieldbackground=T["bg"],
                    foreground=T["fg"], borderwidth=0, rowheight=24)
        s.configure("Treeview.Heading", background=T["panel"], foreground=T["info"],
                    borderwidth=0)
        s.map("Treeview", background=[("selected", T["select"])])
        s.configure("TButton", padding=(12, 4), background="#2d3246", foreground=T["fg"])
        s.map("TButton", background=[("active", "#3a4160"), ("disabled", T["panel"])],
              foreground=[("disabled", T["gutter_fg"])])
        s.configure("Accent.TButton", background="#3b5bdb", foreground="white")
        s.map("Accent.TButton", background=[("active", "#4c6ef5"), ("disabled", T["panel"])])
        s.configure("TEntry", fieldbackground=T["bg"], foreground=T["fg"], insertcolor=T["fg"])
        s.configure("TScrollbar", background="#2d3246", troughcolor=T["bg"], borderwidth=0,
                    arrowsize=12)
        s.configure("TPanedwindow", background=T["panel"])
        s.configure("Status.TLabel", background=T["panel"], foreground=T["info"], padding=(10, 4))

    def _build_ui(self):
        bar = ttk.Frame(self)
        bar.pack(fill="x")
        self.build_btn = ttk.Button(bar, text="Build  F5", command=self.build)
        self.run_btn = ttk.Button(bar, text="Run  F6", style="Accent.TButton", command=self.run)
        self.stop_btn = ttk.Button(bar, text="Stop", command=self.stop, state="disabled")
        for b in (self.build_btn, self.run_btn, self.stop_btn):
            b.pack(side="left", padx=(8, 0), pady=6)
        self.main_label = ttk.Label(bar, text="", style="Status.TLabel")
        self.main_label.pack(side="right", padx=8)

        status = ttk.Frame(self)
        status.pack(side="bottom", fill="x")
        self.status = ttk.Label(status, text="Ready", style="Status.TLabel")
        self.status.pack(side="left")
        self.pos_label = ttk.Label(status, text="Ln 1, Col 1", style="Status.TLabel")
        self.pos_label.pack(side="right")

        self.hpanes = ttk.PanedWindow(self, orient="horizontal")
        self.hpanes.pack(fill="both", expand=True)
        self._build_sidebar()
        panes = ttk.PanedWindow(self.hpanes, orient="vertical")
        self.hpanes.add(panes, weight=1)
        self.notebook = ttk.Notebook(panes)
        self.notebook.bind("<<NotebookTabChanged>>", lambda e: self._tab_changed())
        panes.add(self.notebook, weight=4)

        self.bottom = ttk.Notebook(panes)
        panes.add(self.bottom, weight=1)

        pframe = ttk.Frame(self.bottom)
        self.problems = ttk.Treeview(pframe, columns=("file", "line", "message"),
                                     show="headings", selectmode="browse")
        for col, text, width in (("file", "File", 180), ("line", "Line", 60), ("message", "Problem", 700)):
            self.problems.heading(col, text=text, anchor="w")
            self.problems.column(col, width=width, anchor="w", stretch=(col == "message"))
        self.problems.tag_configure("error", foreground=T["error"])
        psb = ttk.Scrollbar(pframe, orient="vertical", command=self.problems.yview)
        self.problems.configure(yscrollcommand=psb.set)
        self.problems.pack(side="left", fill="both", expand=True)
        psb.pack(side="right", fill="y")
        self.problems.bind("<Double-1>", self._problem_clicked)
        self.problems.bind("<Return>", self._problem_clicked)
        self.bottom.add(pframe, text="Problems")
        self.problem_rows = {}

        cframe = ttk.Frame(self.bottom)
        self.console = tk.Text(cframe, height=8, bg=T["bg"], fg=T["fg"], relief="flat",
                               highlightthickness=0, font=self.font, state="disabled",
                               padx=8, pady=4, wrap="word", insertbackground=T["fg"])
        self.console.tag_configure("info", foreground=T["info"])
        self.console.tag_configure("err", foreground=T["error"])
        csb = ttk.Scrollbar(cframe, orient="vertical", command=self.console.yview)
        self.console.configure(yscrollcommand=csb.set)
        entry_row = ttk.Frame(cframe)
        ttk.Label(entry_row, text="Input to the program:", style="Status.TLabel").pack(side="left")
        self.console_entry = ttk.Entry(entry_row)
        self.console_entry.pack(side="left", fill="x", expand=True, padx=6, pady=3)
        self.console_entry.bind("<Return>", self._send_input)
        entry_row.pack(side="bottom", fill="x")
        self.console.pack(side="left", fill="both", expand=True)
        csb.pack(side="right", fill="y")
        self.bottom.add(cframe, text="Console")

    def _menus(self):
        m = tk.Menu(self)
        opts = dict(tearoff=0, bg=T["panel"], fg=T["fg"], activebackground=T["select"],
                    activeforeground=T["fg"])
        f = tk.Menu(m, **opts)
        f.add_command(label="New", accelerator="Ctrl+N", command=self.new_doc)
        f.add_command(label="Open file...", accelerator="Ctrl+O", command=self.open_dialog)
        f.add_command(label="Open folder...", accelerator="Ctrl+Shift+O", command=self.open_folder_dialog)
        f.add_command(label="Close folder", command=self.close_folder)
        f.add_separator()
        f.add_command(label="Save", accelerator="Ctrl+S", command=self.save)
        f.add_command(label="Save As...", accelerator="Ctrl+Shift+S", command=self.save_as)
        f.add_command(label="Close tab", accelerator="Ctrl+W", command=self.close_doc)
        f.add_separator()
        f.add_command(label="Quit", command=self.quit_app)
        m.add_cascade(label="File", menu=f)
        e = tk.Menu(m, **opts)
        e.add_command(label="Undo", accelerator="Ctrl+Z", command=lambda: self._edit("undo"))
        e.add_command(label="Redo", accelerator="Ctrl+Y", command=lambda: self._edit("redo"))
        e.add_separator()
        e.add_command(label="Find...", accelerator="Ctrl+F", command=self.find)
        e.add_command(label="Find next", accelerator="F3", command=self.find_next)
        e.add_command(label="Go to line...", accelerator="Ctrl+G", command=self.goto_line)
        m.add_cascade(label="Edit", menu=e)
        v = tk.Menu(m, **opts)
        v.add_command(label="Show or hide the project panel", accelerator="Ctrl+B", command=self.toggle_sidebar)
        v.add_command(label="Refresh project panel", accelerator="F2", command=self.refresh_tree)
        m.add_cascade(label="View", menu=v)
        b = tk.Menu(m, **opts)
        b.add_command(label="Build", accelerator="F5", command=self.build)
        b.add_command(label="Build and run in QEMU", accelerator="F6", command=self.run)
        b.add_command(label="Stop", accelerator="Shift+F6", command=self.stop)
        b.add_separator()
        b.add_command(label="Machine code listing", accelerator="F8", command=self.show_listing)
        b.add_separator()
        b.add_command(label="Use this file as the main file", command=self.set_main)
        b.add_command(label="Set QEMU location...", command=self.pick_qemu)
        m.add_cascade(label="Build", menu=b)
        h = tk.Menu(m, **opts)
        h.add_command(label="Language reference", command=self.show_help)
        m.add_cascade(label="Help", menu=h)
        self.configure(menu=m)

    def bind_keys(self, widget):
        table = {
            "<Control-n>": self.new_doc, "<Control-o>": self.open_dialog, "<Control-s>": self.save,
            "<Control-S>": self.save_as, "<Control-w>": self.close_doc, "<Control-f>": self.find,
            "<F3>": self.find_next, "<Control-g>": self.goto_line, "<F5>": self.build,
            "<F6>": self.run, "<Shift-F6>": self.stop, "<F8>": self.show_listing,
            "<Control-q>": self.quit_app, "<Control-O>": self.open_folder_dialog,
            "<Control-b>": self.toggle_sidebar, "<F2>": self.refresh_tree,
        }
        for seq, fn in table.items():
            widget.bind(seq, lambda e, fn=fn: (fn(), "break")[1])

    # --- document handling ---------------------------------------------------------------
    def current(self):
        sel = self.notebook.select()
        for d in self.docs:
            if str(d.frame) == str(sel):
                return d
        return None

    def add_doc(self, doc):
        self.docs.append(doc)
        self.notebook.add(doc.frame, text=doc.name)
        self.notebook.select(doc.frame)
        self.maybe_adopt_main(doc)
        self.after_idle(lambda: self.on_change(doc))
        doc.text.focus_set()

    def new_doc(self):
        self.add_doc(Doc(self, None, TEMPLATE))

    def open_dialog(self):
        paths = filedialog.askopenfilenames(
            initialdir=self.cfg.get("last_dir", os.getcwd()),
            filetypes=[("LMC source", "*.lmc"), ("All files", "*.*")])
        for p in paths:
            self.open_path(p)

    def open_path(self, path):
        path = os.path.abspath(path)
        for d in self.docs:
            if d.path == path:
                self.notebook.select(d.frame)
                return
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError as e:
            messagebox.showerror("Open", f"Cannot open {path}\n{e.strerror}")
            return
        self.cfg["last_dir"] = os.path.dirname(path)
        save_config(self.cfg)
        blank = [d for d in self.docs if d.path is None and not d.modified and d.get_text() == TEMPLATE]
        self.add_doc(Doc(self, path, text))
        if len(self.docs) > 1 and len(blank) == 1 and self.main_path == blank[0].key:
            self.main_path = None
        for d in blank:
            self._remove_doc(d)
        self.update_main_label()

    def save(self, doc=None):
        doc = doc or self.current()
        if doc is None:
            return False
        if doc.path is None:
            return self.save_as(doc)
        return self._write(doc, doc.path)

    def save_as(self, doc=None):
        doc = doc if isinstance(doc, Doc) else self.current()
        if doc is None:
            return False
        path = filedialog.asksaveasfilename(
            initialdir=self.cfg.get("last_dir", os.getcwd()),
            initialfile=doc.name, defaultextension=".lmc",
            filetypes=[("LMC source", "*.lmc"), ("All files", "*.*")])
        if not path:
            return False
        old_key = doc.key
        doc.path = os.path.abspath(path)
        if self.main_path == old_key:
            self.main_path = doc.key
        return self._write(doc, doc.path)

    def _write(self, doc, path):
        try:
            with open(path, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(doc.get_text())
        except OSError as e:
            messagebox.showerror("Save", f"Cannot save {path}\n{e.strerror}")
            return False
        doc.text.edit_modified(False)
        doc.modified = False
        self.cfg["last_dir"] = os.path.dirname(path)
        save_config(self.cfg)
        self.update_tab_title(doc)
        self.maybe_adopt_main(doc)
        self.set_status(f"Saved {path}")
        self.schedule_check()
        return True

    def confirm_close(self, doc):
        if not doc.modified:
            return True
        answer = messagebox.askyesnocancel("Unsaved changes", f"Save changes to {doc.name}?")
        if answer is None:
            return False
        return self.save(doc) if answer else True

    def close_doc(self):
        doc = self.current()
        if doc is None or not self.confirm_close(doc):
            return
        self._remove_doc(doc)

    def _remove_doc(self, doc):
        self.notebook.forget(doc.frame)
        self.docs.remove(doc)
        doc.frame.destroy()
        if not self.docs:
            self.new_doc()
        self.schedule_check()

    def quit_app(self):
        for doc in list(self.docs):
            self.notebook.select(doc.frame)
            if not self.confirm_close(doc):
                return
        self.runner.stop()
        self.destroy()

    def update_tab_title(self, doc):
        self.notebook.tab(doc.frame, text=doc.name + (" *" if doc.modified else ""))

    def maybe_adopt_main(self, doc):
        if self.main_path is None and re.search(r"^\s*main\s*:", doc.get_text(), re.M):
            self.main_path = doc.key
        self.update_main_label()

    def set_main(self, path=None):
        if path is None:
            doc = self.current()
            if not doc:
                return
            path = doc.key
        self.main_path = path
        if self.root_dir and self._inside(path, self.root_dir):
            data = read_project(self.root_dir)
            data["main"] = os.path.relpath(path, self.root_dir).replace(os.sep, "/")
            write_project(self.root_dir, data)
        self.update_main_label()
        self._mark_main()
        self.schedule_check()

    def effective_main(self):
        if self.main_path:
            return self.main_path
        doc = self.current()
        return doc.key if doc else None

    def update_main_label(self):
        main = self.effective_main()
        self.main_label.configure(text=f"Main file: {self.display_name(main)}" if main else "")

    def display_name(self, full):
        if self.root_dir and self._inside(full, self.root_dir):
            return os.path.relpath(full, self.root_dir)
        return os.path.basename(full)

    @staticmethod
    def _inside(path, folder):
        try:
            return os.path.commonpath([os.path.abspath(path), os.path.abspath(folder)]) == os.path.abspath(folder)
        except ValueError:
            return False

    def include_dirs(self):
        return [self.root_dir] if self.root_dir else []

    # --- change tracking, highlighting and checking --------------------------------------
    def on_change(self, doc):
        job = self.highlight_jobs.pop(doc, None)
        if job:
            self.after_cancel(job)
        self.highlight_jobs[doc] = self.after(40, lambda: self._do_highlight(doc))
        doc.redraw_gutter()
        self.update_cursor(doc)
        self.schedule_check()

    def _do_highlight(self, doc):
        self.highlight_jobs.pop(doc, None)
        if doc in self.docs:
            doc.highlight()
            doc.apply_errors()

    def schedule_check(self):
        if self.check_job:
            self.after_cancel(self.check_job)
        self.check_job = self.after(350, self.run_check)

    def _overrides(self):
        return {d.key: d.get_text() for d in self.docs}

    def run_check(self):
        self.check_job = None
        main = self.effective_main()
        self.update_main_label()
        if main is None:
            return
        overrides = self._overrides()
        errors, read = lmcc.check(main, overrides, True, self.include_dirs())
        errors = list(errors)
        for d in self.docs:
            if d.key not in read:
                # a file nobody imports yet: check it alone, ignoring references to other files
                alone, _ = lmcc.check(d.key, overrides, False, self.include_dirs())
                errors += [e for e in alone if e.loc is not None and e.loc.full == d.key
                           and not e.msg.startswith("undefined")]
        self.errors = lmcc.sort_errors(errors)
        self._show_errors()

    def _show_errors(self):
        self.problems.delete(*self.problems.get_children())
        self.problem_rows = {}
        for d in self.docs:
            d.err_lines = {}
        for e in self.errors:
            if e.loc is None:
                row = self.problems.insert("", "end", values=("", "", e.msg), tags=("error",))
                self.problem_rows[row] = None
                continue
            row = self.problems.insert("", "end", values=(self.display_name(e.loc.full), e.loc.line, e.msg),
                                       tags=("error",))
            self.problem_rows[row] = (e.loc.full, e.loc.line)
            for d in self.docs:
                if d.key == e.loc.full:
                    d.err_lines.setdefault(e.loc.line, e.msg)
        for d in self.docs:
            d.apply_errors()
        n = len(self.errors)
        self.bottom.tab(0, text=f"Problems ({n})" if n else "Problems")
        cur = self.current()
        if cur:
            self.update_cursor(cur)

    def _problem_clicked(self, event=None):
        sel = self.problems.selection()
        if not sel:
            return
        target = self.problem_rows.get(sel[0])
        if target:
            self.goto(target[0], target[1])

    def goto(self, key, line):
        for d in self.docs:
            if d.key == key:
                self.notebook.select(d.frame)
                d.text.mark_set("insert", f"{line}.0")
                d.text.see(f"{line}.0")
                d.text.focus_set()
                self.update_cursor(d)
                return
        if os.path.isfile(key):
            self.open_path(key)
            self.goto(key, line)

    def _tab_changed(self):
        doc = self.current()
        if doc:
            self.update_cursor(doc)
            doc.redraw_gutter()
            self.schedule_check()
            if self.root_dir and self.tree.exists(doc.key):
                self.tree.selection_set(doc.key)
                self.tree.see(doc.key)

    def update_cursor(self, doc):
        if doc is not self.current():
            return
        t = doc.text
        line, col = t.index("insert").split(".")
        self.pos_label.configure(text=f"Ln {line}, Col {int(col) + 1}")
        t.tag_remove("curline", "1.0", "end")
        t.tag_add("curline", "insert linestart", "insert lineend+1c")
        msg = doc.err_lines.get(int(line))
        if msg:
            self.set_status(msg, error=True)
        else:
            n = len(self.errors)
            self.set_status(f"{n} problem{'s' if n != 1 else ''}" if n else "No problems found")

    def set_status(self, text, error=False):
        self.status.configure(text=text, foreground=T["error"] if error else T["info"])

    def _edit(self, what):
        doc = self.current()
        if doc:
            try:
                doc.text.edit_undo() if what == "undo" else doc.text.edit_redo()
            except tk.TclError:
                pass

    # --- find and go to ----------------------------------------------------------------------
    def find(self):
        q = simpledialog.askstring("Find", "Find text:", initialvalue=self.last_find, parent=self)
        if q:
            self.last_find = q
            self.find_next()

    def find_next(self):
        doc = self.current()
        if not doc or not self.last_find:
            return
        t = doc.text
        pos = t.search(self.last_find, "insert+1c", stopindex="end", nocase=True)
        if not pos:
            pos = t.search(self.last_find, "1.0", stopindex="end", nocase=True)
        if pos:
            end = f"{pos}+{len(self.last_find)}c"
            t.tag_remove("sel", "1.0", "end")
            t.tag_add("sel", pos, end)
            t.mark_set("insert", pos)
            t.see(pos)
            self.update_cursor(doc)
        else:
            self.set_status(f"'{self.last_find}' not found")

    def goto_line(self):
        doc = self.current()
        if not doc:
            return
        n = simpledialog.askinteger("Go to line", "Line number:", parent=self, minvalue=1)
        if n:
            self.goto(doc.key, n)

    # --- build and run ---------------------------------------------------------------------------
    def _console_write(self, text, tag=None):
        text = text.replace("\r\n", "\n").replace("\r", "")
        self.console.configure(state="normal")
        self.console.insert("end", text, tag)
        self.console.see("end")
        self.console.configure(state="disabled")

    def _compile(self):
        """Checks, then compiles. Returns (asm, code, data) or None."""
        self.run_check()
        main = self.effective_main()
        if main is None:
            return None
        if self.errors:
            self.bottom.select(0)
            n = len(self.errors)
            self.set_status(f"Fix {n} problem{'s' if n != 1 else ''} before building", error=True)
            return None
        try:
            return lmcc.compile_file(main, self._overrides(), self.include_dirs())
        except lmcc.CompileFailed as failure:
            self.errors = failure.errors
            self._show_errors()
            self.bottom.select(0)
            return None

    def build(self):
        result = self._compile()
        if result is None:
            return None
        a, code, data = result
        out = os.path.join(self.root_dir or os.path.dirname(self.effective_main()), "kernel8.img")
        try:
            with open(out, "wb") as fh:
                fh.write(code + data)
        except OSError as e:
            messagebox.showerror("Build", f"Cannot write {out}\n{e.strerror}")
            return None
        size = len(code) + len(data)
        self.bottom.select(1)
        self._console_write(f"Built {out}: {size} bytes (code {len(code)}, data {len(data)})\n", "info")
        self.set_status(f"Built kernel8.img, {size} bytes")
        if self.root_dir:
            self.refresh_tree()
        return out

    def run(self):
        if self.runner.running():
            return
        image = self.build()
        if image is None:
            return
        qemu = find_qemu(self.cfg)
        if qemu is None:
            messagebox.showinfo("QEMU needed", "Running needs QEMU (qemu-system-aarch64).\n"
                                "Install it, or choose its location now.")
            qemu = self.pick_qemu()
            if qemu is None:
                return
        self._console_write("Running in QEMU (Raspberry Pi 3B)\n", "info")
        try:
            self.runner.start(qemu, image)
        except OSError as e:
            self._console_write(f"Could not start QEMU: {e}\n", "err")
            return
        self.run_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self.console_entry.focus_set()

    def stop(self):
        if self.runner.running():
            self.runner.stop()

    def pick_qemu(self):
        path = filedialog.askopenfilename(title="Locate qemu-system-aarch64")
        if path:
            self.cfg["qemu"] = path
            save_config(self.cfg)
            return path
        return None

    def _send_input(self, event=None):
        text = self.console_entry.get()
        self.console_entry.delete(0, "end")
        self.runner.send(text + "\r")

    def _emit(self, kind, text):
        self.events.put((kind, text))

    def _poll_events(self):
        try:
            while True:
                kind, text = self.events.get_nowait()
                if kind == "data":
                    self._console_write(text)
                elif kind == "log":
                    self._console_write(text, "err")
                elif kind == "exit":
                    self._console_write(f"\n[{text}]\n", "info")
                    self.run_btn.configure(state="normal")
                    self.stop_btn.configure(state="disabled")
        except queue.Empty:
            pass
        self._poll_job = self.after(50, self._poll_events)

    # --- project folder ---------------------------------------------------------------------------
    def _build_sidebar(self):
        self.sidebar = ttk.Frame(self.hpanes)
        self.sidebar_shown = False
        self._marked = []
        head = ttk.Frame(self.sidebar)
        head.pack(fill="x")
        self.folder_label = ttk.Label(head, text="", style="Status.TLabel")
        self.folder_label.pack(side="left")
        ttk.Button(head, text="Refresh", width=8, command=self.refresh_tree).pack(side="right", padx=4, pady=3)
        ttk.Button(head, text="New file", width=8, command=self.new_file).pack(side="right", pady=3)
        body = ttk.Frame(self.sidebar)
        body.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(body, show="tree", selectmode="browse")
        self.tree.column("#0", width=230, stretch=True)
        sb = ttk.Scrollbar(body, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.tree.tag_configure("dir", foreground=T["accent"])
        self.tree.tag_configure("lmc", foreground=T["fg"])
        self.tree.tag_configure("other", foreground=T["info"])
        self.tree.tag_configure("main", foreground=T["label_def"], font=self.bold_font)
        self.tree.bind("<<TreeviewOpen>>", self._tree_open)
        self.tree.bind("<Double-1>", self._tree_activate)
        self.tree.bind("<Return>", self._tree_activate)
        for seq in ("<Button-3>", "<Button-2>", "<Control-Button-1>"):
            self.tree.bind(seq, self._tree_menu)
        menu = tk.Menu(self, tearoff=0, bg=T["panel"], fg=T["fg"], activebackground=T["select"],
                       activeforeground=T["fg"])
        menu.add_command(label="Open", command=self._tree_activate)
        menu.add_separator()
        menu.add_command(label="New file...", command=self.new_file)
        menu.add_command(label="New folder...", command=self.new_folder)
        menu.add_command(label="Rename...", command=self.rename_item)
        menu.add_command(label="Delete...", command=self.delete_item)
        menu.add_separator()
        menu.add_command(label="Use as the main file", command=self.set_main_selected)
        menu.add_command(label="Refresh", command=self.refresh_tree)
        self.tree_context = menu

    def show_sidebar(self, show=True):
        if show and not self.sidebar_shown:
            self.hpanes.insert(0, self.sidebar, weight=0)
            self.sidebar_shown = True
            self.after_idle(self._set_sash)
        elif not show and self.sidebar_shown:
            self.hpanes.forget(self.sidebar)
            self.sidebar_shown = False

    def _set_sash(self):
        try:
            self.hpanes.sashpos(0, 260)
        except tk.TclError:
            pass

    def toggle_sidebar(self):
        if self.sidebar_shown:
            self.show_sidebar(False)
        elif self.root_dir:
            self.show_sidebar(True)
        else:
            self.open_folder_dialog()

    def open_folder_dialog(self):
        start = self.cfg.get("last_folder") or self.cfg.get("last_dir") or os.getcwd()
        path = filedialog.askdirectory(initialdir=start, title="Open project folder", mustexist=True)
        if path:
            self.open_folder(path)

    def open_folder(self, path):
        path = os.path.abspath(path)
        if not os.path.isdir(path):
            messagebox.showerror("Open folder", f"{path} is not a folder")
            return
        self.root_dir = path
        self.cfg["last_folder"] = path
        self.cfg["last_dir"] = path
        save_config(self.cfg)
        main = None
        rel = read_project(path).get("main")
        if rel:
            cand = os.path.normpath(os.path.join(path, rel))
            if os.path.isfile(cand):
                main = cand
        self.main_path = main or find_main(path)
        name = os.path.basename(path) or path
        self.title(f"LMC IDE - {name}")
        self.folder_label.configure(text=name)
        self.show_sidebar(True)
        self.tree.delete(*self.tree.get_children(""))
        self._populate("", path)
        self._mark_main()
        if self.main_path:
            self.open_path(self.main_path)
        self.update_main_label()
        self.schedule_check()
        self.set_status(f"Opened folder {path}")

    def close_folder(self):
        if not self.root_dir:
            return
        self.root_dir = None
        self.main_path = None
        self.cfg.pop("last_folder", None)
        save_config(self.cfg)
        self.title("LMC IDE")
        self.show_sidebar(False)
        for d in self.docs:
            self.maybe_adopt_main(d)
        self.update_main_label()
        self.schedule_check()

    def _populate(self, parent, path):
        self.tree.delete(*self.tree.get_children(parent))
        try:
            names = [n for n in os.listdir(path) if not is_hidden(n)]
        except OSError:
            names = []
        entries = [(n, os.path.join(path, n)) for n in names]
        dirs = sorted((e for e in entries if os.path.isdir(e[1])), key=lambda e: e[0].lower())
        files = sorted((e for e in entries if not os.path.isdir(e[1])), key=lambda e: e[0].lower())
        for name, full in dirs:
            self.tree.insert(parent, "end", iid=full, text=name, tags=("dir",))
            try:
                has_children = any(not is_hidden(n) for n in os.listdir(full))
            except OSError:
                has_children = False
            if has_children:
                self.tree.insert(full, "end", iid=full + "|", text="")   # placeholder, filled when opened
        for name, full in files:
            tag = "lmc" if name.lower().endswith(".lmc") else "other"
            self.tree.insert(parent, "end", iid=full, text=name, tags=(tag,))

    def _tree_open(self, event=None):
        iid = self.tree.focus()
        if iid and self.tree.get_children(iid) == (iid + "|",):
            self._populate(iid, iid)
            self._mark_main()

    def _all_items(self, parent=""):
        for iid in self.tree.get_children(parent):
            yield iid
            yield from self._all_items(iid)

    def _reopen(self, expanded, parent=""):
        for iid in self.tree.get_children(parent):
            if iid in expanded and os.path.isdir(iid):
                self._populate(iid, iid)
                self.tree.item(iid, open=True)
                self._reopen(expanded, iid)

    def refresh_tree(self):
        if not self.root_dir:
            return
        expanded = {i for i in self._all_items() if self.tree.item(i, "open")}
        selected = self.tree.selection()
        self.tree.delete(*self.tree.get_children(""))
        self._populate("", self.root_dir)
        self._reopen(expanded)
        self._marked = []
        self._mark_main()
        if selected and self.tree.exists(selected[0]):
            self.tree.selection_set(selected[0])

    def _mark_main(self):
        if not self.root_dir:
            return
        for iid in self._marked:
            if self.tree.exists(iid):
                tag = "lmc" if iid.lower().endswith(".lmc") else "other"
                self.tree.item(iid, text=os.path.basename(iid), tags=(tag,))
        self._marked = []
        if self.main_path and self.tree.exists(self.main_path):
            self.tree.item(self.main_path, text=os.path.basename(self.main_path) + "  (main)", tags=("main",))
            self._marked = [self.main_path]

    def _selected_path(self):
        sel = self.tree.selection()
        if sel and not sel[0].endswith("|"):
            return sel[0]
        return None

    def _tree_menu(self, event):
        iid = self.tree.identify_row(event.y)
        if iid:
            self.tree.selection_set(iid)
        else:
            self.tree.selection_remove(*self.tree.selection())
        try:
            self.tree_context.tk_popup(event.x_root, event.y_root)
        finally:
            self.tree_context.grab_release()

    def _tree_activate(self, event=None):
        path = self._selected_path()
        if path and os.path.isfile(path):
            if looks_like_text(path):
                self.open_path(path)
            else:
                messagebox.showinfo("Open", f"{os.path.basename(path)} is not a text file, so it can't be edited here.")

    def _target_dir(self):
        path = self._selected_path()
        if path is None:
            return self.root_dir
        return path if os.path.isdir(path) else os.path.dirname(path)

    def _ask_name(self, title, initial=""):
        name = simpledialog.askstring(title, "Name:", initialvalue=initial, parent=self)
        if not name:
            return None
        name = name.strip()
        if not name or name in (".", "..") or any(ch in name for ch in '/\\:*?"<>|'):
            messagebox.showerror(title, "That is not a valid name.")
            return None
        return name

    def _reveal(self, path):
        if not self.root_dir or not self._inside(path, self.root_dir):
            return
        rel = os.path.relpath(os.path.dirname(path), self.root_dir)
        cur = self.root_dir
        if rel != ".":
            for part in rel.split(os.sep):
                cur = os.path.join(cur, part)
                if self.tree.exists(cur):
                    if self.tree.get_children(cur) == (cur + "|",):
                        self._populate(cur, cur)
                    self.tree.item(cur, open=True)
        self._mark_main()
        if self.tree.exists(path):
            self.tree.selection_set(path)
            self.tree.see(path)

    def new_file(self):
        if not self.root_dir:
            self.new_doc()
            return
        name = self._ask_name("New file")
        if not name:
            return
        if "." not in name:
            name += ".lmc"
        path = os.path.join(self._target_dir(), name)
        if os.path.exists(path):
            messagebox.showerror("New file", f"{name} already exists.")
            return
        try:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("")
        except OSError as e:
            messagebox.showerror("New file", f"Cannot create {path}\n{e.strerror}")
            return
        self.refresh_tree()
        self._reveal(path)
        self.open_path(path)

    def new_folder(self):
        if not self.root_dir:
            return
        name = self._ask_name("New folder")
        if not name:
            return
        path = os.path.join(self._target_dir(), name)
        try:
            os.makedirs(path)
        except OSError as e:
            messagebox.showerror("New folder", f"Cannot create {path}\n{e.strerror}")
            return
        self.refresh_tree()
        self._reveal(path)

    def rename_item(self):
        old = self._selected_path()
        if not old:
            return
        name = self._ask_name("Rename", os.path.basename(old))
        if not name or name == os.path.basename(old):
            return
        new = os.path.join(os.path.dirname(old), name)
        if os.path.exists(new):
            messagebox.showerror("Rename", f"{name} already exists.")
            return
        try:
            os.rename(old, new)
        except OSError as e:
            messagebox.showerror("Rename", f"Cannot rename\n{e.strerror}")
            return

        def remap(p):
            return new + p[len(old):] if p == old or p.startswith(old + os.sep) else p

        for d in self.docs:
            if d.path:
                d.path = remap(d.path)
                self.update_tab_title(d)
        if self.main_path:
            moved = remap(self.main_path)
            if moved != self.main_path:
                self.set_main(moved)
        self.refresh_tree()
        self._reveal(new)
        self.update_main_label()
        self.schedule_check()

    def delete_item(self):
        path = self._selected_path()
        if not path:
            return
        what = "the folder and everything in it" if os.path.isdir(path) else "the file"
        if not messagebox.askyesno("Delete", f"Delete {what} '{os.path.basename(path)}'?\n"
                                   "This cannot be undone.", icon="warning", parent=self):
            return
        for d in list(self.docs):
            if d.path and (d.path == path or d.path.startswith(path + os.sep)):
                self._remove_doc(d)
        try:
            shutil.rmtree(path) if os.path.isdir(path) else os.remove(path)
        except OSError as e:
            messagebox.showerror("Delete", f"Cannot delete\n{e.strerror}")
        if self.main_path and (self.main_path == path or self.main_path.startswith(path + os.sep)):
            self.main_path = None
        self.refresh_tree()
        self.update_main_label()
        self.schedule_check()

    def set_main_selected(self):
        path = self._selected_path()
        if path and os.path.isfile(path):
            self.set_main(path)

    # --- extra windows -----------------------------------------------------------------------------
    def show_listing(self):
        result = self._compile()
        if result is None:
            return
        a, code, data = result
        win = tk.Toplevel(self)
        win.title("Machine code listing")
        win.geometry("760x600")
        txt = tk.Text(win, bg=T["bg"], fg=T["fg"], font=self.font, wrap="none", relief="flat")
        sb = ttk.Scrollbar(win, command=txt.yview)
        txt.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        txt.pack(fill="both", expand=True)
        txt.insert("1.0", lmcc.make_listing(a, code, data))
        txt.configure(state="disabled")

    def show_help(self):
        win = tk.Toplevel(self)
        win.title("Language reference")
        win.geometry("640x640")
        txt = tk.Text(win, bg=T["bg"], fg=T["fg"], font=self.font, relief="flat", padx=12, pady=8)
        txt.pack(fill="both", expand=True)
        txt.insert("1.0", HELP_TEXT)
        txt.configure(state="disabled")


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    folder = next((a for a in args if os.path.isdir(a)), None)
    files = [a for a in args if not os.path.isdir(a)]
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)    # sharp text on high resolution screens
        except Exception:
            pass
    try:
        app = IDE(files, folder)
    except tk.TclError as e:
        print(f"Could not open the IDE window: {e}", file=sys.stderr)
        return 1
    except Exception:
        text = traceback.format_exc()
        try:
            with open(LOG_PATH, "a", encoding="utf-8") as fh:
                fh.write(time.strftime("%Y-%m-%d %H:%M:%S") + "\n" + text + "\n")
        except OSError:
            pass
        sys.stderr.write(text)
        try:
            root = tk.Tk()
            root.withdraw()
            messagebox.showerror("LMC IDE could not start", text[-1500:])
        except Exception:
            pass
        return 1
    app.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
