"""Shared-file synchronization in explicit, unrelated temporary Git checkouts."""
import json
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest


SOURCE = Path(__file__).parents[3] / "scripts/sync_apps.py"


class SyncAcceptance(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.hub = self.root / "tools/macos-app-tooling"
        (self.hub / "scripts").mkdir(parents=True)
        shutil.copy2(SOURCE, self.hub / "scripts/sync_apps.py")
        self.template = self.hub / "templates/macos-app"
        self.sources = {
            "scripts/app.py": "#!/usr/bin/env python3\nprint('shared')\n",
            "scripts/lib/signing.sh": "# shared signing\n",
            "ci.yml": "name: CI\n",
            "release.yml": "name: Release\n",
            "shared/MacAppDesign.swift": "// shared design\n",
            "shared/LocalizationResources.swift": "// shared localization\n",
        }
        for name, content in self.sources.items():
            path = self.template / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        (self.template / "scripts/app.py").chmod(0o755)
        subprocess.run(["git", "init", "-q", str(self.hub)], check=True)
        self.git(self.hub, "add", "templates")
        self.repo = self.checkout("projects/unrelated/app")

    def tearDown(self):
        self.temporary.cleanup()

    def git(self, repo, *args):
        return subprocess.check_output(["git", "-C", str(repo), *args])

    def checkout(self, name):
        repo = self.root / name
        repo.mkdir(parents=True)
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        config = {"schemaVersion": 1, "product": "ExampleApp", "buildSystem": "swiftpm",
                  "shared": {"designDestination": "Sources/App/MacAppDesign.swift",
                             "localizationDestination": "Sources/Core/LocalizationResources.swift"}}
        (repo / "macos-app.json").write_text(json.dumps(config))
        (repo / "Own.swift").write_text("// retained product source\n")
        return repo

    def run_sync(self, *args):
        return subprocess.run([sys.executable, "-B", str(self.hub / "scripts/sync_apps.py"), *map(str, args)],
                              cwd=self.root, capture_output=True, text=True)

    def assert_code(self, code, *args):
        result = self.run_sync(*args)
        self.assertEqual(result.returncode, code, result.stdout + result.stderr)
        return result

    def snapshot(self, repo):
        return {p.relative_to(repo).as_posix(): (p.read_bytes(), stat.S_IMODE(p.stat().st_mode))
                for p in repo.rglob("*") if p.is_file() and ".git" not in p.relative_to(repo).parts}

    def change_config(self, **values):
        path = self.repo / "macos-app.json"
        config = json.loads(path.read_text())
        config.update(values)
        path.write_text(json.dumps(config))

    def test_explicit_checkout_is_required(self):
        self.assert_code(2, "--check")
        self.assertFalse((self.hub / "catalog").exists())

    def test_check_and_apply_work_without_catalog_or_neighbor_layout(self):
        before = self.snapshot(self.repo)
        self.assert_code(1, "--check", self.repo)
        self.assertEqual(self.snapshot(self.repo), before)
        self.assert_code(0, "--apply", self.repo)
        self.assert_code(0, "--check", self.repo)
        self.assertEqual((self.repo / "scripts/app.py").read_bytes(),
                         (self.template / "scripts/app.py").read_bytes())
        self.assertEqual(stat.S_IMODE((self.repo / "scripts/app.py").stat().st_mode), 0o755)
        self.assertEqual((self.repo / "Sources/Core/LocalizationResources.swift").read_text(),
                         self.sources["shared/LocalizationResources.swift"])
        self.assertEqual((self.repo / ".github/workflows/ci.yml").read_text(), self.sources["ci.yml"])
        self.assertEqual((self.repo / "Own.swift").read_text(), "// retained product source\n")
        self.assertFalse((self.hub / "catalog").exists())

    def test_execution_mode_drift_is_detected_and_repaired(self):
        self.assert_code(0, "--apply", self.repo)
        target = self.repo / "scripts/app.py"
        target.chmod(0o644)
        self.assert_code(1, "--check", self.repo)
        self.assert_code(0, "--apply", self.repo)
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o755)

    def test_untracked_template_files_are_not_copied(self):
        (self.template / "scripts/local.env").write_text("LOCAL_ONLY=fixture\n")
        cache = self.template / "scripts/__pycache__"
        cache.mkdir()
        (cache / "local.pyc").write_bytes(b"local cache")
        self.assert_code(0, "--apply", self.repo)
        self.assertFalse((self.repo / "scripts/local.env").exists())
        self.assertFalse((self.repo / "scripts/__pycache__").exists())

    def test_unrelated_staged_content_and_product_tools_are_preserved(self):
        tools = self.repo / "scripts/product.py"
        tools.parent.mkdir()
        tools.write_text("# product tool\n")
        self.git(self.repo, "add", "Own.swift", "scripts/product.py")
        staged = self.git(self.repo, "diff", "--cached", "--binary")
        self.assert_code(0, "--apply", self.repo)
        self.assertEqual(self.git(self.repo, "diff", "--cached", "--binary"), staged)
        self.assertEqual(tools.read_text(), "# product tool\n")

    def test_all_targets_are_preflighted_before_any_apply(self):
        second = self.checkout("another/location/app")
        self.assert_code(0, "--apply", self.repo, second)
        changed = second / "scripts/app.py"
        changed.write_text("# staged app change\n")
        self.git(second, "add", "scripts/app.py")
        (self.template / "scripts/app.py").write_text("# new shared version\n")
        before = self.snapshot(self.repo)
        staged = self.git(second, "diff", "--cached", "--binary")
        self.assert_code(2, "--apply", self.repo, second)
        self.assertEqual(self.snapshot(self.repo), before)
        self.assertEqual(self.git(second, "diff", "--cached", "--binary"), staged)
        self.assertEqual(changed.read_text(), "# staged app change\n")

    def test_duplicate_checkout_and_nonroot_paths_are_rejected(self):
        self.assert_code(2, "--apply", self.repo, self.repo)
        child = self.repo / "subdirectory"
        child.mkdir()
        self.assert_code(2, "--apply", child)
        self.assertFalse((self.repo / "scripts").exists())

    def test_symlink_checkouts_config_and_destination_parents_are_rejected(self):
        link = self.root / "checkout-link"
        link.symlink_to(self.repo, target_is_directory=True)
        self.assert_code(2, "--apply", link)
        config = self.repo / "macos-app.json"
        outside = self.root / "external.json"
        config.rename(outside)
        config.symlink_to(outside)
        self.assert_code(2, "--apply", self.repo)
        config.unlink()
        outside.rename(config)
        directory = self.root / "external-source"
        directory.mkdir()
        (self.repo / "Sources").symlink_to(directory, target_is_directory=True)
        self.assert_code(2, "--apply", self.repo)
        self.assertEqual(list(directory.iterdir()), [])

    def test_invalid_shared_destinations_are_rejected_without_writes(self):
        for shared in [None, [], {"path": "../app"},
                       {"designDestination": "../outside.swift"},
                       {"localizationDestination": "/absolute.swift"},
                       {"designDestination": ".git/config"},
                       {"designDestination": "Sources/Same.swift", "localizationDestination": "Sources/Same.swift"}]:
            with self.subTest(shared=shared):
                self.change_config(shared=shared)
                before = self.snapshot(self.repo)
                self.assert_code(2, "--apply", self.repo)
                self.assertEqual(self.snapshot(self.repo), before)


if __name__ == "__main__":
    unittest.main()
