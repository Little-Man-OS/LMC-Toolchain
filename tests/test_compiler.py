import os
import shutil
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

    def test_slots_come_first(self):
        _, _, data = build("v: DAT 7\nmain:\n sta 2\n hlt\n")
        self.assertEqual(data[3 * 8:4 * 8], (7).to_bytes(8, "little"))

    def test_import(self):
        _, code, _ = build('IMP "lib"\nmain:\n bra helper\n',
                           extra={"lib.lmc": "helper:\n hlt\n"})
        self.assertTrue(code)


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
            "main:\n lda 70000\n": "too large",
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
    def run_image(self, name, timeout=3):
        a, code, data = compiler.compile_file(os.path.join(EXAMPLES, name))
        with tempfile.TemporaryDirectory() as d:
            img, out = os.path.join(d, "k.img"), os.path.join(d, "out.txt")
            with open(img, "wb") as fh:
                fh.write(code + data)
            p = subprocess.Popen(["qemu-system-aarch64", "-M", "raspi3b", "-kernel", img, "-display", "none",
                                  "-serial", "null", "-serial", "file:" + out],
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            time.sleep(timeout)
            p.kill()
            p.wait()
            with open(out) as fh:
                return fh.read().replace("\r", "")

    def test_hello(self):
        self.assertEqual(self.run_image("hello.lmc"),
                         "Hello from LMC on bare metal!\n5\n4\n3\n2\n1\nDone\n")


if __name__ == "__main__":
    unittest.main()
