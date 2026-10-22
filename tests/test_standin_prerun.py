"""The stand-in pre-run helper: paid once, untimed, with no side effects."""
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from seren_support import prerun, standin


class StandinPrerunTests(unittest.TestCase):
    def write(self, text):
        path = Path(tempfile.mkdtemp()) / "standin"
        path.write_text(standin(text))
        path.chmod(0o755)
        return path

    def test_python_stand_in_pre_run_has_no_side_effects(self):
        marker = Path(tempfile.mkdtemp()) / "ran"
        path = self.write("#!" + sys.executable + " -I\nopen(%r, 'w').write('x')\n"
                          % str(marker))
        prerun(path)
        self.assertFalse(marker.exists())
        subprocess.run([str(path)], check=True)  # a normal run still does its work
        self.assertTrue(marker.exists())

    def test_shell_stand_in_pre_run_exits_cleanly(self):
        path = self.write("#!/bin/sh\nexit 99\n")
        prerun(path)  # check=True: the guard exits 0 before the stand-in's own exit 99
        self.assertEqual(99, subprocess.run([str(path)]).returncode)

    def test_a_stand_in_needs_a_shebang(self):
        with self.assertRaises(ValueError):
            standin("print('no shebang')\n")


if __name__ == "__main__":
    unittest.main()
