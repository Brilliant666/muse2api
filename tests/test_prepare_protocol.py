"""Real Git regression for archive byte fidelity on Windows and Unix."""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("prepare_protocol", ROOT / "tools" / "prepare_protocol.py")
prepare_protocol = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(prepare_protocol)


class GitArchiveBytesTests(unittest.TestCase):
    def test_archive_preserves_raw_lf_and_crlf_despite_checkout_conversion(self):
        with tempfile.TemporaryDirectory(prefix="muse-prepare-eol-") as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()

            def git(*arguments):
                return subprocess.check_output(
                    ["git", *arguments], cwd=source,
                    env=prepare_protocol.git_environment(), stderr=subprocess.PIPE)

            git("init", "-q")
            originals = {"lf.txt": b"first\nsecond\n", "crlf.txt": b"first\r\nsecond\r\n"}
            for name, content in originals.items():
                (source / name).write_bytes(content)
            git("-c", "core.autocrlf=false", "add", "--", *originals)
            git("-c", "user.name=Preparation Test", "-c", "user.email=prepare@example.invalid",
                "commit", "-qm", "raw fixture bytes")
            commit = git("rev-parse", "HEAD").decode("ascii").strip()

            # Reproduce a normal Windows checkout setting without touching
            # global Git configuration. A dirty checkout must not affect the
            # fixed source object either.
            git("config", "core.autocrlf", "true")
            git("config", "core.eol", "crlf")
            (source / "lf.txt").write_bytes(b"dirty local content\r\n")
            output = root / "output"
            prepare_protocol.archive_fixed(source, commit, output)
            for name, expected in originals.items():
                with self.subTest(path=name):
                    self.assertEqual(git("cat-file", "blob", commit + ":" + name), expected)
                    self.assertEqual((output / name).read_bytes(), expected)
            self.assertEqual((source / "lf.txt").read_bytes(), b"dirty local content\r\n")

    def test_patch_output_ignores_global_checkout_conversion(self):
        with tempfile.TemporaryDirectory(prefix="muse-prepare-apply-eol-") as temporary:
            root = Path(temporary)
            output = root / "output"
            output.mkdir()
            (output / "module.py").write_bytes(b"before\n")
            patch_path = root / "change.patch"
            patch_path.write_bytes(b"diff --git a/module.py b/module.py\n"
                                   b"--- a/module.py\n+++ b/module.py\n"
                                   b"@@ -1 +1 @@\n-before\n+after\n")
            settings = root / "gitconfig"
            settings.write_text("[core]\n\tautocrlf = true\n\teol = crlf\n", encoding="utf-8")
            with patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": str(settings),
                                         "GIT_CONFIG_NOSYSTEM": "1"}):
                prepare_protocol.run_git(["apply", "--check", str(patch_path)],
                                         cwd=output, isolated_apply=True)
                prepare_protocol.run_git(["apply", str(patch_path)],
                                         cwd=output, isolated_apply=True)
            self.assertEqual((output / "module.py").read_bytes(), b"after\n")


if __name__ == "__main__":
    unittest.main()
