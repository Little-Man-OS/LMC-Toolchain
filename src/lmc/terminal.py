"""
Shell sessions for the IDE terminal. No Tk in here, so it can be tested without a display.

Each command you type runs in a fresh shell process, started in the terminal's current folder,
with output streamed back as it arrives. The folder is carried from one command to the next, so
`cd drivers && ls` leaves you in drivers. Things that live inside a single shell process, such
as exported variables and aliases, do not carry over to the next command.
"""
import codecs
import os
import re
import shutil
import signal
import subprocess
import sys
import sysconfig
import tempfile
import threading
import uuid

IS_WINDOWS = os.name == "nt"
CREATE_NO_WINDOW = 0x08000000
CREATE_NEW_PROCESS_GROUP = 0x00000200

_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[()][A-Za-z0-9]|\x1b[=>]")
POSIX_SHELLS = {"bash", "sh", "zsh", "dash", "ksh", "ash"}


def strip_ansi(text):
    """Remove colour and cursor control sequences."""
    return _ANSI.sub("", text)


def shell_kind(path):
    name = re.split(r"[\\/]", path)[-1].lower()
    for suffix in (".exe", ".com", ".bat", ".cmd"):
        if name.endswith(suffix):
            name = name[:-len(suffix)]
    if name in POSIX_SHELLS:
        return "posix"
    if name == "cmd":
        return "cmd"
    if name in ("powershell", "pwsh"):
        return "powershell"
    return "posix"


def detect_shell(cfg=None):
    """The shell to use: the one chosen in the IDE, else bash if there is a real one, else cmd."""
    chosen = (cfg or {}).get("shell")
    if chosen and os.path.isfile(chosen):
        return chosen
    if IS_WINDOWS:
        found = shutil.which("bash")
        if found and not any(x in found.lower() for x in ("\\system32\\", "\\windowsapps\\")):
            return found                      # an MSYS2 or Git bash, not the WSL launcher
        return os.environ.get("COMSPEC") or "cmd.exe"
    return os.environ.get("SHELL") or shutil.which("bash") or "/bin/sh"


def script_dirs():
    """Folders that hold lmcc and lmc-ide, so they work in the terminal even if not on PATH."""
    dirs = [os.path.dirname(os.path.abspath(sys.executable))]
    for scheme in (None, "nt_user", "posix_user"):
        try:
            d = sysconfig.get_path("scripts", scheme) if scheme else sysconfig.get_path("scripts")
        except (KeyError, ValueError):
            continue
        if d:
            dirs.append(d)
    seen, out = set(), []
    for d in dirs:
        if d and os.path.isdir(d) and d not in seen:
            seen.add(d)
            out.append(d)
    return out


def split_held(buf, marker):
    """Split buf into text that is safe to show and a tail that might be the start of marker."""
    for k in range(min(len(marker) - 1, len(buf)), 0, -1):
        if marker.startswith(buf[-k:]):
            return buf[:-k], buf[-k:]
    return buf, ""


def _simple_cd(command):
    """If command is just `cd`, `cd path` or `cd /d path`, return the path (empty for home)."""
    m = re.fullmatch(r"\s*(?:cd|chdir|Set-Location|sl)(?:\s+(?:/d\s+)?(.*?))?\s*", command, re.I)
    if not m or re.search(r"[;&|<>]", command):
        return None
    arg = (m.group(1) or "").strip()
    if len(arg) >= 2 and arg[0] == arg[-1] and arg[0] in "\"'":
        arg = arg[1:-1]
    return arg


class Session:
    """Runs one command at a time and reports output through callbacks.

    on_output(text) and on_exit(code) are called from a background thread.
    """

    def __init__(self, cwd, shell=None, on_output=None, on_exit=None):
        self.cwd = os.path.abspath(cwd)
        self.shell = shell or detect_shell()
        self.kind = shell_kind(self.shell)
        self.on_output = on_output or (lambda text: None)
        self.on_exit = on_exit or (lambda code: None)
        self.proc = None
        self.previous = self.cwd
        self.extra_env = {}          # for example LMC_INCLUDE, set by the IDE to the project folder
        self._tmp = None

    @property
    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def set_shell(self, shell):
        self.shell = shell
        self.kind = shell_kind(shell)

    def set_cwd(self, path):
        self.previous = self.cwd
        self.cwd = os.path.abspath(path)

    def _env(self):
        env = dict(os.environ)
        env["TERM"] = "dumb"
        env["NO_COLOR"] = "1"
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        env.update(self.extra_env)
        extra = [d for d in script_dirs()]
        if extra:
            env["PATH"] = os.pathsep.join(extra + [env.get("PATH", "")])
        # when the IDE is run from the source folder, `python -m lmc` should still work in the terminal
        parent = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        if os.path.basename(parent) not in ("site-packages", "dist-packages"):
            old = env.get("PYTHONPATH")
            env["PYTHONPATH"] = os.pathsep.join([parent] + ([old] if old else []))
        return env

    def _change_dir(self, arg):
        """Handles a plain cd for shells that cannot report their folder back to us."""
        if arg == "-":
            target = self.previous
        elif arg in ("", "~"):
            target = os.path.expanduser("~")
        else:
            target = os.path.expanduser(arg)
            if IS_WINDOWS and re.match(r"^/[a-zA-Z](/|$)", target):
                target = target[1].upper() + ":" + (target[2:] or "\\")
            if not os.path.isabs(target):
                target = os.path.join(self.cwd, target)
        target = os.path.normpath(target)
        if os.path.isdir(target):
            self.set_cwd(target)
            self.on_exit(0)
        else:
            self.on_output(f"cd: no such folder: {arg}\n")
            self.on_exit(1)

    def run(self, command):
        """Start a command. Returns False if one is already running."""
        if self.running:
            return False
        if not command.strip():
            self.on_exit(0)
            return True
        if self.kind != "posix":
            arg = _simple_cd(command)
            if arg is not None:
                self._change_dir(arg)
                return True
        marker = None
        flags = CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP if IS_WINDOWS else 0
        kwargs = dict(cwd=self.cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                      stderr=subprocess.STDOUT, env=self._env(), bufsize=0)
        if IS_WINDOWS:
            kwargs["creationflags"] = flags
        else:
            kwargs["start_new_session"] = True
        try:
            if self.kind == "posix":
                marker = f"__LMC_CWD_{uuid.uuid4().hex}__"
                self._tmp = tempfile.mkdtemp(prefix="lmc-term-")
                script = os.path.join(self._tmp, "cmd.sh")
                python = sys.executable.replace("'", "'\\''")
                with open(script, "w", encoding="utf-8", newline="\n") as fh:
                    # lmcc works even if pip's scripts folder is not on PATH
                    fh.write(f"command -v lmcc >/dev/null 2>&1 || lmcc() {{ '{python}' -m lmc \"$@\"; }}\n")
                    fh.write(f"{command}\n__lmc_rc=$?\n"
                             f"printf '%s%s\\n' '{marker}' \"$(pwd -W 2>/dev/null || pwd)\"\n"
                             "exit $__lmc_rc\n")
                if IS_WINDOWS:
                    script = script.replace("\\", "/")      # MSYS2 and Git bash like forward slashes
                self.proc = subprocess.Popen([self.shell, script], **kwargs)
            elif self.kind == "cmd":
                line = f'"{self.shell}" /d /s /c "chcp 65001>nul & {command}"'
                self.proc = subprocess.Popen(line, **kwargs)
            else:
                self.proc = subprocess.Popen([self.shell, "-NoProfile", "-Command", command], **kwargs)
        except OSError as e:
            self.proc = None
            self._cleanup()
            self.on_output(f"Could not start {self.shell}: {e}\n")
            self.on_exit(127)
            return True
        threading.Thread(target=self._read, args=(self.proc, marker), daemon=True).start()
        return True

    def _read(self, proc, marker):
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        held, after, newcwd = "", False, ""
        while True:
            try:
                chunk = proc.stdout.read(4096)
            except (OSError, ValueError):
                break
            if not chunk:
                break
            text = held + decoder.decode(chunk)
            held = ""
            if marker:
                out = []
                while text:
                    if after:                              # reading the folder after the marker
                        end = text.find("\n")
                        if end < 0:
                            newcwd += text
                            text = ""
                        else:
                            newcwd += text[:end]
                            text = text[end + 1:]
                            after = False
                        continue
                    at = text.find(marker)
                    if at >= 0:
                        out.append(text[:at])
                        text = text[at + len(marker):]
                        after = True
                        continue
                    safe, held = split_held(text, marker)
                    out.append(safe)
                    text = ""
                text = "".join(out)
            if text:
                self.on_output(text)
        tail = held + decoder.decode(b"", final=True)
        if tail and not after:
            self.on_output(tail)
        code = proc.wait()
        for pipe in (proc.stdout, proc.stdin):
            try:
                pipe.close()
            except (OSError, ValueError):
                pass
        if newcwd.strip():
            self._adopt_cwd(newcwd.strip())
        self.proc = None
        self._cleanup()
        self.on_exit(code)

    def _adopt_cwd(self, path):
        if IS_WINDOWS and re.match(r"^/[a-zA-Z](/|$)", path):
            path = path[1].upper() + ":" + (path[2:] or "\\")
        if os.path.isdir(path):
            self.set_cwd(path)

    def _cleanup(self):
        if self._tmp:
            shutil.rmtree(self._tmp, ignore_errors=True)
            self._tmp = None

    def send(self, line):
        """Type a line into the running program."""
        if self.running:
            try:
                self.proc.stdin.write((line + "\n").encode("utf-8"))
                self.proc.stdin.flush()
            except (OSError, ValueError):
                pass

    def close_stdin(self):
        if self.running:
            try:
                self.proc.stdin.close()
            except (OSError, ValueError):
                pass

    def kill(self):
        """Stop the running command and anything it started."""
        proc = self.proc
        if proc is None or proc.poll() is not None:
            return
        if IS_WINDOWS:
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           creationflags=CREATE_NO_WINDOW)
        else:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except (OSError, ProcessLookupError):
                pass
            threading.Timer(2.0, self._force_kill, args=(proc,)).start()

    @staticmethod
    def _force_kill(proc):
        if proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (OSError, ProcessLookupError):
                pass
