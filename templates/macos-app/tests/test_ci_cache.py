"""Input-cache behavior in real Git checkouts; no compiler or remote services."""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts/lib/release"))
import ci_prepare


@contextlib.contextmanager
def checkout():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td).resolve()
        subprocess.run(["git", "init", "-q", str(root)], check=True)
        (root / "Sources").mkdir()
        (root / "Sources/A.swift").write_bytes(b"let a = 1\n")
        (root / "Sources/B.swift").write_bytes(b"let b = 2\n")
        (root / ".gitignore").write_text(".build/\n")
        subprocess.run(["git", "-C", str(root), "add", "."], check=True)
        subprocess.run(["git", "-C", str(root), "-c", "user.name=Cache Test", "-c",
                        "user.email=cache@example.invalid", "commit", "-qm", "fixture"], check=True)
        previous = Path.cwd()
        os.chdir(root)
        try:
            yield root, root / ".build/app/debug/arm64"
        finally:
            os.chdir(previous)


def git_state(root):
    return tuple(subprocess.check_output(["git", "-C", str(root), *args]) for args in
                 (("status", "--porcelain=v1", "-z"), ("diff", "--binary"), ("diff", "--cached", "--binary")))


def touch(path):
    timestamp = path.stat().st_mtime_ns + 10_000_000_000
    os.utime(path, ns=(path.stat().st_atime_ns, timestamp))
    return timestamp


def invoke(action, cache):
    stream = io.StringIO()
    with contextlib.redirect_stdout(stream):
        result = ci_prepare.main(["cache-inputs", action, "--cache-dir", str(cache)])
    assert json.loads(stream.getvalue()) == result
    return result


def assets(root):
    paths = (root / "Resources/Assets.xcassets/App.imageset/Contents.json",
             root / "Resources/Dock.icon/Assets/icon.svg")
    for path in paths:
        path.parent.mkdir(parents=True)
        path.write_bytes(b"asset bytes\n")
    subprocess.run(["git", "add", "Resources"], check=True)
    directories = [path for path in (root / "Resources").rglob("*") if path.is_dir()]
    return paths, directories


class InputCacheTests(unittest.TestCase):
    def test_identical_asset_trees_restore_only_asset_directory_times(self):
        with checkout() as (root, cache):
            files, directories = assets(root)
            original = {path: path.stat().st_mtime_ns for path in directories}
            self.assertEqual(invoke("save", cache)["savedDirectories"], 4)
            for path in directories:
                touch(path)
            parent_time = touch(root / "Resources")
            source_time = root.stat().st_mtime_ns
            before = git_state(root)
            for path in (*files, *directories):
                info = path.stat()
                os.utime(path, ns=(info.st_atime_ns - 3_600_000_000_000, info.st_mtime_ns))
            atimes = {path: path.stat().st_atime_ns for path in (*files, *directories)}
            result = invoke("restore", cache)
            self.assertEqual(result["restoredDirectories"], 4)
            for path in directories:
                self.assertEqual(path.stat().st_mtime_ns, original[path])
                self.assertEqual(path.stat().st_atime_ns, atimes[path])
            for path in files:
                self.assertEqual(path.stat().st_atime_ns, atimes[path])
            self.assertEqual((root / "Resources").stat().st_mtime_ns, parent_time)
            self.assertEqual(root.stat().st_mtime_ns, source_time)
            self.assertEqual([path.read_bytes() for path in files], [b"asset bytes\n"] * 2)
            self.assertEqual(git_state(root), before)

    def test_asset_changes_preserve_entire_affected_tree_directory_times(self):
        for change in ("content", "file-mode", "directory-mode", "add", "delete", "rename", "untracked", "untrack", "empty-directory", "symlink", "directory-symlink"):
            with self.subTest(change=change), checkout() as (root, cache):
                files, directories = assets(root)
                invoke("save", cache)
                changed = files[0]
                if change == "content":
                    changed.write_bytes(b"changed asset bytes\n")
                elif change == "file-mode":
                    changed.chmod(0o755)
                elif change == "directory-mode":
                    changed.parent.chmod(0o700)
                elif change in ("add", "untracked"):
                    new = changed.parent / "new.json"
                    new.write_bytes(b"new asset\n")
                    if change == "add":
                        subprocess.run(["git", "add", str(new)], check=True)
                elif change == "delete":
                    changed.unlink()
                elif change == "rename":
                    subprocess.run(["git", "mv", str(changed), str(changed.with_name("renamed.json"))], check=True)
                elif change == "empty-directory":
                    (changed.parent / "Empty").mkdir()
                elif change == "untrack":
                    subprocess.run(["git", "rm", "--cached", str(changed)], check=True, stdout=subprocess.DEVNULL)
                elif change == "directory-symlink":
                    (changed.parent / "Linked").symlink_to(files[1].parent, target_is_directory=True)
                else:
                    changed.unlink()
                    changed.symlink_to(files[1])
                affected = [path for path in directories if "Assets.xcassets" in path.parts]
                fresh = {path: touch(path) for path in affected}
                unchanged = {path: path.stat().st_mtime_ns for path in directories if path not in affected}
                for path in unchanged:
                    touch(path)
                source = root / "Sources/A.swift"
                original_source_time = source.stat().st_mtime_ns
                touch(source)
                before = git_state(root)
                result = invoke("restore", cache)
                self.assertEqual(result["restoredDirectories"], 2)
                self.assertEqual(source.stat().st_mtime_ns, original_source_time)
                self.assertEqual({path: path.stat().st_mtime_ns for path in affected}, fresh)
                self.assertEqual({path: path.stat().st_mtime_ns for path in unchanged}, unchanged)
                self.assertEqual(git_state(root), before)
                if change in ("content", "file-mode", "directory-mode", "add", "rename"):
                    self.assertEqual(invoke("save", cache)["savedDirectories"], 4)
                    for path in fresh:
                        touch(path)
                    self.assertEqual(invoke("restore", cache)["restoredDirectories"], 4)
                    self.assertEqual({path: path.stat().st_mtime_ns for path in affected}, fresh)

    def test_asset_parent_symlink_cannot_restore_external_directory_times(self):
        for location in ("asset-root", "parent"):
            with self.subTest(location=location), checkout() as (root, cache), tempfile.TemporaryDirectory() as outside:
                files, directories = assets(root)
                invoke("save", cache)
                external = Path(outside) / "Assets.xcassets/App.imageset"
                external.mkdir(parents=True)
                (external / "Contents.json").write_bytes(files[0].read_bytes())
                removed_files = files if location == "parent" else files[:1]
                removed_dirs = directories if location == "parent" else [path for path in directories if "Assets.xcassets" in path.parts]
                for path in removed_files:
                    path.unlink()
                for path in sorted(removed_dirs, key=lambda path: len(path.parts), reverse=True):
                    path.rmdir()
                if location == "parent":
                    (root / "Resources").rmdir()
                    (root / "Resources").symlink_to(outside, target_is_directory=True)
                else:
                    (root / "Resources/Assets.xcassets").symlink_to(external.parent, target_is_directory=True)
                fresh = touch(external)
                result = invoke("restore", cache)
                count = 0 if location == "parent" else 2
                self.assertEqual(result["restoredDirectories"], count)
                self.assertEqual(external.stat().st_mtime_ns, fresh)
                self.assertEqual(invoke("save", cache)["savedDirectories"], count)

    def test_invalid_asset_metadata_rejects_all_file_and_directory_writes(self):
        with checkout() as (root, cache):
            _, directories = assets(root)
            invoke("save", cache)
            metadata = cache / ".source-inputs.json"
            valid = json.loads(metadata.read_text())
            mutations = ("root-escape", "nonasset-root", "member-escape", "duplicate-tree", "duplicate-member", "type", "mtime", "mode", "missing-root", "file-hash")
            for mutation in mutations:
                with self.subTest(mutation=mutation):
                    bad = json.loads(json.dumps(valid))
                    tree = bad["assetTrees"][0]
                    if mutation == "root-escape":
                        tree["path"] = "../Assets.xcassets"
                    elif mutation == "nonasset-root":
                        tree["path"] = "Sources"
                    elif mutation == "member-escape":
                        tree["members"][0]["path"] = "Sources/A.swift"
                    elif mutation == "duplicate-tree":
                        bad["assetTrees"].append(tree)
                    elif mutation == "duplicate-member":
                        tree["members"].append(tree["members"][0])
                    elif mutation == "type":
                        tree["members"][0]["type"] = "symlink"
                    elif mutation == "mtime":
                        tree["members"][0]["mtimeNs"] = True
                    elif mutation == "mode":
                        tree["members"][0]["mode"] = -1
                    elif mutation == "file-hash":
                        next(member for member in tree["members"] if member["type"] == "file")["sha256"] = "0" * 64
                    else:
                        tree["members"] = tree["members"][1:]
                    metadata.write_text(json.dumps(bad))
                    fresh = {path: touch(path) for path in directories}
                    source = root / "Sources/A.swift"
                    source_time = touch(source)
                    before = git_state(root)
                    self.assertEqual(invoke("restore", cache)["status"], "rejected")
                    self.assertEqual(source.stat().st_mtime_ns, source_time)
                    self.assertEqual({path: path.stat().st_mtime_ns for path in directories}, fresh)
                    self.assertEqual(git_state(root), before)

    def test_restore_same_bytes_preserves_contents_atime_and_git_state(self):
        with checkout() as (root, cache):
            source = root / "Sources/A.swift"
            original = source.stat().st_mtime_ns
            contents = source.read_bytes()
            self.assertEqual(invoke("save", cache)["saved"], 3)
            touch(source)
            before = git_state(root)
            atime = source.stat().st_atime_ns
            result = invoke("restore", cache)
            self.assertEqual(result["restored"], 3)
            self.assertEqual(source.stat().st_mtime_ns, original)
            self.assertEqual(source.stat().st_atime_ns, atime)
            self.assertEqual(source.read_bytes(), contents)
            self.assertEqual(git_state(root), before)

    def test_changed_mode_content_new_deleted_and_untracked_keep_current_mtime(self):
        with checkout() as (root, cache):
            invoke("save", cache)
            changed = root / "Sources/A.swift"
            changed.write_bytes(b"let a = 3\n")
            changed_time = touch(changed)
            (root / "Sources/B.swift").unlink()
            new = root / "Sources/New.swift"
            new.write_bytes(b"let n = 4\n")
            subprocess.run(["git", "add", "Sources"], check=True)
            subprocess.run(["git", "-c", "user.name=Cache Test", "-c", "user.email=cache@example.invalid",
                            "commit", "-qm", "next head"], check=True)
            new_time = touch(new)
            untracked = root / "private.txt"
            untracked.write_bytes(b"user data")
            untracked_time = touch(untracked)
            before = git_state(root)
            result = invoke("restore", cache)
            self.assertEqual(result["restored"], 1)
            self.assertEqual(changed.stat().st_mtime_ns, changed_time)
            self.assertEqual(new.stat().st_mtime_ns, new_time)
            self.assertEqual(untracked.stat().st_mtime_ns, untracked_time)
            self.assertEqual(git_state(root), before)
            self.assertEqual(invoke("save", cache)["saved"], 3)
            touch(changed)
            touch(new)
            invoke("restore", cache)
            self.assertEqual(changed.stat().st_mtime_ns, changed_time)
            self.assertEqual(new.stat().st_mtime_ns, new_time)
            changed.chmod(0o755)
            changed_time = touch(changed)
            invoke("restore", cache)
            self.assertEqual(changed.stat().st_mtime_ns, changed_time)

    def test_symlink_source_and_parent_never_touch_external_files(self):
        with checkout() as (root, cache), tempfile.TemporaryDirectory() as external:
            invoke("save", cache)
            outside = Path(external) / "A.swift"
            outside.write_bytes(b"let a = 1\n")
            outside_time = touch(outside)
            source = root / "Sources/A.swift"
            source.unlink()
            source.symlink_to(outside)
            before = git_state(root)
            invoke("restore", cache)
            self.assertEqual(outside.stat().st_mtime_ns, outside_time)
            self.assertEqual(git_state(root), before)
            self.assertEqual(invoke("save", cache)["saved"], 2)
            source.unlink()
            (root / "Sources/B.swift").unlink()
            (root / "Sources").rmdir()
            (root / "Sources").symlink_to(external, target_is_directory=True)
            invoke("restore", cache)
            self.assertEqual(outside.stat().st_mtime_ns, outside_time)
            self.assertEqual(invoke("save", cache)["saved"], 1)

    def test_missing_and_invalid_metadata_are_optional_safe_misses(self):
        with checkout() as (root, cache):
            self.assertEqual(invoke("restore", cache)["status"], "missing")
            self.assertFalse(cache.exists())
            invoke("save", cache)
            metadata = cache / ".source-inputs.json"
            valid = json.loads(metadata.read_text())
            invalid_cases = ["not json", {}, dict(valid, root="/different-checkout"),
                             dict(valid, schemaVersion=True), dict(valid, schemaVersion=1), dict(valid, files=[{}])]
            for field, value in (("mtimeNs", True), ("mtimeNs", -1), ("mtimeNs", 2**64),
                                 ("sha256", "invalid"), ("mode", True), ("mode", 0o10000)):
                invalid = json.loads(json.dumps(valid))
                invalid["files"][0][field] = value
                invalid_cases.append(invalid)
            for invalid in invalid_cases:
                with self.subTest(invalid=invalid):
                    source = root / "Sources/A.swift"
                    fresh = touch(source)
                    metadata.write_text(invalid if isinstance(invalid, str) else json.dumps(invalid))
                    before = git_state(root)
                    self.assertEqual(invoke("restore", cache)["status"], "rejected")
                    self.assertEqual(source.stat().st_mtime_ns, fresh)
                    self.assertEqual(git_state(root), before)

    def test_bad_paths_reject_entire_metadata_before_restoring_any_file(self):
        with checkout() as (root, cache), tempfile.TemporaryDirectory() as external:
            invoke("save", cache)
            metadata = cache / ".source-inputs.json"
            valid = json.loads(metadata.read_text())
            outside = Path(external) / "outside.swift"
            outside.write_bytes(b"let a = 1\n")
            outside_time = touch(outside)
            for bad_path in (str(outside), "../outside.swift", "Sources/../A.swift", ".", "", ".build/app/debug/arm64/.source-inputs.json"):
                with self.subTest(path=bad_path):
                    malformed = json.loads(json.dumps(valid))
                    malformed["files"].append(dict(valid["files"][0], path=bad_path))
                    metadata.write_text(json.dumps(malformed))
                    source = root / "Sources/A.swift"
                    fresh = touch(source)
                    self.assertEqual(invoke("restore", cache)["status"], "rejected")
                    self.assertEqual(source.stat().st_mtime_ns, fresh)
                    self.assertEqual(outside.stat().st_mtime_ns, outside_time)

    def test_valid_record_cannot_restore_an_untracked_file(self):
        with checkout() as (root, cache):
            invoke("save", cache)
            source = root / "Sources/A.swift"
            subprocess.run(["git", "rm", "--cached", "Sources/A.swift"], check=True)
            fresh = touch(source)
            before = git_state(root)
            invoke("restore", cache)
            self.assertEqual(source.stat().st_mtime_ns, fresh)
            self.assertEqual(git_state(root), before)

    def test_debug_and_release_paths_allowed_but_unsafe_cache_paths_fail(self):
        with checkout() as (root, cache), tempfile.TemporaryDirectory() as external:
            for allowed in (cache, root / ".build/app/release", Path(".build/app/debug/x86_64")):
                with self.subTest(cache=allowed):
                    self.assertEqual(invoke("save", allowed)["saved"], 3)
            for invalid in (root, root / ".build/app", root / "dist/release", Path(".build/app/../elsewhere"), Path(external)):
                with self.subTest(cache=invalid), self.assertRaises(ValueError):
                    invoke("restore", invalid)
            (root / ".build/app/linked").symlink_to(external, target_is_directory=True)
            with self.assertRaises(ValueError):
                invoke("save", root / ".build/app/linked/cache")
            metadata = cache / ".source-inputs.json"
            metadata.unlink()
            outside = Path(external) / "metadata.json"
            outside.write_bytes(b"private contents")
            outside_time = touch(outside)
            metadata.symlink_to(outside)
            for action in ("restore", "save"):
                self.assertEqual(invoke(action, cache)["status"], "rejected")
            self.assertEqual(outside.read_bytes(), b"private contents")
            self.assertEqual(outside.stat().st_mtime_ns, outside_time)

    def test_cli_reports_safe_cache_miss_and_rejects_invalid_argument_path(self):
        script = Path(ci_prepare.__file__).resolve()
        with checkout() as (root, cache):
            command = [sys.executable, "-B", str(script), "cache-inputs", "restore", "--cache-dir"]
            miss = subprocess.run([*command, str(cache)], cwd=root, capture_output=True, text=True)
            self.assertEqual(miss.returncode, 0, miss.stderr)
            self.assertEqual(json.loads(miss.stdout)["status"], "missing")
            invalid = subprocess.run([*command, str(root)], cwd=root, capture_output=True, text=True)
            self.assertNotEqual(invalid.returncode, 0)
            self.assertIn("Input cache", invalid.stderr)


if __name__ == "__main__":
    unittest.main()
