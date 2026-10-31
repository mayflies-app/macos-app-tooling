#!/usr/bin/env python3
"""Compare or copy shared files into explicitly supplied app checkouts."""
import argparse
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parent.parent


def safe_path(base, relative):
    if not isinstance(relative, str) or not relative:
        raise ValueError("Managed paths must be nonempty repository-relative strings")
    relative = PurePosixPath(relative)
    if relative.is_absolute() or any(part in (".", "..", ".git") for part in relative.parts):
        raise ValueError(f"Unsafe managed path: {relative}")
    path = base
    if path.is_symlink():
        raise ValueError(f"Symlink root: {base}")
    for part in relative.parts:
        path = path / part
        if path.is_symlink():
            raise ValueError(f"Symlink managed path: {path}")
    if not path.resolve().is_relative_to(base.resolve()):
        raise ValueError(f"Managed path leaves repository: {path}")
    return path


def payloads(root, shared):
    template = safe_path(root, "templates/macos-app")
    result = []
    # Ignore machine-local caches and untracked files in the template directory.
    tracked = subprocess.check_output(["git", "-C", str(root), "ls-files", "-z", "--",
                                       "templates/macos-app/scripts"]).split(b"\0")
    for name in sorted(os.fsdecode(name) for name in tracked if name):
        source = safe_path(root, name)
        result.append((source, source.relative_to(template).as_posix()))
    if not result:
        raise ValueError("Template scripts are missing")
    result += [(safe_path(template, name), f".github/workflows/{name}") for name in ("ci.yml", "release.yml")]
    result.append((safe_path(template, "dependabot.yml"), ".github/dependabot.yml"))
    for key, name in (("designDestination", "MacAppDesign.swift"),
                      ("localizationDestination", "LocalizationResources.swift")):
        if key in shared:
            if not isinstance(shared[key], str):
                raise ValueError("Shared destinations must be repository-relative strings")
            result.append((safe_path(template, f"shared/{name}"), shared[key]))
    destinations = [PurePosixPath(relative).as_posix() for _, relative in result]
    if len(set(destinations)) != len(destinations):
        raise ValueError("Shared destinations must be distinct")
    for source, _ in result:
        if not source.is_file():
            raise ValueError(f"Template file missing: {source}")
    return result


def prepare(root, clone, apply=False):
    clone = Path(clone).absolute()
    if clone.is_symlink() or not clone.is_dir():
        raise ValueError(f"Missing or symlink app checkout: {clone}")
    repo = clone.resolve()
    if repo == root.resolve():
        raise ValueError("An app checkout cannot be the source repository")
    observed = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "--show-toplevel"], text=True).strip()
    if Path(observed).resolve() != repo:
        raise ValueError(f"App path is not the repository root: {repo}")
    config = json.loads(safe_path(repo, "macos-app.json").read_text())
    if (not isinstance(config, dict) or config.get("schemaVersion") != 1
            or config.get("buildSystem") not in ("swiftpm", "xcode")
            or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]*", str(config.get("product", "")))):
        raise ValueError("Expected a macOS app configuration with a valid product")
    shared = config.get("shared")
    if not isinstance(shared, dict) or set(shared) - {"designDestination", "localizationDestination"}:
        raise ValueError("macos-app.json must declare a shared object with repository-relative destinations")
    changes = []
    for source, relative in payloads(root, shared):
        target = safe_path(repo, relative)
        if target.exists() and not target.is_file():
            raise ValueError(f"Managed target is not a file: {target}")
        if (not target.exists() or source.read_bytes() != target.read_bytes()
                or stat.S_IMODE(source.stat().st_mode) != stat.S_IMODE(target.stat().st_mode)):
            changes.append((source, relative))
    if apply and changes:
        staged = subprocess.check_output(["git", "-C", str(repo), "diff", "--cached", "--name-only", "--", *[relative for _, relative in changes]], text=True).strip()
        if staged:
            raise ValueError(f"Refuse to overwrite staged managed files in {config['product']}:\n{staged}")
    return config["product"], repo, changes


def sync(prepared, apply=False):
    name, repo, changes = prepared
    if apply:
        for source, relative in changes:
            target = safe_path(repo, relative)
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(dir=target.parent, prefix=".sync-", delete=False) as stream:
                    temporary = Path(stream.name)
                    stream.write(source.read_bytes())
                shutil.copymode(source, temporary)
                safe_path(repo, relative)
                os.replace(temporary, target)
            finally:
                if temporary is not None and temporary.exists():
                    temporary.unlink()
    for _, relative in changes:
        print(f"{'copied' if apply else 'differs'} {name}/{relative}")
    if not changes:
        print(f"matches {name}")
    return not changes or apply


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("checkouts", metavar="APP_CHECKOUT", nargs="+",
                        help="App repository root; relative paths use the caller's working directory")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="Readonly byte comparison (default)")
    mode.add_argument("--apply", action="store_true", help="Copy common files; preserve product tools and staged changes")
    args = parser.parse_args()
    try:
        prepared = [prepare(ROOT, checkout, args.apply) for checkout in args.checkouts]
        if len({repo for _, repo, _ in prepared}) != len(prepared):
            raise ValueError("Each app checkout must be supplied only once")
        results = [sync(item, args.apply) for item in prepared]
        return 0 if all(results) else 1
    except (ValueError, OSError, KeyError, subprocess.CalledProcessError) as error:
        parser.exit(2, f"sync_apps: {error}\n")


if __name__ == "__main__":
    raise SystemExit(main())
