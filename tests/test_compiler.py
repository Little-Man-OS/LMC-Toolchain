import os
import shutil
import socket
import subprocess
import tempfile
import time
import unittest

from lmc import compiler

EXAMPLES = os.path.join(os.path.dirname(compiler.__file__), "examples")


def build(source, name="prog.lmc", extra=None):
    """Compile source text in a temp folder, returns (asm, code, data)."""
    with tempfile.TemporaryDirectory() as d:
        for fname, text in (extra or {}).items():
            with open(os.path.join(d, fname), "w") as fh:
                fh.write(text)
        path = os.path.join(d, name)
        with open(path, "w") as fh:
            fh.write(source)
        return compiler.compile_file(path)


def errors_for(source, **kw):
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "prog.lmc")
        with open(path, "w") as fh:
            fh.write(source)
        errs, _ = compiler.check(path, **kw)
        return [(e.loc.line if e.loc else None, e.msg) for e in errs]


class Compile(unittest.TestCase):
    def test_examples_compile(self):
        for name in os.listdir(EXAMPLES):
            if name.endswith(".lmc"):
                a, code, data = compiler.compile_file(os.path.join(EXAMPLES, name))
                self.assertEqual(len(code) % 16, 0, "data area must start 16 byte aligned")
                self.assertEqual(len(data) % 8, 0)

    def test_same_input_same_output(self):
        src = "main:\n lda #1\n out\n hlt\n"
        self.assertEqual(build(src)[1:], build(src)[1:])

    def test_forward_and_backward_branches(self):
        build("main:\n bra later\n hlt\nback:\n hlt\nlater:\n bra back\n")

    def test_data_alignment(self):
        _, _, data = build("c: CHA 'x'\ni: INT 5\nd: DAT 9\nmain:\n hlt\n")
        self.assertEqual(data[0:1], b"x")
        self.assertEqual(data[4:8], (5).to_bytes(4, "little"))
        self.assertEqual(data[8:16], (9).to_bytes(8, "little"))

    def test_no_mailbox_area(self):
        _, _, data = build("v: DAT 7\nmain:\n hlt\n")
        self.assertEqual(data, (7).to_bytes(8, "little"))      # just the named data, nothing before it

    def test_labels_with_and_without_colons(self):
        with_colons = build("main:\n lda x\n bra end\nend:\n out\n hlt\nx: DAT 4\n")
        without = build("main LDA x\n BRA end\nend OUT\n HLT\nx DAT 4\n")
        self.assertEqual(with_colons[1:], without[1:])

    def test_higginson_alternative_names(self):
        a = build("main:\n sta x\n hlt\nx: DAT\n")
        b = build("main:\n STO x\n COB\nx: DAT\n")
        self.assertEqual(a[1:], b[1:])

    def test_absolute_address_operands(self):
        build("main:\n lda 0x200000\n sta 16\n hlt\n")

    def test_import(self):
        _, code, _ = build('IMP "lib"\nmain:\n bra helper\n',
                           extra={"lib.lmc": "helper:\n hlt\n"})
        self.assertTrue(code)


class ProjectIncludes(unittest.TestCase):
    def make_project(self, d):
        os.makedirs(os.path.join(d, "kernel"))
        os.makedirs(os.path.join(d, "drivers"))
        with open(os.path.join(d, "drivers", "uart.lmc"), "w") as fh:
            fh.write("putc:\n hlt\n")
        with open(os.path.join(d, "kernel", "main.lmc"), "w") as fh:
            fh.write('IMP "drivers/uart"\nmain:\n bra putc\n')
        return os.path.join(d, "kernel", "main.lmc")

    def run_cli(self, argv, env=None):
        old = dict(os.environ)
        os.environ.pop("LMC_INCLUDE", None)
        os.environ.update(env or {})
        try:
            return compiler.main(argv)
        finally:
            os.environ.clear()
            os.environ.update(old)

    def test_import_from_project_root_needs_a_hint(self):
        with tempfile.TemporaryDirectory() as d:
            main = self.make_project(d)
            out = os.path.join(d, "o.img")
            self.assertEqual(self.run_cli([main, "-o", out]), 1)
            self.assertEqual(self.run_cli([main, "-o", out, "-I", d]), 0)
            self.assertEqual(self.run_cli([main, "-o", out], {"LMC_INCLUDE": d}), 0)

    def test_lmcproject_file_marks_the_project_root(self):
        with tempfile.TemporaryDirectory() as d:
            main = self.make_project(d)
            with open(os.path.join(d, ".lmcproject"), "w") as fh:
                fh.write("{}")
            self.assertEqual(self.run_cli([main, "-o", os.path.join(d, "o.img")]), 0)


class Errors(unittest.TestCase):
    def test_reports_every_error(self):
        errs = errors_for("main:\n lda nowhere\n frob 3\n add\n")
        self.assertEqual([line for line, _ in errs], [2, 3, 4])

    def test_messages(self):
        cases = {
            "main:\n bra x\nx: DAT 1\n": "is data",
            "main:\n sta #3\n": "immediate",
            "f: DUB 1.5\nmain:\n lda f\n": "floating point",
            "main:\n hlt\nmain:\n hlt\n": "already defined",
            "main:\n lda 5\n": "multiple of 8",
            "main:\n lda 0xFFFFFFFFFFFFFFF8\n": "too large",
            "main:\n imp nofile\n": "cannot find",
            "main:\n out 1 2\n": "at most one",
        }
        for src, fragment in cases.items():
            msgs = " ".join(m for _, m in errors_for(src))
            self.assertIn(fragment, msgs, src)

    def test_typo_after_label_does_not_hide_main(self):
        self.assertEqual(errors_for("main:\n oops\n"), [(2, "unknown instruction 'oops'")])

    def test_bad_data_value_does_not_cascade(self):
        errs = errors_for("x: INT banana\nmain:\n lda x\n hlt\n")
        self.assertEqual(len(errs), 1)

    def test_missing_main(self):
        self.assertIn("main", " ".join(m for _, m in errors_for("hlt\n")))
        self.assertEqual(errors_for("hlt\n", require_main=False), [])

    def test_unsaved_buffer_overrides_disk(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "a.lmc")
            with open(path, "w") as fh:
                fh.write("main:\n hlt\n")
            errs, _ = compiler.check(path, {path: "main:\n oops\n"})
            self.assertEqual(len(errs), 1)


@unittest.skipUnless(shutil.which("aarch64-linux-gnu-as") and shutil.which("aarch64-linux-gnu-ld")
                     and shutil.which("aarch64-linux-gnu-objcopy"), "GNU aarch64 binutils not installed")
class AgainstGnuAssembler(unittest.TestCase):
    def test_machine_code_matches_gnu_as(self):
        for name in os.listdir(EXAMPLES):
            if not name.endswith(".lmc"):
                continue
            a, code, data = compiler.compile_file(os.path.join(EXAMPLES, name))
            with tempfile.TemporaryDirectory() as d:
                s = os.path.join(d, "p.s")
                with open(s, "w") as fh:
                    fh.write(compiler.make_asm_source(a, data))
                subprocess.check_call(["aarch64-linux-gnu-as", s, "-o", s + ".o"])
                subprocess.check_call(["aarch64-linux-gnu-ld", "-Ttext=0x80000", "-o", s + ".elf", s + ".o"])
                subprocess.check_call(["aarch64-linux-gnu-objcopy", "-O", "binary", s + ".elf", s + ".bin"])
                with open(s + ".bin", "rb") as fh:
                    self.assertEqual(fh.read(), code + data, name)


@unittest.skipUnless(shutil.which("qemu-system-aarch64"), "QEMU not installed")
class RunsInQemu(unittest.TestCase):
    def run_source(self, source, inputs=(), seconds=2.5):
        """Build a program and run it on an emulated Pi, typing the given lines. Returns what it printed."""
        a, code, data = build(source)
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        with tempfile.TemporaryDirectory() as d:
            img = os.path.join(d, "k.img")
            with open(img, "wb") as fh:
                fh.write(code + data)
            p = subprocess.Popen(["qemu-system-aarch64", "-M", "raspi3b", "-kernel", img, "-display", "none",
                                  "-serial", "null", "-serial", f"tcp:127.0.0.1:{port},server"],
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                sock = None
                for _ in range(50):
                    try:
                        sock = socket.create_connection(("127.0.0.1", port), timeout=0.5)
                        break
                    except OSError:
                        time.sleep(0.1)
                self.assertIsNotNone(sock, "could not connect to QEMU")
                sock.settimeout(0.3)
                out = b""
                end = time.time() + seconds
                pending = list(inputs)
                next_input = time.time() + 0.6
                while time.time() < end:
                    if pending and time.time() >= next_input:
                        sock.sendall((pending.pop(0) + "\r").encode())
                        next_input = time.time() + 0.5
                    try:
                        out += sock.recv(4096)
                    except socket.timeout:
                        pass
                return out.decode().replace("\r", "")
            finally:
                p.kill()
                p.wait()

    def test_hello(self):
        with open(os.path.join(EXAMPLES, "hello.lmc")) as fh:
            out = self.run_source(fh.read())
        self.assertEqual(out, "Hello from LMC on bare metal!\n5\n4\n3\n2\n1\nDone\n")

    def test_higginson_program_with_input(self):
        with open(os.path.join(EXAMPLES, "higginson.lmc")) as fh:
            out = self.run_source(fh.read(), ["3", "10"])
        self.assertEqual(out.split(), ["3", "10", "7"])        # |3 - 10| = 7

    def test_add_example(self):
        with open(os.path.join(EXAMPLES, "add.lmc")) as fh:
            out = self.run_source(fh.read(), ["40", "-2"])
        self.assertEqual(out.split(), ["40", "-2", "38"])

    def test_store_and_load_at_a_real_address(self):
        out = self.run_source("main:\n lda #42\n sta 0x200000\n lda #0\n lda 0x200000\n out\n hlt\n")
        self.assertEqual(out.split(), ["42"])


if __name__ == "__main__":
    unittest.main()
