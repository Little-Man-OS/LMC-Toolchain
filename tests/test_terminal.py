import os
import sys
import tempfile
import threading
import time
import unittest

from lmc import terminal

POSIX = os.name != "nt"


class Collector:
    def __init__(self, cwd, shell=None):
        self.text, self.codes = "", []
        self.done = threading.Event()
        self.session = terminal.Session(cwd, shell, self._out, self._exit)

    def _out(self, t):
        self.text += t

    def _exit(self, code):
        self.codes.append(code)
        self.done.set()

    def run(self, command, timeout=8):
        self.done.clear()
        self.text = ""
        self.session.run(command)
        assert self.done.wait(timeout), f"timed out running {command!r}, output so far: {self.text!r}"
        return self.text, self.codes[-1]


class Helpers(unittest.TestCase):
    def test_strip_ansi(self):
        self.assertEqual(terminal.strip_ansi("\x1b[31mred\x1b[0m plain \x1b]0;title\x07done"), "red plain done")

    def test_split_held_keeps_possible_marker_start(self):
        self.assertEqual(terminal.split_held("hello __LM", "__LMC_X__"), ("hello ", "__LM"))
        self.assertEqual(terminal.split_held("nothing here", "__LMC_X__"), ("nothing here", ""))

    def test_shell_kind(self):
        self.assertEqual(terminal.shell_kind("/usr/bin/bash"), "posix")
        self.assertEqual(terminal.shell_kind(r"C:\Windows\System32\cmd.exe"), "cmd")
        self.assertEqual(terminal.shell_kind("pwsh"), "powershell")

    def test_simple_cd_detection(self):
        self.assertEqual(terminal._simple_cd("cd src"), "src")
        self.assertEqual(terminal._simple_cd("cd /d C:\\x"), "C:\\x")
        self.assertEqual(terminal._simple_cd("cd"), "")
        self.assertIsNone(terminal._simple_cd("cd src && make"))
        self.assertIsNone(terminal._simple_cd("echo hi"))


@unittest.skipUnless(POSIX, "these use a POSIX shell")
class Commands(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = os.path.realpath(self.tmp.name)
        os.makedirs(os.path.join(self.dir, "sub"))
        self.t = Collector(self.dir, "/bin/bash")
        self.addCleanup(self.tmp.cleanup)

    def test_output_and_exit_code(self):
        out, code = self.t.run("echo hello; echo oops 1>&2; exit 3")
        self.assertIn("hello", out)
        self.assertIn("oops", out)                 # stderr is shown too
        self.assertEqual(code, 3)

    def test_marker_never_shown(self):
        out, _ = self.t.run("echo hi")
        self.assertEqual(out.strip(), "hi")
        self.assertNotIn("__LMC_CWD", out)

    def test_runs_in_current_folder(self):
        out, _ = self.t.run("pwd")
        self.assertEqual(out.strip(), self.dir)

    def test_cd_carries_to_next_command(self):
        self.t.run("cd sub")
        self.assertEqual(self.t.session.cwd, os.path.join(self.dir, "sub"))
        out, _ = self.t.run("pwd")
        self.assertEqual(out.strip(), os.path.join(self.dir, "sub"))

    def test_compound_cd(self):
        out, _ = self.t.run("cd sub && echo inside && pwd")
        self.assertIn("inside", out)
        self.assertEqual(self.t.session.cwd, os.path.join(self.dir, "sub"))

    def test_failed_command_keeps_folder(self):
        self.t.run("cd nonexistent_folder")
        self.assertEqual(self.t.session.cwd, self.dir)

    def test_output_without_trailing_newline(self):
        out, _ = self.t.run("printf abc")
        self.assertEqual(out, "abc")

    def test_stdin_reaches_the_program(self):
        self.t.done.clear()
        self.t.text = ""
        self.t.session.run("read name; echo got:$name")
        time.sleep(0.3)
        self.t.session.send("ellie")
        self.assertTrue(self.t.done.wait(5))
        self.assertIn("got:ellie", self.t.text)

    def test_kill_stops_a_long_command(self):
        self.t.done.clear()
        self.t.session.run("sleep 30")
        time.sleep(0.4)
        self.assertTrue(self.t.session.running)
        start = time.time()
        self.t.session.kill()
        self.assertTrue(self.t.done.wait(5))
        self.assertLess(time.time() - start, 4)
        self.assertFalse(self.t.session.running)

    def test_second_command_while_running_is_refused(self):
        self.t.done.clear()
        self.t.session.run("sleep 5")
        time.sleep(0.2)
        self.assertFalse(self.t.session.run("echo no"))
        self.t.session.kill()
        self.t.done.wait(5)

    def test_unicode_output(self):
        out, _ = self.t.run("echo 'caf\u00e9 \u2713'")
        self.assertIn("caf\u00e9 \u2713", out)

    def test_python_scripts_folder_is_on_path(self):
        out, _ = self.t.run("echo $PATH")
        self.assertIn(os.path.dirname(os.path.abspath(sys.executable)), out.strip().split(os.pathsep))

    def test_lmcc_command_works_even_when_not_installed(self):
        out, code = self.t.run("lmcc --version")
        self.assertEqual(code, 0, out)
        self.assertTrue(out.startswith("lmcc "), out)

    def test_marker_split_across_reads(self):
        # a slow writer that prints the end in pieces still must not leak the marker
        out, _ = self.t.run("printf 'one'; sleep 0.2; printf ' two\\n'; sleep 0.2; echo three")
        self.assertEqual(out.split(), ["one", "two", "three"])


if __name__ == "__main__":
    unittest.main()
