"""Behavioral release boundaries. Never calls signing services or GitHub."""
import copy
import contextlib
import io
import json
import os
from pathlib import Path
import plistlib
import stat
import signal
import shlex
import subprocess
import sys
import tempfile
import time
import re
import textwrap
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts/lib/release"))
import release_contract as c
import release
import ci_security
import ci_prepare
import tap_update

CONFIG = {"schemaVersion": 1, "product": "SampleApp", "appName": "Sample App", "executable": "SampleApp",
          "bundleIdentifier": "dev.sample.app", "repository": "owner/sample-app", "caskToken": "sample-app",
          "description": "A local desktop app", "minimumSystemVersion": "14.0", "buildSystem": "swiftpm",
          "architectures": ["arm64", "x86_64", "universal"], "versionFile": "VERSION", "paths": {"build": "Scripts/build.sh"}}
COMMIT = "a" * 40


def fixture(directory, arch="universal", version="1.2.3", signed=True, notarized=True, build="7"):
    dmg = Path(directory) / c.artifact_name(CONFIG, version, arch)
    dmg.write_bytes((arch + version).encode())
    signature = {"category": "developer-id" if signed else "adhoc", "teamIdentifier": "TEAM" if signed else None,
                 "runtime": signed, "timestamp": "Oct 10, 2026" if signed else None}
    arches = ["arm64", "x86_64"] if arch == "universal" else [arch]
    bundle = {"appName": CONFIG["appName"], "executable": CONFIG["executable"], "architectures": arches,
              "minimumSystemVersionByArchitecture": {a: "14.0" for a in arches},
              "info": {"CFBundleShortVersionString": version.split("-")[0], "CFBundleVersion": build,
                       "CFBundleIdentifier": CONFIG["bundleIdentifier"], "CFBundleExecutable": CONFIG["executable"], "LSMinimumSystemVersion": "14.0"},
              "signature": copy.deepcopy(signature), "executableSignature": copy.deepcopy(signature), "payloadSha256": "c" * 64}
    notary = {"status": "not-requested", "receipt": None, "stapled": False}
    if notarized:
        receipt = dmg.with_suffix(".notary.json")
        receipt.write_text(json.dumps({"status": "Accepted", "id": "submission-" + arch}))
        notary = {"status": "accepted", "stapled": True, "submissionId": "submission-" + arch,
                  "receipt": {"path": receipt.name, "sha256": c.digest(receipt)}}
    manifest = {"schemaVersion": 1, "product": CONFIG["product"], "repository": CONFIG["repository"], "source": {"commit": COMMIT, "dirty": False},
                "version": version, "build": build, "architecture": arch, "bundle": bundle, "dmgSignature": signature,
                "artifact": {"name": dmg.name, "path": dmg.name, "size": dmg.stat().st_size, "sha256": c.digest(dmg)}, "notarization": notary}
    dmg.with_suffix(".json").write_text(json.dumps(manifest))
    dmg.with_suffix(".dmg.sha256").write_text(f"{c.digest(dmg)}  {dmg.name}\n")
    return dmg, manifest


class ContractTests(unittest.TestCase):
    def test_strict_semver_rejects_path_metadata_newline_and_numeric_leading_zero(self):
        for version in ("01.2.3", "1.2.3-beta.01", "1.2.3+build", "1.2.3\n", "../1.2.3", "1.2.3/arm64", "1.2", "v1.2.3"):
            with self.subTest(version=version), self.assertRaises(ValueError):
                c.validate_version(version)
        self.assertEqual(c.validate_version("1.2.3-beta.1"), "1.2.3-beta.1")

    def test_product_filename_and_cask_safe_text(self):
        self.assertEqual(c.artifact_name(CONFIG, "1.2.3", "universal"), "SampleApp-1.2.3-universal.dmg")
        for key, value in (("appName", 'evil#{system("rm") }'), ("description", 'bad"\nend'), ("repository", "../repo"), ("product", "../App")):
            bad = dict(CONFIG, **{key: value})
            with self.subTest(key=key), self.assertRaises(ValueError):
                c.validate_config(bad)

    def test_candidate_ad_hoc_uses_real_bytes_but_cannot_claim_promotion(self):
        with tempfile.TemporaryDirectory() as td:
            dmg, manifest = fixture(td, signed=False, notarized=False)
            candidate = Path(td) / "candidate.rb"
            original_manifest = copy.deepcopy(manifest)
            original_config = copy.deepcopy(CONFIG)
            result = c.prepare_cask(CONFIG, manifest, dmg, candidate)
            self.assertEqual(manifest, original_manifest)
            self.assertEqual(CONFIG, original_config)
            self.assertFalse(result["promotionReady"])
            self.assertIn(c.digest(dmg), candidate.read_text())
            self.assertIn('uninstall quit: "dev.sample.app"', candidate.read_text())
            self.assertNotIn("Application Support", candidate.read_text())
            self.assertNotIn("Keychain", candidate.read_text())
            with self.assertRaises(ValueError):
                c.prepare_cask(CONFIG, manifest, dmg, candidate, promotion=True)

    def test_homebrew_floors_use_supported_symbols_and_preserve_minor_and_patch(self):
        sonoma = c.cask_text(CONFIG, "1.2.3", "a" * 64)
        self.assertIn("depends_on macos: :sonoma", sonoma)
        self.assertNotIn("preflight_steps", sonoma)
        for floor, cases in (("13.5", (("13.4.9", False), ("13.5", True), ("13.5.0", True), ("14.0", True), ("26.0", True))),
                             ("13.5.1", (("13.5", False), ("13.5.0", False), ("13.5.1", True), ("13.6", True), ("14.0", True), ("26.0", True)))):
            with self.subTest(floor=floor):
                config = dict(CONFIG, minimumSystemVersion=floor)
                text = c.cask_text(config, "1.2.3", "a" * 64)
                self.assertIn("depends_on macos: :ventura", text)
                self.assertLess(text.index('app "Sample App.app"'), text.index("preflight_steps do"))
                self.assertLess(text.index("preflight_steps do"), text.index("uninstall quit:"))
                match = re.search(r'run "/bin/sh", args: \["-c", <<~SH\]\n(.*?)^    SH$', text, re.MULTILINE | re.DOTALL)
                self.assertIsNotNone(match)
                guard = textwrap.dedent(match[1])
                for actual, accepted in cases:
                    injected = guard.replace("$(/usr/bin/sw_vers -productVersion)", shlex.quote(actual))
                    result = subprocess.run(["/bin/sh", "-c", injected], capture_output=True, text=True)
                    self.assertEqual(result.returncode == 0, accepted, (floor, actual, result.stderr))
        with self.assertRaisesRegex(ValueError, "major"):
            c.cask_text(dict(CONFIG, minimumSystemVersion="99.0"), "1.2.3", "a" * 64)

    def test_no_notary_or_debug_or_wrong_team_or_dirty_cannot_promote(self):
        with tempfile.TemporaryDirectory() as td:
            dmg, manifest = fixture(td)
            c.verify_manifest(CONFIG, manifest, dmg, promotion=True)
            changes = [("notarization", {"status": "not-requested"}), ("source", {"commit": COMMIT, "dirty": True})]
            for key, value in changes:
                with self.subTest(key=key), self.assertRaises(ValueError):
                    c.verify_manifest(CONFIG, dict(manifest, **{key: value}), dmg, promotion=True)
            for scope, field, value in (("executableSignature", "runtime", False), ("signature", "teamIdentifier", "WRONG"), ("signature", "category", "adhoc")):
                bad = copy.deepcopy(manifest)
                bad["bundle"][scope][field] = value
                with self.subTest(scope=scope, field=field), self.assertRaises(ValueError):
                    c.verify_manifest(CONFIG, bad, dmg, promotion=True)

    def test_timestamp_sentinel_is_not_a_secure_manifest_timestamp(self):
        with tempfile.TemporaryDirectory() as td:
            dmg, manifest = fixture(td)
            for sentinel in ("none", "None", "NONE", "", "not set"):
                bad = copy.deepcopy(manifest)
                bad["dmgSignature"]["timestamp"] = sentinel
                with self.subTest(sentinel=sentinel), self.assertRaisesRegex(ValueError, "timestamp"):
                    c.verify_manifest(CONFIG, bad, dmg, promotion=True)

    def test_collection_accepts_universal_only_and_full_architecture_groups(self):
        for architectures in (("universal",), c.ARCHITECTURES):
            with self.subTest(architectures=architectures), tempfile.TemporaryDirectory() as td:
                for arch in architectures:
                    fixture(td, arch)
                manifests, files = c.collect(CONFIG, td, "1.2.3", COMMIT, promotion=True)
                self.assertEqual([m["architecture"] for m in manifests], list(architectures))
                self.assertEqual({p.name for p in files}, {c.artifact_name(CONFIG, "1.2.3", arch).removesuffix(".dmg") + suffix
                                                        for arch in architectures for suffix in (".dmg", ".json", ".dmg.sha256", ".notary.json")})
                universal = next(m for m in manifests if m["architecture"] == "universal")
                self.assertEqual(universal["bundle"]["architectures"], ["arm64", "x86_64"])

    def test_collection_rejects_partial_missing_unknown_and_duplicate_dmg_groups(self):
        for architectures in ((), ("arm64",), ("arm64", "x86_64"), ("arm64", "universal"), ("x86_64", "universal")):
            with self.subTest(architectures=architectures), tempfile.TemporaryDirectory() as td:
                for arch in architectures:
                    fixture(td, arch)
                with self.assertRaises(ValueError):
                    c.collect(CONFIG, td, "1.2.3", COMMIT, promotion=True)
        for architectures in (("universal",), c.ARCHITECTURES):
            for extra in ("unknown", "duplicate"):
                with self.subTest(architectures=architectures, extra=extra), tempfile.TemporaryDirectory() as td:
                    for arch in architectures:
                        fixture(td, arch)
                    if extra == "unknown":
                        (Path(td) / "unrelated.dmg").write_bytes(b"unexpected artifact")
                    else:
                        nested = Path(td) / "nested"
                        nested.mkdir()
                        fixture(nested, "universal")
                    with self.assertRaisesRegex(ValueError, "Unexpected DMG|exactly one"):
                        c.collect(CONFIG, td, "1.2.3", COMMIT, promotion=True)

    def test_universal_collection_retains_checksum_receipt_source_and_signature_gates(self):
        for invalid in ("checksum", "receipt", "source", "team", "slices"):
            with self.subTest(invalid=invalid), tempfile.TemporaryDirectory() as td:
                dmg, manifest = fixture(td)
                error = {"checksum": "checksum", "receipt": "receipt bytes", "source": "source mismatch",
                         "team": "consistent team", "slices": "architecture/identity"}[invalid]
                if invalid == "checksum":
                    dmg.with_suffix(".dmg.sha256").write_text(f"{'0' * 64}  {dmg.name}\n")
                elif invalid == "receipt":
                    dmg.with_suffix(".notary.json").write_text(json.dumps({"status": "Accepted", "id": "submission-universal", "changed": True}))
                else:
                    if invalid == "source":
                        manifest["source"]["commit"] = "b" * 40
                    elif invalid == "team":
                        manifest["bundle"]["executableSignature"]["teamIdentifier"] = "WRONG"
                    else:
                        manifest["bundle"]["architectures"] = ["arm64"]
                    dmg.with_suffix(".json").write_text(json.dumps(manifest))
                with self.assertRaisesRegex(ValueError, error):
                    c.collect(CONFIG, td, "1.2.3", COMMIT, promotion=True)

    def test_mutated_final_dmg_or_receipt_invalidates_collection(self):
        with tempfile.TemporaryDirectory() as td:
            for arch in c.ARCHITECTURES:
                fixture(td, arch)
            c.collect(CONFIG, td, "1.2.3", COMMIT, promotion=True)
            dmg = Path(td) / c.artifact_name(CONFIG, "1.2.3", "universal")
            dmg.write_bytes(b"replaced after stapling")
            with self.assertRaisesRegex(ValueError, "size/digest"):
                c.collect(CONFIG, td, "1.2.3", COMMIT, promotion=True)
        with tempfile.TemporaryDirectory() as td:
            for arch in c.ARCHITECTURES:
                fixture(td, arch)
            (Path(td) / "SampleApp-1.2.3-universal.notary.json").write_text('{"status":"Invalid","id":"submission"}')
            with self.assertRaisesRegex(ValueError, "Accepted"):
                c.collect(CONFIG, td, "1.2.3", COMMIT, promotion=True)

    def test_three_architectures_must_share_source_and_build(self):
        with tempfile.TemporaryDirectory() as td:
            fixture(td, "arm64")
            fixture(td, "x86_64", build="8")
            fixture(td, "universal")
            with self.assertRaisesRegex(ValueError, "share build"):
                c.collect(CONFIG, td, "1.2.3", COMMIT)
        for field, value in (("commit", "b" * 40), ("dirty", True)):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as td:
                for arch in c.ARCHITECTURES:
                    dmg, manifest = fixture(td, arch)
                    if arch == "arm64":
                        manifest["source"][field] = value
                        dmg.with_suffix(".json").write_text(json.dumps(manifest))
                with self.assertRaisesRegex(ValueError, "share build number and source state"):
                    c.collect(CONFIG, td, "1.2.3")

    def test_promotion_rejects_individually_valid_architectures_from_different_teams(self):
        with tempfile.TemporaryDirectory() as td:
            for arch, team in zip(c.ARCHITECTURES, ("TEAM_A", "TEAM_B", "TEAM_C")):
                dmg, manifest = fixture(td, arch)
                for signature in (manifest["bundle"]["signature"], manifest["bundle"]["executableSignature"], manifest["dmgSignature"]):
                    signature["teamIdentifier"] = team
                c.verify_manifest(CONFIG, manifest, dmg, promotion=True)
                dmg.with_suffix(".json").write_text(json.dumps(manifest))
            with self.assertRaisesRegex(ValueError, "same signing"):
                c.collect(CONFIG, td, "1.2.3", COMMIT, promotion=True)

    def test_collection_rejects_architectures_with_mixed_signing_or_notary_readiness(self):
        for inconsistent in ("signing", "notarization"):
            with self.subTest(inconsistent=inconsistent), tempfile.TemporaryDirectory() as td:
                for arch in c.ARCHITECTURES:
                    fixture(td, arch, signed=not (inconsistent == "signing" and arch == "arm64"), notarized=not (inconsistent == "notarization" and arch == "arm64"))
                with self.assertRaisesRegex(ValueError, "same signing"):
                    c.collect(CONFIG, td, "1.2.3", COMMIT)

    def test_prerelease_cannot_generate_stable_cask(self):
        with self.assertRaises(ValueError):
            c.cask_text(CONFIG, "1.2.3-beta.1", "f" * 64)

    def test_ci_environment_alone_cannot_enable_remote(self):
        env = {"GITHUB_ACTIONS": "true", "GITHUB_EVENT_NAME": "workflow_dispatch", "GH_TOKEN": "secret"}
        for flags in ((False, False), (True, False), (False, True)):
            with self.subTest(flags=flags), self.assertRaises(ValueError):
                c.require_ci_remote(*flags, environment=env)
        c.require_ci_remote(True, True, environment=env)
        with self.assertRaises(ValueError):
            c.require_ci_remote(True, True, environment=dict(env, GITHUB_EVENT_NAME="pull_request"))


    def test_release_identity_checks_source_tag_draft_and_prerelease(self):
        metadata = {"tag_name": "v1.2.3", "draft": False, "prerelease": False}
        with patch.object(release, "tag_commit", return_value=COMMIT):
            release.verify_release_identity(CONFIG, metadata, "1.2.3", COMMIT)
            for change in ({"draft": True}, {"prerelease": True}, {"tag_name": "v1.2.2"}):
                with self.subTest(change=change), self.assertRaises(ValueError):
                    release.verify_release_identity(CONFIG, dict(metadata, **change), "1.2.3", COMMIT)
        with patch.object(release, "tag_commit", return_value="b" * 40), self.assertRaises(ValueError):
            release.verify_release_identity(CONFIG, metadata, "1.2.3", COMMIT)

    def test_same_version_rerun_reuses_public_assets_without_local_collect_or_upload(self):
        with patch.object(release, "check_publish_state", return_value="reuse"), patch.object(release, "verify_public_release", return_value={"publicAssetVerified": True}) as verify, patch.object(c, "collect") as collect, patch.object(c, "run") as run:
            release.publish(CONFIG, "1.2.3", COMMIT, "/nonexistent/new-build")
            verify.assert_called_once_with(CONFIG, "1.2.3", COMMIT)
            collect.assert_not_called()
            run.assert_not_called()

    def test_actual_public_download_bytes_must_match_digest_and_url(self):
        with tempfile.TemporaryDirectory() as td:
            metadata = {"tag_name": "v1.2.3"}
            asset = {"name": "SampleApp-1.2.3-universal.dmg", "size": 7, "digest": "sha256:" + c.hashlib.sha256(b"correct").hexdigest(),
                     "browser_download_url": "https://github.com/owner/sample-app/releases/download/v1.2.3/SampleApp-1.2.3-universal.dmg"}
            with patch.object(release.urllib.request, "urlopen", return_value=io.BytesIO(b"changed")):
                with self.assertRaisesRegex(ValueError, "digest"):
                    release.download_asset(CONFIG, metadata, asset, td)
            with patch.object(release.urllib.request, "urlopen") as request, self.assertRaisesRegex(ValueError, "URL"):
                release.download_asset(CONFIG, metadata, dict(asset, name="new.dmg", browser_download_url="https://foreign.invalid/new.dmg"), td)
            request.assert_not_called()


class AppVerificationTests(unittest.TestCase):
    def test_new_sdk_entitlements_require_xml_output_and_debug_entitlement_is_rejected(self):
        def sdk(entitlements):
            def fake(args, **kwargs):
                details = "Authority=Developer ID Application: Test\nTeamIdentifier=TEAM\nTimestamp=Oct 10, 2026\nflags=0x10000(runtime)\n"
                if "--entitlements" in args:
                    data = b"[Dict]\n [Key] com.apple.security.app-sandbox\n [Value] false\n" if args[args.index("--entitlements") + 1] == "-" else plistlib.dumps(entitlements)
                    return subprocess.CompletedProcess(args, 0, stdout=data, stderr=b"")
                return subprocess.CompletedProcess(args, 0, stdout="", stderr=details)
            return fake
        with patch.object(c, "run", side_effect=sdk({"com.apple.security.app-sandbox": False})):
            self.assertEqual(c.signature_details("sample.app", "TEAM", True, executable=True)["category"], "developer-id")
        with patch.object(c, "run", side_effect=sdk({"com.apple.security.get-task-allow": True})), self.assertRaisesRegex(ValueError, "debugging"):
            c.signature_details("sample.app", "TEAM", True, executable=True)

    def test_nonempty_invalid_entitlements_are_not_silently_ignored(self):
        def invalid(args, **kwargs):
            if "--entitlements" in args:
                return subprocess.CompletedProcess(args, 0, stdout=b"invalid entitlement data", stderr=b"")
            details = "Authority=Developer ID Application: Test\nTeamIdentifier=TEAM\nTimestamp=Oct 10, 2026\nflags=0x10000(runtime)\n"
            return subprocess.CompletedProcess(args, 0, stdout="", stderr=details)
        with patch.object(c, "run", side_effect=invalid), self.assertRaises(plistlib.InvalidFileException):
            c.signature_details("sample.app", "TEAM", True, executable=True)

    def test_app_root_cannot_be_a_symlink_to_an_owner_outside_the_artifact(self):
        with tempfile.TemporaryDirectory() as td:
            owner = Path(td) / "owner" / "Sample App.app"
            owner.mkdir(parents=True)
            volume = Path(td) / "volume"
            volume.mkdir()
            link = volume / owner.name
            link.symlink_to(owner, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "root.*directory"):
                c.verify_app(CONFIG, link, "1.2.3", "7", "universal")

    def test_internal_framework_links_are_preserved_but_links_escaping_bundle_are_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            app = Path(td) / "Sample App.app"
            version = app / "Contents/Frameworks/Sample.framework/Versions/A"
            version.mkdir(parents=True)
            (version / "Sample").write_bytes(b"framework payload")
            (version.parent / "Current").symlink_to("A", target_is_directory=True)
            framework = version.parent.parent
            (framework / "Sample").symlink_to("Versions/Current/Sample")
            self.assertRegex(c.bundle_payload_digest(app), r"^[a-f0-9]{64}$")
            (app / "Contents/external").symlink_to(Path(td) / "host-private-data")
            with self.assertRaisesRegex(ValueError, "outside"):
                c.bundle_payload_digest(app)
    def test_same_signed_payload_at_two_paths_is_equivalent_but_resource_change_is_not(self):
        import shutil
        with tempfile.TemporaryDirectory() as td:
            original = Path(td) / "owner" / "Sample App.app"
            mounted = Path(td) / "mounted" / "Sample App.app"
            (original / "Contents/Resources").mkdir(parents=True)
            (original / "Contents/Resources/data.json").write_text('{"value":1}')
            shutil.copytree(original, mounted)
            def fake(args, **kwargs):
                details = f"Executable={args[-1]}/Contents/MacOS/SampleApp\nSignature=adhoc\nTeamIdentifier=not set\nCDHash=abc123\n"
                return subprocess.CompletedProcess(args, 0, stdout="", stderr=details)
            with patch.object(c, "run", side_effect=fake):
                self.assertEqual(c.signature_details(original), c.signature_details(mounted))
            self.assertEqual(c.bundle_payload_digest(original), c.bundle_payload_digest(mounted))
            (mounted / "Contents/Resources/data.json").write_text('{"value":2}')
            self.assertNotEqual(c.bundle_payload_digest(original), c.bundle_payload_digest(mounted))

    def test_manifest_consumes_mounted_copy_and_rejects_changed_resource_payload(self):
        import shutil
        with tempfile.TemporaryDirectory() as td:
            app = Path(td) / "owner" / "Sample App.app"
            (app / "Contents/MacOS").mkdir(parents=True)
            (app / "Contents/Resources").mkdir()
            info = {"CFBundleIdentifier": "dev.sample.app", "CFBundleExecutable": "SampleApp", "CFBundleShortVersionString": "1.2.3", "CFBundleVersion": "7", "LSMinimumSystemVersion": "14.0"}
            (app / "Contents/Info.plist").write_bytes(plistlib.dumps(info))
            (app / "Contents/MacOS/SampleApp").write_bytes(b"same executable")
            (app / "Contents/Resources/data.json").write_text('{"value":1}')
            dmg = Path(td) / "SampleApp-1.2.3-universal.dmg"
            dmg.write_bytes(b"final artifact")
            altered = False
            detached = []
            def external(args, **kwargs):
                args = list(map(str, args))
                if args[:2] == ["hdiutil", "attach"]:
                    mount = Path(args[args.index("-mountpoint") + 1])
                    copied = mount / app.name
                    shutil.copytree(app, copied)
                    if altered:
                        (copied / "Contents/Resources/data.json").write_text('{"value":2}')
                if args[:2] == ["hdiutil", "detach"]:
                    detached.append(args[-1])
                details = f"Executable={args[-1]}\nSignature=adhoc\nTeamIdentifier=not set\nCDHash=abc123\n"
                return subprocess.CompletedProcess(args, 0, stdout="", stderr=details)
            with patch.object(c, "source_state", return_value={"commit": COMMIT, "dirty": True}), patch.object(c, "run", side_effect=external), patch.object(c, "output", side_effect=lambda args: "arm64 x86_64\n" if args[0] == "lipo" else "minos 14.0\n"):
                manifest = c.create_manifest(CONFIG, td, app, dmg, "1.2.3", "7", "universal")
                self.assertEqual(manifest["bundle"]["payloadSha256"], c.bundle_payload_digest(app))
                altered = True
                with self.assertRaisesRegex(ValueError, "DMG app differs"):
                    c.create_manifest(CONFIG, td, app, dmg, "1.2.3", "7", "universal")
            self.assertEqual(len(detached), 2)

    def test_verify_app_checks_actual_architectures_and_macho_minimum(self):
        with tempfile.TemporaryDirectory() as td:
            app = Path(td) / "Sample App.app"
            (app / "Contents/MacOS").mkdir(parents=True)
            info = {"CFBundleIdentifier": "dev.sample.app", "CFBundleExecutable": "SampleApp", "CFBundleShortVersionString": "1.2.3", "CFBundleVersion": "7", "LSMinimumSystemVersion": "14.0"}
            (app / "Contents/Info.plist").write_bytes(plistlib.dumps(info))
            (app / "Contents/MacOS/SampleApp").write_bytes(b"macho-fixture")
            with patch.object(c, "signature_details", return_value={"category": "adhoc"}), patch.object(c, "output", side_effect=lambda args: "arm64 x86_64\n" if args[0] == "lipo" else "platform MACOS\nminos 14.0\nsdk 26.0\n"):
                c.verify_app(CONFIG, app, "1.2.3", "7", "universal")
            with patch.object(c, "output", return_value="arm64\n"), self.assertRaisesRegex(ValueError, "architecture"):
                c.verify_app(CONFIG, app, "1.2.3", "7", "universal")
            with patch.object(c, "output", side_effect=["arm64\n", "minos 13.0\n"]), self.assertRaisesRegex(ValueError, "minimum"):
                c.verify_app(CONFIG, app, "1.2.3", "7", "arm64")

    def test_dmg_signing_does_not_require_runtime_but_executable_does(self):
        details = "Authority=Developer ID Application: Test\nTeamIdentifier=TEAM\nTimestamp=Oct 10, 2026\n"
        def fake(args, **kwargs):
            return subprocess.CompletedProcess(args, 0, stdout=b"" if not kwargs.get("text") else "", stderr=details)
        with patch.object(c, "run", side_effect=fake):
            c.signature_details("sample.dmg", "TEAM", True)
            with self.assertRaisesRegex(ValueError, "runtime"):
                c.signature_details("sample.app", "TEAM", True, executable=True)

    def test_codesign_none_timestamp_cannot_pass_developer_id_verification(self):
        def fake(args, **kwargs):
            details = "Authority=Developer ID Application: Test\nTeamIdentifier=TEAM\nTimestamp=none\nflags=0x10000(runtime)\n"
            return subprocess.CompletedProcess(args, 0, stdout="", stderr=details)
        with patch.object(c, "run", side_effect=fake), self.assertRaisesRegex(ValueError, "timestamp"):
            c.signature_details("sample.dmg", "TEAM", True)


class ToolchainTests(unittest.TestCase):
    def owner_fixture(self, directory):
        root = Path(directory)
        config = root / "macos-app.json"
        config.write_text(json.dumps(CONFIG))
        scripts = root / "scripts"
        scripts.mkdir()
        (scripts / "app.py").write_text('''import argparse,pathlib
p=argparse.ArgumentParser()
p.add_argument('command', choices=['package'])
for name in ('version','build','arch','output','sign'):
    p.add_argument('--'+name,required=True)
a=p.parse_args()
out=pathlib.Path(a.output)
if not out.parent.is_dir():
    raise SystemExit('missing owner output parent')
out.mkdir()
(out/'app'/'Sample App.app').mkdir(parents=True)
(out/('SampleApp-'+a.version+'-'+a.arch+'.dmg')).write_bytes(b'owner artifact')
(pathlib.Path(__file__).parent.parent/'owner-ran').write_text('ran')
''')
        return config

    def test_ci_prepare_creates_owned_parents_and_preserves_new_output_owner_contract(self):
        with tempfile.TemporaryDirectory() as td:
            config = self.owner_fixture(td)
            output = Path(td) / "dist/release/arm64"
            self.assertFalse(output.parent.exists())
            manifest = {"artifact": {"sha256": "e" * 64}, "notarization": {"status": "not-requested"}}
            with patch.object(c, "create_manifest", return_value=manifest), patch("sys.stdout", new=io.StringIO()):
                ci_prepare.prepare(config, "1.2.3", "1", "arm64", output)
            self.assertTrue((Path(td) / "owner-ran").exists())
            self.assertTrue(output.is_dir())
            self.assertTrue((output / "SampleApp-1.2.3-arm64.json").is_file())

    def test_package_owner_environment_excludes_secrets_after_import_without_changing_parent(self):
        private_names = ("MACOS_CERTIFICATE", "MACOS_CERTIFICATE_PASSWORD", "MACOS_KEYCHAIN_PASSWORD",
                         "APPLE_NOTARIZATION_PRIVATE_KEY", "HOMEBREW_TAP_TOKEN", "GH_TOKEN", "GITHUB_TOKEN")
        parent = {name: "fixture-only-" + name.lower() for name in private_names}
        parent.update(MACOS_SIGNING_IDENTITY="fixture Developer ID identity", MACOS_TEAM_ID="FIXTURETEAM",
                      DEVELOPER_DIR="/fixture/Xcode/Contents/Developer", PATH="/fixture/tools",
                      APPLE_NOTARIZATION_KEY_ID="fixture-key-id", APPLE_NOTARIZATION_ISSUER_ID="fixture-issuer")
        for signed, notarized in ((False, False), (True, False), (True, True)):
            with self.subTest(signed=signed, notarized=notarized), tempfile.TemporaryDirectory() as td:
                config = self.owner_fixture(td)
                output = Path(td) / "dist/release/universal"
                imported_environment = dict(parent, CODE_SIGN_KEYCHAIN="/fixture/imported.keychain-db",
                                            KEYCHAIN_PATH="/fixture/imported.keychain-db")
                owner_environments = []
                import_finished = False
                @contextlib.contextmanager
                def import_keychain():
                    nonlocal import_finished
                    # Certificate import still needs the unchanged private parent values.
                    self.assertEqual(dict(os.environ), parent)
                    import_finished = True
                    yield imported_environment
                def owner(args, **kwargs):
                    if signed:
                        self.assertTrue(import_finished)
                    owner_environments.append(dict(kwargs.get("env", os.environ)))
                    output.resolve().mkdir()
                def notary(*args):
                    # Apple submission receives the parent's private key after packaging.
                    self.assertEqual(dict(os.environ), parent)
                manifest = {"artifact": {"sha256": "e" * 64}, "notarization": {"status": "accepted" if notarized else "not-requested"}}
                with patch.dict(os.environ, parent, clear=True), \
                     patch.object(ci_security, "temporary_keychain", side_effect=import_keychain) as keychain, \
                     patch.object(c, "run", side_effect=owner), \
                     patch.object(ci_security, "notarize", side_effect=notary) as submit, \
                     patch.object(c, "create_manifest", return_value=manifest), contextlib.redirect_stdout(io.StringIO()):
                    ci_prepare.prepare(config, "1.2.3", "1", "universal", output, signed=signed, notarized=notarized)
                    self.assertEqual(dict(os.environ), parent)
                self.assertEqual(len(owner_environments), 1)
                child = owner_environments[0]
                self.assertFalse(set(private_names) & set(child))
                for name in ("MACOS_SIGNING_IDENTITY", "MACOS_TEAM_ID", "DEVELOPER_DIR", "PATH"):
                    self.assertEqual(child[name], parent[name])
                if signed:
                    keychain.assert_called_once_with()
                    for name in ("CODE_SIGN_KEYCHAIN", "KEYCHAIN_PATH"):
                        self.assertEqual(child[name], imported_environment[name])
                else:
                    keychain.assert_not_called()
                if notarized:
                    submit.assert_called_once()
                else:
                    submit.assert_not_called()
                self.assertEqual(imported_environment, dict(parent, CODE_SIGN_KEYCHAIN="/fixture/imported.keychain-db",
                                                            KEYCHAIN_PATH="/fixture/imported.keychain-db"))

    def test_invalid_ci_prepare_inputs_do_not_create_parents_or_call_an_owner(self):
        for invalid in ("version", "arch", "relative", "outside", "symlink", "existing"):
            with self.subTest(invalid=invalid), tempfile.TemporaryDirectory() as td:
                config = self.owner_fixture(td)
                root = Path(td)
                output = root / "dist/release/arm64"
                version, arch = "1.2.3", "arm64"
                if invalid == "version":
                    version = "1.2.3/path"
                elif invalid == "arch":
                    arch = "future-arch"
                elif invalid == "relative":
                    output = Path("dist/release/arm64")
                elif invalid == "outside":
                    output = root.parent / (root.name + "-outside")
                elif invalid == "symlink":
                    (root / "external").mkdir()
                    (root / "dist").symlink_to(root / "external", target_is_directory=True)
                else:
                    output.mkdir(parents=True)
                with patch.object(c, "run") as owner, patch.object(ci_security, "temporary_keychain") as keychain:
                    with self.assertRaises(ValueError):
                        ci_prepare.prepare(config, version, "1", arch, output)
                    owner.assert_not_called()
                    keychain.assert_not_called()
                self.assertFalse((root / "owner-ran").exists())
                if invalid not in ("symlink", "existing"):
                    self.assertFalse((root / "dist").exists())

    def test_xcode_sdk_and_swift_versions_must_meet_compile_requirements(self):
        with tempfile.TemporaryDirectory() as td:
            expected = ["Xcode 27.0\nBuild version 18A1\n", "27.0\n", "Apple Swift version 6.4 (swiftlang-build)\nTarget: arm64-apple-macosx27.0\n"]
            with patch.object(c, "output", side_effect=expected) as command:
                result = ci_prepare.verify_toolchain(td)
            self.assertEqual(result["macosSDK"], "27.0")
            self.assertEqual(result["swift"], "6.4")
            self.assertTrue(all(call.kwargs["env"]["DEVELOPER_DIR"] == td for call in command.call_args_list))
            for index, insufficient in ((0, "Xcode 26.6\nBuild version old\n"), (1, "26.6\n"), (2, "Apple Swift version 6.3\n")):
                readings = list(expected)
                readings[index] = insufficient
                with self.subTest(index=index), patch.object(c, "output", side_effect=readings), self.assertRaises(ValueError):
                    ci_prepare.verify_toolchain(td)
            with patch.object(c, "output", side_effect=[expected[0], expected[1], "unrecognized tool output"]), self.assertRaises(ValueError):
                ci_prepare.verify_toolchain(td)

    def test_missing_developer_directory_fails_before_invoking_any_tool(self):
        with tempfile.TemporaryDirectory() as td, patch.object(c, "output") as command:
            with self.assertRaisesRegex(ValueError, "developer directory"):
                ci_prepare.verify_toolchain(str(Path(td) / "missing"))
            command.assert_not_called()

    def test_toolchain_cache_fingerprint_is_stable_and_tracks_exact_builds(self):
        readings = ["Xcode 27.0\nBuild version 18A1\n", "27.0\n", "Apple Swift version 6.4 (swiftlang-6.4.0.1)\nTarget: arm64-apple-macosx27.0\n"]
        with tempfile.TemporaryDirectory() as td, patch.object(ci_prepare.platform, "machine", return_value="arm64"):
            def verify(versions):
                with patch.object(c, "output", side_effect=versions):
                    return ci_prepare.verify_toolchain(td)
            baseline = verify(readings)
            self.assertEqual(baseline["cacheFingerprint"], c.hashlib.sha256((readings[0] + "\n27.0\n" + readings[2]).encode()).hexdigest())
            self.assertEqual(verify(readings)["cacheFingerprint"], baseline["cacheFingerprint"])
            for index, changed in ((0, readings[0].replace("18A1", "18A2")), (2, readings[2].replace("6.4.0.1", "6.4.0.2"))):
                versions = list(readings)
                versions[index] = changed
                with self.subTest(index=index):
                    result = verify(versions)
                    self.assertEqual((result["xcode"], result["swift"]), (baseline["xcode"], baseline["swift"]))
                    self.assertNotEqual(result["cacheFingerprint"], baseline["cacheFingerprint"])

    def test_toolchain_native_architecture_normalizes_aliases_and_rejects_unknown(self):
        readings = ["Xcode 27.0\nBuild version 18A1\n", "27.0\n", "Apple Swift version 6.4\n"]
        with tempfile.TemporaryDirectory() as td:
            for native, expected in (("arm64", "arm64"), ("aarch64", "arm64"), ("x86_64", "x86_64"), ("AMD64", "x86_64")):
                with self.subTest(native=native), patch.object(c, "output", side_effect=readings), patch.object(ci_prepare.platform, "machine", return_value=native):
                    self.assertEqual(ci_prepare.verify_toolchain(td)["nativeArchitecture"], expected)
            with patch.object(c, "output", side_effect=readings), patch.object(ci_prepare.platform, "machine", return_value="future-arch"), self.assertRaisesRegex(ValueError, "native build architecture"):
                ci_prepare.verify_toolchain(td)

    def test_toolchain_cli_appends_only_cache_metadata_and_runs_no_security_operations(self):
        readings = ["Xcode 27.0\nBuild version 18A1\n", "27.0\n", "Apple Swift version 6.4\n"]
        for output_enabled in (False, True):
            with self.subTest(output_enabled=output_enabled), tempfile.TemporaryDirectory() as td:
                output = Path(td) / "github-output"
                if output_enabled:
                    output.write_text("earlier=value\n")
                environment = {"GITHUB_OUTPUT": str(output)} if output_enabled else {}
                capture = io.StringIO()
                with patch.dict(os.environ, environment, clear=True), \
                     patch.object(ci_prepare, "DEVELOPER_DIRECTORY", td), \
                     patch.object(ci_prepare.platform, "machine", return_value="aarch64"), \
                     patch.object(c, "output", side_effect=readings) as command, \
                     patch.object(c, "run", side_effect=AssertionError("external mutation")), \
                     patch.object(ci_security, "temporary_keychain", side_effect=AssertionError("security operation")), \
                     patch.object(ci_security, "notarize", side_effect=AssertionError("notary operation")), \
                     contextlib.redirect_stdout(capture):
                    result = ci_prepare.main(["verify-toolchain"])
                self.assertEqual(json.loads(capture.getvalue()), result)
                self.assertEqual([call.args[0] for call in command.call_args_list],
                                 [["xcodebuild", "-version"], ["xcrun", "--sdk", "macosx", "--show-sdk-version"], ["xcrun", "swift", "--version"]])
                if output_enabled:
                    self.assertEqual(output.read_text(), f"earlier=value\nfingerprint={result['cacheFingerprint']}\narchitecture=arm64\n")
                    self.assertEqual(list(Path(td).iterdir()), [output])
                else:
                    self.assertEqual(list(Path(td).iterdir()), [])



class SecurityTests(unittest.TestCase):
    def test_real_sigterm_during_notary_wait_removes_mock_private_key(self):
        with tempfile.TemporaryDirectory() as td:
            directory = Path(td)
            tools = directory / "tools"
            tools.mkdir()
            ready = directory / "ready"
            dmg = directory / "sample.dmg"
            dmg.write_bytes(b"test-only artifact")
            receipt = directory / "receipt.json"
            stub = "#!" + sys.executable + "\n" + '''import os, pathlib, sys, time
name = pathlib.Path(sys.argv[0]).name
if name == "codesign" and "--display" in sys.argv:
    print("Authority=Developer ID Application: Test\\nTeamIdentifier=TEAM\\nTimestamp=Oct 10, 2026", file=sys.stderr)
elif name == "xcrun" and sys.argv[1:3] == ["notarytool", "submit"]:
    pathlib.Path(os.environ["MOCK_NOTARY_READY"]).write_text("waiting")
    time.sleep(0.5)
    print('{"status":"Invalid","id":"mock-submission"}')
'''
            for name in ("codesign", "hdiutil", "xcrun"):
                executable = tools / name
                executable.write_text(stub)
                executable.chmod(0o755)
            env = dict(os.environ, PATH=str(tools) + os.pathsep + os.environ.get("PATH", ""),
                       MOCK_NOTARY_READY=str(ready), RUNNER_TEMP=td, GITHUB_ACTIONS="true", GITHUB_EVENT_NAME="workflow_dispatch",
                       MACOS_TEAM_ID="TEAM", APPLE_NOTARIZATION_PRIVATE_KEY="-----BEGIN PRIVATE KEY-----\nmock-only-non-key\n-----END PRIVATE KEY-----",
                       APPLE_NOTARIZATION_KEY_ID="MOCKKEY", APPLE_NOTARIZATION_ISSUER_ID="MOCKISSUER")
            command = [sys.executable, "-B", str(Path(ci_security.__file__)), "notarize", "--ci", "--allow-remote", "--dmg", str(dmg), "--receipt", str(receipt)]
            child = subprocess.Popen(command, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                deadline = time.monotonic() + 5
                while not ready.exists() and child.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(ready.exists(), "mock submission must be waiting before SIGTERM")
                keys = list(directory.glob("release-notary-*/AuthKey.p8"))
                self.assertEqual(len(keys), 1)
                self.assertEqual(stat.S_IMODE(keys[0].stat().st_mode), 0o600)
                child.send_signal(signal.SIGTERM)
                stdout, stderr = child.communicate(timeout=5)
                self.assertFalse(list(directory.glob("release-notary-*")), "SIGTERM must clean the notary credential directory")
                self.assertEqual(child.returncode, 128 + signal.SIGTERM)
                self.assertNotIn("mock-only-non-key", stdout + stderr)
            finally:
                if child.poll() is None:
                    child.kill()
                    child.communicate()

    def test_cleanup_error_preserves_owner_failure_and_restores_previous_signal_handler(self):
        import contextlib
        with tempfile.TemporaryDirectory() as td:
            calls = []
            env = {"RUNNER_TEMP": td, "MACOS_CERTIFICATE": "Y2VydA==", "MACOS_CERTIFICATE_PASSWORD": "password", "MACOS_SIGNING_IDENTITY": "Developer ID Application: Test", "MACOS_TEAM_ID": "TEAM"}
            def fake(*args):
                args = tuple(map(str, args))
                calls.append(args)
                if args == ("list-keychains", "-d", "user", "-s", "/original/a.keychain-db"):
                    raise OSError("simulated restore failure")
                text = '"/original/a.keychain-db"\n' if args in (("list-keychains", "-d", "user"), ("default-keychain", "-d", "user")) else ""
                return subprocess.CompletedProcess(args, 0, stdout=text, stderr="")
            original = signal.getsignal(signal.SIGTERM)
            def prior_handler(signum, frame):
                pass
            signal.signal(signal.SIGTERM, prior_handler)
            try:
                with patch.object(ci_security, "security", side_effect=fake), contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaisesRegex(RuntimeError, "owner failed"):
                        with ci_security.temporary_keychain(env):
                            raise RuntimeError("owner failed")
                self.assertIs(signal.getsignal(signal.SIGTERM), prior_handler)
                self.assertIn(("default-keychain", "-d", "user", "-s", "/original/a.keychain-db"), calls)
                self.assertEqual(calls[-1][0], "delete-keychain")
            finally:
                signal.signal(signal.SIGTERM, original)

    def test_real_sigterm_runs_mock_security_cleanup_before_child_exits(self):
        with tempfile.TemporaryDirectory() as td:
            directory = Path(td)
            ready = directory / "ready"
            events = directory / "events.jsonl"
            executable = directory / "security"
            executable.write_text("#!" + sys.executable + "\n" + '''import json, os, pathlib, sys
args = sys.argv[1:]
event = {"action": args[0], "restored": "-s" in args and any(str(value).startswith("/original/") for value in args)}
with pathlib.Path(os.environ["MOCK_SECURITY_LOG"]).open("a") as stream:
    stream.write(json.dumps(event) + "\\n")
if args == ["list-keychains", "-d", "user"]:
    print('"/original/a.keychain-db" "/original/b.keychain-db"')
elif args == ["default-keychain", "-d", "user"]:
    print('"/original/a.keychain-db"')
''')
            executable.chmod(0o755)
            env = dict(os.environ, PATH=str(directory) + os.pathsep + os.environ.get("PATH", ""),
                       MOCK_SECURITY_LOG=str(events), GITHUB_ACTIONS="true", GITHUB_EVENT_NAME="workflow_dispatch",
                       RUNNER_TEMP=td, MACOS_CERTIFICATE="Y2VydA==", MACOS_CERTIFICATE_PASSWORD="test-password",
                       MACOS_SIGNING_IDENTITY="Developer ID Application: Test", MACOS_TEAM_ID="TEAM")
            owner_code = "import pathlib,time;pathlib.Path(" + repr(str(ready)) + ").write_text('ready');time.sleep(0.5)"
            command = [sys.executable, "-B", str(Path(ci_security.__file__)), "run-with-keychain", "--ci", "--allow-remote", "--", sys.executable, "-c", owner_code]
            child = subprocess.Popen(command, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                deadline = time.monotonic() + 5
                while not ready.exists() and child.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(ready.exists(), "test owner must enter the keychain context before SIGTERM")
                child.send_signal(signal.SIGTERM)
                stdout, stderr = child.communicate(timeout=5)
                recorded = [json.loads(line) for line in events.read_text().splitlines()]
                self.assertIn({"action": "list-keychains", "restored": True}, recorded)
                self.assertIn({"action": "default-keychain", "restored": True}, recorded)
                self.assertEqual(recorded[-1]["action"], "delete-keychain")
                self.assertEqual(child.returncode, 128 + signal.SIGTERM)
                self.assertFalse(list(directory.glob("release-signing-*")))
                self.assertNotIn("test-password", stdout + stderr)
            finally:
                if child.poll() is None:
                    child.kill()
                    child.communicate()

    def test_keychain_restores_default_and_search_list_when_owner_fails(self):
        with tempfile.TemporaryDirectory() as td:
            calls = []
            env = {"RUNNER_TEMP": td, "MACOS_CERTIFICATE": "Y2VydA==", "MACOS_CERTIFICATE_PASSWORD": "password", "MACOS_SIGNING_IDENTITY": "Developer ID Application: Test", "MACOS_TEAM_ID": "TEAM"}
            def fake(*args):
                calls.append(tuple(map(str, args)))
                text = '"/original/a.keychain-db" "/original/b.keychain-db"\n' if args == ("list-keychains", "-d", "user") else '"/original/a.keychain-db"\n' if args == ("default-keychain", "-d", "user") else ""
                return subprocess.CompletedProcess(args, 0, stdout=text, stderr="")
            with patch.object(ci_security, "security", side_effect=fake):
                with self.assertRaisesRegex(RuntimeError, "owner failed"):
                    with ci_security.temporary_keychain(env) as active:
                        p12 = Path(active["CODE_SIGN_KEYCHAIN"]).parent / "certificate.p12"
                        self.assertEqual(stat.S_IMODE(p12.stat().st_mode), 0o600)
                        raise RuntimeError("owner failed")
            self.assertIn(("list-keychains", "-d", "user", "-s", "/original/a.keychain-db", "/original/b.keychain-db"), calls)
            self.assertIn(("default-keychain", "-d", "user", "-s", "/original/a.keychain-db"), calls)
            self.assertEqual(calls[-1][0], "delete-keychain")
            self.assertEqual(list(Path(td).iterdir()), [])

    def test_exit_zero_notary_invalid_is_failure_and_never_stapled(self):
        with tempfile.TemporaryDirectory() as td:
            env = {"RUNNER_TEMP": td, "APPLE_NOTARIZATION_PRIVATE_KEY": "-----BEGIN PRIVATE KEY-----\nfixture\n-----END PRIVATE KEY-----", "APPLE_NOTARIZATION_KEY_ID": "KEY", "APPLE_NOTARIZATION_ISSUER_ID": "ISSUER", "MACOS_TEAM_ID": "TEAM"}
            receipt = Path(td) / "receipt.json"
            (Path(td) / "sample.dmg").write_bytes(b"signed package")
            with patch.object(c, "signature_details"), patch.object(c, "run") as run, patch.object(ci_security.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, stdout='{"status":"Invalid","id":"failed"}', stderr="")):
                with self.assertRaisesRegex(ValueError, "Accepted"):
                    ci_security.notarize(Path(td) / "sample.dmg", receipt, env)
            self.assertEqual(json.loads(receipt.read_text())["status"], "Invalid")
            self.assertFalse(any("stapler" in map(str, call.args[0]) for call in run.call_args_list))
            self.assertFalse(any(path.suffix == ".p8" for path in Path(td).rglob("*")))

    def test_notary_submission_errors_are_safe_and_do_not_claim_missing_evidence(self):
        for stdout, stderr, code, classification in (
            ("", "HTTP status code 401. Invalid credentials. sensitive-fixture", 1, "authentication rejected"),
            ("not JSON: sensitive-fixture", "HTTP status code 403. sensitive-fixture", 1, "authorization rejected"),
            ("[]", "sensitive-fixture", 1, "submission failed"),
            ("null", "sensitive-fixture", 0, "invalid submission response"),
            ("", "sensitive-fixture", 0, "invalid submission response"),
        ):
            with self.subTest(stdout=stdout, code=code), tempfile.TemporaryDirectory() as td:
                env = {"RUNNER_TEMP": td, "APPLE_NOTARIZATION_PRIVATE_KEY": "-----BEGIN PRIVATE KEY-----\nsensitive-fixture\n-----END PRIVATE KEY-----", "APPLE_NOTARIZATION_KEY_ID": "KEY", "APPLE_NOTARIZATION_ISSUER_ID": "ISSUER"}
                receipt = Path(td) / "receipt.json"
                (Path(td) / "sample.dmg").write_bytes(b"signed package")
                result = subprocess.CompletedProcess([], code, stdout=stdout, stderr=stderr)
                with patch.object(c, "signature_details"), patch.object(c, "run") as run, patch.object(ci_security.subprocess, "run", return_value=result) as external:
                    with self.assertRaises(RuntimeError) as failure:
                        ci_security.notarize(Path(td) / "sample.dmg", receipt, env)
                diagnostic = str(failure.exception)
                self.assertIn(classification, diagnostic)
                self.assertIn("exit code " + str(code), diagnostic)
                self.assertIn("No valid JSON receipt or submission ID", diagnostic)
                self.assertNotIn("sensitive-fixture", diagnostic)
                self.assertNotIn("retained receipt/log contains evidence", diagnostic)
                self.assertEqual(receipt.read_text(), stdout or "{}\n")
                self.assertEqual(external.call_count, 1, "no log request without a submission ID")
                self.assertFalse(any("stapler" in map(str, call.args[0]) for call in run.call_args_list))
                self.assertFalse(list(Path(td).glob("release-notary-*")))

    def test_notary_failed_json_receipt_without_id_is_retained_without_requesting_log(self):
        with tempfile.TemporaryDirectory() as td:
            env = {"RUNNER_TEMP": td, "APPLE_NOTARIZATION_PRIVATE_KEY": "-----BEGIN PRIVATE KEY-----\nfixture\n-----END PRIVATE KEY-----", "APPLE_NOTARIZATION_KEY_ID": "KEY", "APPLE_NOTARIZATION_ISSUER_ID": "ISSUER"}
            receipt = Path(td) / "receipt.json"
            (Path(td) / "sample.dmg").write_bytes(b"signed package")
            stdout = '{"status":"Error"}'
            with patch.object(c, "signature_details"), patch.object(c, "run") as run, patch.object(ci_security.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, stdout=stdout, stderr="")) as external:
                with self.assertRaisesRegex(RuntimeError, "JSON receipt retained; no submission ID"):
                    ci_security.notarize(Path(td) / "sample.dmg", receipt, env)
            self.assertEqual(receipt.read_text(), stdout)
            self.assertEqual(external.call_count, 1)
            self.assertFalse(any("stapler" in map(str, call.args[0]) for call in run.call_args_list))


class ReleaseDiagnosticTests(unittest.TestCase):
    def test_github_api_failure_keeps_safe_http_diagnosis(self):
        rejected = subprocess.CompletedProcess([], 1, 'SECRET-RESPONSE', 'gh: Bad credentials (HTTP 401)')
        with patch.object(release.subprocess, 'run', return_value=rejected), contextlib.redirect_stderr(io.StringIO()) as output:
            with self.assertRaises(subprocess.CalledProcessError) as raised:
                release.github_json('repos/owner/sample-app')
        self.assertIn('stage=github-api-read kind=authentication', c.failure_message(raised.exception))
        self.assertNotIn('SECRET-RESPONSE', output.getvalue())

    def test_external_failures_are_classified_without_echoing_commands_or_output(self):
        cases = [("remote: Invalid username or token. Authentication failed", "authentication"),
                 ("remote: Write access to repository not granted. HTTP 403", "authorization"),
                 ("remote: Repository not found.", "repository-access"),
                 ("fatal: Could not resolve host: github.com", "network"),
                 ("! [rejected] HEAD -> main (fetch first)", "concurrent-update"),
                 ("opaque tool failure", "external-command")]
        for text, expected in cases:
            with self.subTest(expected=expected):
                error = subprocess.CalledProcessError(128, ["git", "SECRET-COMMAND"],
                                                     output="SECRET-STDOUT", stderr=text + " SECRET-STDERR")
                output = io.StringIO()
                with patch.dict(os.environ, {"GITHUB_ACTIONS": "true"}), contextlib.redirect_stderr(output):
                    with self.assertRaises(subprocess.CalledProcessError):
                        with c.release_stage("tap-push"):
                            raise error
                message = output.getvalue()
                self.assertIn("stage=tap-push", message)
                self.assertIn("kind=" + expected, message)
                self.assertIn("exit=128", message)
                self.assertIn("::error", message)
                self.assertIn("HOMEBREW_TAP_TOKEN", message)
                self.assertIn(expected, c.failure_message(error))
                for secret in ("SECRET-COMMAND", "SECRET-STDOUT", "SECRET-STDERR"):
                    self.assertNotIn(secret, message)

    def test_nested_stage_retains_original_validation_and_redacts_secrets(self):
        output = io.StringIO()
        with patch.dict(os.environ, {"GITHUB_ACTIONS": "true", "HOMEBREW_TAP_TOKEN": "fixture-private-token"}), \
             contextlib.redirect_stderr(output):
            with self.assertRaises(ValueError) as raised:
                with c.release_stage("prepare"):
                    with c.release_stage("notarization"):
                        raise ValueError("Invalid receipt fixture-private-token\nunsafe next line")
        self.assertEqual(output.getvalue().count("::error"), 1)
        self.assertIn("stage=notarization", c.failure_message(raised.exception))
        self.assertIn("kind=validation", output.getvalue())
        self.assertNotIn("fixture-private-token", output.getvalue())
        self.assertNotIn("\nunsafe next line", output.getvalue())


class TapTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.tap = self.root / "tap"
        self.tap.mkdir()
        self.git("init", "-b", "main")
        self.git("config", "user.name", "Original")
        self.git("config", "user.email", "original@example.invalid")
        (self.tap / "Casks").mkdir()
        self.target = self.tap / "Casks/sample-app.rb"
        self.target.write_text(c.cask_text(CONFIG, "1.0.0", "a" * 64))
        (self.tap / "Casks/other.rb").write_text("other cask\n")
        self.git("add", ".")
        self.git("commit", "-m", "Initial")
        self.candidate = self.root / "candidate.rb"
        self.candidate.write_text(c.cask_text(CONFIG, "1.2.3", "b" * 64))

    def tearDown(self):
        self.temporary.cleanup()

    def git(self, *args):
        return subprocess.check_output(["git", "-C", str(self.tap), *args], text=True, stderr=subprocess.DEVNULL)

    def test_preview_is_readonly_and_local_commit_only_changes_one_cask(self):
        before = self.git("rev-parse", "HEAD")
        tap_update.update(self.tap, CONFIG, self.candidate, "1.2.3")
        self.assertIn('version "1.0.0"', self.target.read_text())
        self.assertEqual(before, self.git("rev-parse", "HEAD"))
        (self.tap / "Casks/other.rb").write_text("unrelated work\n")
        result = tap_update.update(self.tap, CONFIG, self.candidate, "1.2.3", write=True, commit=True)
        self.assertTrue(result["committed"])
        self.assertEqual(self.git("show", "--pretty=", "--name-only", "HEAD").strip(), "Casks/sample-app.rb")
        self.assertEqual(self.git("config", "user.name").strip(), "Original")
        self.assertIn("unrelated work", (self.tap / "Casks/other.rb").read_text())
        self.assertFalse(tap_update.update(self.tap, CONFIG, self.candidate, "1.2.3", write=True)["changed"])

    def test_foreign_staged_or_dirty_target_preserves_all_bytes(self):
        (self.tap / "Casks/other.rb").write_text("staged user work\n")
        self.git("add", "Casks/other.rb")
        before = self.target.read_bytes()
        with self.assertRaisesRegex(ValueError, "staged"):
            tap_update.update(self.tap, CONFIG, self.candidate, "1.2.3", write=True, commit=True)
        self.assertEqual(before, self.target.read_bytes())
        self.git("reset", "--", "Casks/other.rb")
        self.target.write_text("user edited target\n")
        with self.assertRaisesRegex(ValueError, "dirty"):
            tap_update.update(self.tap, CONFIG, self.candidate, "1.2.3", write=True)
        self.assertEqual(self.target.read_text(), "user edited target\n")

    def test_immutable_hash_drift_and_downgrade_are_rejected(self):
        for version, sha, error in (("1.0.0", "b" * 64, "drift"), ("0.9.9", "a" * 64, "downgrade")):
            self.candidate.write_text(c.cask_text(CONFIG, version, sha))
            with self.subTest(version=version), self.assertRaisesRegex(ValueError, error):
                tap_update.update(self.tap, CONFIG, self.candidate, version, write=True)
        self.assertIn('version "1.0.0"', self.target.read_text())

    def promotion(self, pushes):
        calls = []
        def fake_git(tap, *arguments):
            calls.append(arguments)
            if arguments[:3] == ("remote", "get-url", "origin"):
                return "https://github.com/mayflies-app/homebrew-tap.git"
            if arguments == ("branch", "--show-current"):
                return "main"
            if arguments[:2] == ("diff", "--name-only") and arguments[-1] == "HEAD":
                return "Casks/sample-app.rb\n"
            return ""
        with patch.object(tap_update, "check_checkout"), \
             patch.object(tap_update, "git", side_effect=fake_git), \
             patch.object(release, "verify_public_release", return_value={"artifact": {"sha256": "b" * 64}}), \
             patch.object(tap_update, "update", return_value={"changed": True, "committed": True}), \
             patch.object(tap_update.subprocess, "run", side_effect=pushes) as push:
            try:
                result = tap_update.remote_update(self.tap, CONFIG, self.candidate, "1.2.3", COMMIT, "main", "mayflies-app/homebrew-tap")
            finally:
                self.promotion_calls, self.promotion_push_count = calls, push.call_count
        return result

    def test_authentication_failure_stops_without_fetch_or_retry_and_retains_diagnosis(self):
        for text, kind in [('Bad credentials. Authentication failed.', 'authentication'),
                           ('Write access to repository not granted.', 'authorization'),
                           ('Could not resolve host: github.com', 'network'),
                           ('An unrecognized rejection', 'external-command')]:
            failure = subprocess.CompletedProcess([], 128, "", text + " SECRET-RAW-TOKEN")
            with self.subTest(kind=kind), contextlib.redirect_stderr(io.StringIO()) as output:
                with self.assertRaises(subprocess.CalledProcessError) as raised:
                    self.promotion([failure, failure, failure])
            self.assertEqual(self.promotion_push_count, 1)
            self.assertEqual(sum(args[0] == "fetch" for args in self.promotion_calls), 1)
            self.assertIn("stage=tap-push", c.failure_message(raised.exception))
            self.assertIn("kind=" + kind, c.failure_message(raised.exception))
            self.assertNotIn("SECRET-RAW-TOKEN", output.getvalue())

    def test_only_non_fast_forward_is_rebased_and_retried(self):
        rejected = subprocess.CompletedProcess([], 1, "", "! [rejected] HEAD -> main (fetch first)")
        accepted = subprocess.CompletedProcess([], 0, "", "")
        with contextlib.redirect_stderr(io.StringIO()) as output:
            result = self.promotion([rejected, accepted])
        self.assertTrue(result["pushed"])
        self.assertEqual(self.promotion_push_count, 2)
        self.assertEqual(sum(args[0] == "rebase" for args in self.promotion_calls), 1)
        self.assertIn("status=retry", output.getvalue())
        self.assertNotIn("::error", output.getvalue())


if __name__ == "__main__":
    unittest.main()
