"""IDE tests. They need a display (on a headless Linux box run them under xvfb-run) and skip otherwise."""
import json
import os
import tempfile
import time
import unittest
from unittest import mock

try:
    import tkinter as tk
    from lmc import gui
    _root = tk.Tk()
    _root.destroy()
    HAVE_DISPLAY = True
except Exception:          # no Tk, or no display
    HAVE_DISPLAY = False


def pump(app, secs=0.7):
    end = time.time() + secs
    while time.time() < end:
        app.update()
        time.sleep(0.02)


def write(root, rel, text):
    path = os.path.join(root, *rel.split("/"))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        fh.write(text)
    return path


@unittest.skipUnless(HAVE_DISPLAY, "needs Tk and a display")
class ProjectTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = os.path.realpath(self.tmp.name)
        write(self.root, "kernel/main.lmc", 'IMP "drivers/uart"\nmain:\n        otc #72\n        bra done\ndone:\n        hlt\n')
        write(self.root, "drivers/uart.lmc", "; uart helpers\nbuf: CHA \"x\"\n")
        write(self.root, "notes.txt", "todo\n")
        with open(os.path.join(self.root, "blob.img"), "wb") as fh:
            fh.write(b"\0\1\2\3")
        self.home = tempfile.TemporaryDirectory()
        patcher = mock.patch.object(gui, "CONFIG_PATH", os.path.join(self.home.name, "cfg.json"))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.app = gui.IDE([], self.root)
        self.addCleanup(self.app.destroy)
        pump(self.app)

    def tearDown(self):
        self.tmp.cleanup()
        self.home.cleanup()

    def test_folder_opens_and_main_is_found(self):
        app = self.app
        self.assertTrue(app.sidebar_shown)
        self.assertEqual(app.main_path, os.path.join(self.root, "kernel", "main.lmc"))
        self.assertEqual([d.name for d in app.docs], ["main.lmc"])
        top = [app.tree.item(i, "text") for i in app.tree.get_children("")]
        self.assertEqual(top[:2], ["drivers", "kernel"])          # folders first
        self.assertIn("notes.txt", top)
        self.assertEqual(app.errors, [])                            # IMP resolved from the project root

    def test_hidden_files_are_not_listed(self):
        write(self.root, ".git/config", "x")
        self.app.refresh_tree()
        self.assertNotIn(".git", [self.app.tree.item(i, "text") for i in self.app.tree.get_children("")])

    def test_errors_in_imported_files_show_with_relative_names(self):
        write(self.root, "drivers/uart.lmc", "oops\n")
        self.app.run_check()
        rows = [self.app.problems.item(i, "values") for i in self.app.problems.get_children()]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], os.path.join("drivers", "uart.lmc"))

    def test_build_writes_image_in_project_root(self):
        out = self.app.build()
        self.assertEqual(out, os.path.join(self.root, "kernel8.img"))
        self.assertTrue(os.path.getsize(out) > 100)

    def test_set_main_is_remembered(self):
        other = write(self.root, "other.lmc", "main:\n hlt\n")
        self.app.set_main(other)
        with open(os.path.join(self.root, ".lmcproject")) as fh:
            self.assertEqual(json.load(fh)["main"], "other.lmc")
        self.app.close_folder()
        self.app.open_folder(self.root)
        self.assertEqual(self.app.main_path, other)

    def test_new_rename_delete(self):
        app = self.app
        with mock.patch.object(gui.simpledialog, "askstring", return_value="lib"):
            app.new_file()
        pump(app, 0.3)
        created = os.path.join(self.root, "lib.lmc")
        self.assertTrue(os.path.isfile(created))
        self.assertIn("lib.lmc", [d.name for d in app.docs])

        app.tree.selection_set(created)
        with mock.patch.object(gui.simpledialog, "askstring", return_value="lib2.lmc"):
            app.rename_item()
        renamed = os.path.join(self.root, "lib2.lmc")
        self.assertTrue(os.path.isfile(renamed) and not os.path.exists(created))
        self.assertIn("lib2.lmc", [d.name for d in app.docs])      # the open tab followed the rename

        app.tree.selection_set(renamed)
        with mock.patch.object(gui.messagebox, "askyesno", return_value=True):
            app.delete_item()
        self.assertFalse(os.path.exists(renamed))
        self.assertNotIn("lib2.lmc", [d.name for d in app.docs])

    def test_binary_file_is_not_opened(self):
        app = self.app
        app.tree.selection_set(os.path.join(self.root, "blob.img"))
        with mock.patch.object(gui.messagebox, "showinfo") as info:
            app._tree_activate()
        info.assert_called_once()
        self.assertNotIn("blob.img", [d.name for d in app.docs])

    def test_invalid_names_are_rejected(self):
        with mock.patch.object(gui.simpledialog, "askstring", return_value="a/b"), \
                mock.patch.object(gui.messagebox, "showerror") as err:
            self.assertIsNone(self.app._ask_name("x"))
        err.assert_called_once()

    def test_close_folder_hides_panel(self):
        self.app.close_folder()
        self.assertFalse(self.app.sidebar_shown)
        self.assertIsNone(self.app.root_dir)


if __name__ == "__main__":
    unittest.main()
