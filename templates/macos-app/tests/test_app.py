"""Isolated CLI and recorder acceptance; no signing, GUI or remote operation."""
import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import plistlib
import shutil
import shlex
import signal
import stat
import subprocess
import sys
import tempfile
import time
import types
import unittest
from unittest.mock import patch

sys.dont_write_bytecode = True
spec = importlib.util.spec_from_file_location("common_app", Path(__file__).parents[1] / "scripts/app.py")
app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app)


class CLIAcceptance(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.config = {"schemaVersion": 1, "buildSystem": "swiftpm", "product": "Example", "appName": "Example App",
                       "executable": "Example", "bundleIdentifier": "dev.example.app", "minimumSystemVersion": "14.0",
                       "versionFile": "VERSION", "packaging": {"resources": [], "resourceBundles": [], "dependencyLicenses": []}}
        self.write_config()
        (self.root / "VERSION").write_text("1.2.3\n")
        self.calls = []
        self.fail_at = None

    def tearDown(self):
        self.temporary.cleanup()

    def write_config(self):
        (self.root / "macos-app.json").write_text(json.dumps(self.config))

    def tool(self, argv, root, capture=False):
        argv = list(map(str, argv))
        self.calls.append(argv)
        if self.fail_at and argv[0] == self.fail_at:
            raise subprocess.CalledProcessError(1, argv)
        if argv[:2] == ["swift", "build"]:
            scratch = Path(argv[argv.index("--scratch-path") + 1])
            arch = scratch.name
            directory = scratch / "bin"
            directory.mkdir(parents=True, exist_ok=True)
            (directory / "Example").write_bytes(arch.encode())
            for name in ["Example_Resources.bundle", "AutoDiscovered.bundle"]:
                bundle = directory / name
                (bundle / "zh-Hans.lproj").mkdir(parents=True, exist_ok=True)
                (bundle / "zh-Hans.lproj/Localizable.strings").write_text('"hello"="你好";')
            checkout = scratch / "checkouts/dependency"
            checkout.mkdir(parents=True, exist_ok=True)
            (checkout / "LICENSE").write_text("Dependency license")
            (checkout / "NOTICE.txt").write_text("Dependency notice")
            return types.SimpleNamespace(stdout=str(directory) + "\n")
        if argv[:2] == ["xcodebuild", "build"]:
            derived = Path(argv[argv.index("-derivedDataPath") + 1])
            configuration = argv[argv.index("-configuration") + 1]
            bundle = derived / "Build/Products" / configuration / "Example App.app"
            (bundle / "Contents/MacOS").mkdir(parents=True, exist_ok=True)
            (bundle / "Contents/Resources/en.lproj").mkdir(parents=True, exist_ok=True)
            (bundle / "Contents/MacOS/Example").write_bytes(b"xcode executable")
            (bundle / "Contents/Resources/en.lproj/Localizable.strings").write_text('"hello"="Hello";')
            (bundle / "Contents/Resources/Assets.car").write_bytes(b"compiled asset catalog")
            (bundle / "Contents/Info.plist").write_bytes(plistlib.dumps({
                "CFBundleExecutable": "Example", "CFBundleIdentifier": "dev.example.app",
                "CFBundlePackageType": "APPL", "NSPrincipalClass": "NSApplication"}))
        if argv[0] == "lipo":
            files = argv[2:argv.index("-output")]
            Path(argv[-1]).write_bytes(b"+".join(Path(p).read_bytes() for p in files))
        if argv[0] == "bash" and "app-sign" in argv:
            # The adapter reads the selected identity and team from its private work file.
            Path(argv[argv.index("app-sign") + 8]).write_text("-\n\n")
        if argv[0] == "hdiutil":
            Path(argv[-1]).write_bytes(b"final dmg")
        return types.SimpleNamespace(stdout="")

    def call(self, arguments):
        with patch.object(app, "execute", self.tool), patch.object(app, "execute_native", self.tool), contextlib.redirect_stdout(io.StringIO()):
            return app.main(arguments, root=self.root)

    def previous(self, package=False):
        output = self.root / ("dist/release/universal" if package else "dist/dev")
        bundle = output / ("app" if package else "") / "Example App.app"
        bundle.mkdir(parents=True)
        (bundle / "old-payload").write_bytes(b"keep")
        return output, bundle

    def use_xcode(self):
        (self.root / "Example.xcodeproj").mkdir()
        (self.root / "Example.entitlements").write_bytes(plistlib.dumps({"com.apple.security.app-sandbox": True}))
        (self.root / "tools").mkdir()
        for name in ["first.sh", "second.sh"]:
            (self.root / "tools" / name).write_text("#!/bin/bash\nexit 0\n")
        self.config.update(buildSystem="xcode", paths={"project": "Example.xcodeproj", "entitlements": "Example.entitlements"},
                           xcode={"scheme": "Example"}, testScripts=["tools/first.sh", "tools/second.sh"])
        self.write_config()

    def test_xcode_build_preserves_complete_bundle_and_signs_with_product_entitlements(self):
        self.use_xcode()
        with patch.dict(os.environ, {"GITHUB_ACTIONS": "true"}):
            self.call(["build", "--arch", "arm64", "--sign", "ad-hoc"])
        bundle = self.root / "dist/dev/Example App.app"
        self.assertEqual((bundle / "Contents/Resources/Assets.car").read_bytes(), b"compiled asset catalog")
        self.assertTrue((bundle / "Contents/Resources/en.lproj/Localizable.strings").is_file())
        self.assertEqual(plistlib.loads((bundle / "Contents/Info.plist").read_bytes())["NSPrincipalClass"], "NSApplication")
        build = next(c for c in self.calls if c[0] == "xcodebuild")
        self.assertIn("CODE_SIGNING_ALLOWED=NO", build)
        self.assertIn("SDK_STAT_CACHE_ENABLE=NO", build)
        # CLI signing owns the complete main executable. Xcode's debug dylib
        # shim leaves separately signed code and causes a real dyld Team mismatch.
        self.assertIn("ENABLE_DEBUG_DYLIB=NO", build)
        self.assertIn("MACOSX_DEPLOYMENT_TARGET=14.0", build)
        sign = next(c for c in self.calls if c[0] == "bash")
        self.assertEqual(sign[-1], str(self.root / "Example.entitlements"))
        self.assertFalse(any(c[0] == "swift" for c in self.calls))

    def test_xcode_universal_build_uses_both_architectures_and_common_package_manifest(self):
        self.use_xcode()
        manifest = types.SimpleNamespace(create_manifest=lambda *args: {"artifact": {"sha256": "b" * 64}})
        with patch.object(app, "import_local", return_value=manifest), patch.dict(os.environ, {"GITHUB_ACTIONS": "false"}):
            self.call(["package"])
        builds = [c for c in self.calls if c[0] == "xcodebuild"]
        self.assertEqual(len(builds), 1)
        self.assertIn("ARCHS=arm64 x86_64", builds[0])
        self.assertIn("ONLY_ACTIVE_ARCH=NO", builds[0])
        self.assertFalse(any(arg.startswith("SDK_STAT_CACHE_ENABLE=") for arg in builds[0]))
        self.assertEqual(builds[0][builds[0].index("-configuration") + 1], "Release")
        self.assertTrue((self.root / "dist/release/universal/Example-1.2.3-universal.dmg.sha256").is_file())

    def test_xcode_source_plist_is_validated_without_replacing_compiled_metadata(self):
        self.use_xcode()
        (self.root / "Resources").mkdir()
        source = self.root / "Resources/Info.plist"
        source.write_bytes(plistlib.dumps({"CFBundleShortVersionString": "MAYFLIES_APP_VERSION"}))
        self.config["packaging"]["infoPlist"] = "Resources/Info.plist"
        self.write_config()
        self.call(["build", "--arch", "arm64"])
        info = plistlib.loads((self.root / "dist/dev/Example App.app/Contents/Info.plist").read_bytes())
        self.assertEqual(info["NSPrincipalClass"], "NSApplication")
        self.assertEqual(info["CFBundleShortVersionString"], "1.2.3")
        source.write_text("broken plist")
        self.calls.clear()
        with self.assertRaises(plistlib.InvalidFileException):
            self.call(["build"])
        self.assertEqual(self.calls, [])

    def test_xcode_test_runs_only_declared_product_scripts_in_order(self):
        self.use_xcode()
        self.call(["test", "--arch", "arm64"])
        cache = f"TEST_MODULE_CACHE_PATH={self.root / '.build/app/debug/arm64/test-module-cache'}"
        self.assertEqual(self.calls, [["env", "TEST_ARCH=arm64", cache, "bash", str(self.root / "tools/first.sh")],
                                      ["env", "TEST_ARCH=arm64", cache, "bash", str(self.root / "tools/second.sh")]])

    def test_regression_scripts_receive_configuration_and_architecture_scoped_cache(self):
        self.use_xcode()
        (self.root / "tools/first.sh").write_text('printf "%s\\n%s\\n" "$TEST_ARCH" "$TEST_MODULE_CACHE_PATH" > "$1"\n')
        for configuration, arch in (("debug", "arm64"), ("release", "arm64"), ("debug", "x86_64")):
            with self.subTest(configuration=configuration, arch=arch):
                self.calls.clear()
                self.call(["test", "--configuration", configuration, "--arch", arch])
                expected = self.root / ".build/app" / configuration / arch / "test-module-cache"
                output = self.root / "received-env.txt"
                subprocess.run([*self.calls[0], str(output)], check=True)
                self.assertEqual(output.read_text().splitlines(), [arch, str(expected)])
                self.assertFalse(expected.exists())

    def test_xcode_compile_and_sign_failure_preserve_previous_running_app(self):
        self.use_xcode()
        _, previous = self.previous()
        for tool in ["xcodebuild", "bash"]:
            with self.subTest(tool=tool):
                self.calls.clear()
                self.fail_at = tool
                with self.assertRaises(subprocess.CalledProcessError):
                    self.call(["run", "--arch", "arm64"])
                self.assertEqual((previous / "old-payload").read_bytes(), b"keep")
                self.assertFalse(any("--stop" in c or "--launch" in c for c in self.calls))

    def test_xcode_rejects_wrong_bundle_identity_before_replacement(self):
        self.use_xcode()
        _, previous = self.previous()
        self.config["bundleIdentifier"] = "different.example.app"
        self.write_config()
        with self.assertRaisesRegex(ValueError, "identity"):
            self.call(["run", "--arch", "arm64"])
        self.assertEqual((previous / "old-payload").read_bytes(), b"keep")
        self.assertFalse(any(c[0] == "bash" or "--stop" in c for c in self.calls))

    def test_xcode_dry_run_performs_no_build_signing_or_filesystem_writes(self):
        self.use_xcode()
        before = {str(p.relative_to(self.root)): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        for command in ["build", "run", "test", "package"]:
            with patch.object(app, "execute", side_effect=AssertionError("dry run executed")), contextlib.redirect_stdout(io.StringIO()):
                result = app.main([command, "--dry-run"], root=self.root)
            self.assertTrue(result["commands"])
            self.assertEqual(before, {str(p.relative_to(self.root)): p.read_bytes() for p in self.root.rglob("*") if p.is_file()})

    def test_xcode_rejects_external_paths_and_unsafe_scheme_before_execution(self):
        self.use_xcode()
        for change in [{"paths": {"project": "../outside.xcodeproj"}}, {"xcode": {"scheme": "-evil"}},
                       {"testScripts": ["../other.sh"]}]:
            original = json.loads(json.dumps(self.config))
            self.config.update(change)
            self.write_config()
            with self.assertRaises(ValueError):
                self.call(["build"])
            self.config = original
        self.assertEqual(self.calls, [])

    def test_preserves_product_metadata_resources_licenses_and_all_swiftpm_bundles(self):
        resources = self.root / "Resources"
        (resources / "Markdown").mkdir(parents=True)
        (resources / "Markdown/sticky.js").write_text("local markdown")
        (resources / "AppIcon.icns").write_bytes(b"icon")
        info = {"CFBundleDisplayName": "Human Name", "NSAppTransportSecurity": {"NSAllowsArbitraryLoads": True}}
        (resources / "Info.plist").write_bytes(plistlib.dumps(info))
        self.config["packaging"].update(infoPlist="Resources/Info.plist", resources=["Resources/Markdown", "Resources/AppIcon.icns"],
                                        resourceBundles=["Example_Resources.bundle"], dependencyLicenses=["dependency"])
        self.write_config()
        self.call(["build", "--arch", "arm64", "--sign", "ad-hoc", "--version", "2.3.4-beta.1", "--build", "42"])
        bundle = self.root / "dist/dev/Example App.app"
        info = plistlib.loads((bundle / "Contents/Info.plist").read_bytes())
        self.assertEqual(info["CFBundleDisplayName"], "Human Name")
        self.assertTrue(info["NSAppTransportSecurity"]["NSAllowsArbitraryLoads"])
        self.assertEqual((info["CFBundleShortVersionString"], info["CFBundleVersion"]), ("2.3.4", "42"))
        for path in ["Markdown/sticky.js", "AppIcon.icns", "Example_Resources.bundle/zh-Hans.lproj/Localizable.strings",
                     "AutoDiscovered.bundle/zh-Hans.lproj/Localizable.strings", "Licenses/dependency.txt", "Licenses/dependency-NOTICE.txt"]:
            self.assertTrue((bundle / "Contents/Resources" / path).is_file(), path)

    def test_generated_info_honors_declared_metadata_but_rejects_identity_override(self):
        self.config["packaging"]["metadata"] = {"CFBundleLocalizations": ["en", "zh-Hans"], "CFBundleIconFile": "AppIcon"}
        self.write_config()
        self.call(["build", "--arch", "arm64"])
        info = plistlib.loads((self.root / "dist/dev/Example App.app/Contents/Info.plist").read_bytes())
        self.assertEqual(info["CFBundleLocalizations"], ["en", "zh-Hans"])
        self.assertEqual(info["CFBundleIconFile"], "AppIcon")
        self.config["packaging"]["metadata"]["CFBundleIdentifier"] = "foreign.app"
        self.write_config()
        self.calls.clear()
        with self.assertRaisesRegex(ValueError, "cannot override"):
            self.call(["build"])
        self.assertEqual(self.calls, [])

    def test_universal_uses_independent_architecture_builds_and_lipo(self):
        self.call(["build", "--arch", "universal", "--sign", "ad-hoc"])
        builds = [c for c in self.calls if c[:2] == ["swift", "build"] and "--show-bin-path" not in c]
        self.assertEqual(len(builds), 2)
        self.assertNotEqual(builds[0][builds[0].index("--scratch-path") + 1], builds[1][builds[1].index("--scratch-path") + 1])
        binary = self.root / "dist/dev/Example App.app/Contents/MacOS/Example"
        self.assertEqual(binary.read_bytes(), b"arm64+x86_64")

    def test_compile_and_sign_failure_keep_previous_app_and_do_not_stop_it(self):
        _, previous = self.previous()
        for tool in ["swift", "bash"]:
            with self.subTest(tool=tool):
                self.calls.clear()
                self.fail_at = tool
                with self.assertRaises(subprocess.CalledProcessError):
                    self.call(["run", "--arch", "arm64"])
                self.assertEqual((previous / "old-payload").read_bytes(), b"keep")
                self.assertFalse(any("--stop" in c or "--launch" in c for c in self.calls))

    def test_missing_required_bundle_keeps_previous_app(self):
        _, previous = self.previous()
        self.config["packaging"]["resourceBundles"] = ["Missing.bundle"]
        self.write_config()
        with self.assertRaisesRegex(ValueError, "Required SwiftPM"):
            self.call(["build", "--arch", "arm64"])
        self.assertEqual((previous / "old-payload").read_bytes(), b"keep")

    def test_run_stops_exact_output_only_after_build_and_passes_app_arguments(self):
        self.previous()
        self.call(["run", "--arch", "arm64", "--", "--fixture", "value with spaces"])
        lifecycle = [c for c in self.calls if "--stop" in c or "--launch" in c]
        path = str(self.root / "dist/dev/Example App.app")
        self.assertEqual(lifecycle, [["swift", str(app.LIB / "RunApp.swift"), "--stop", path],
                                     ["swift", str(app.LIB / "RunApp.swift"), "--launch", path, "--", "--fixture", "value with spaces"]])
        self.assertLess(next(i for i,c in enumerate(self.calls) if c[0] == "bash"), self.calls.index(lifecycle[0]))

    def test_every_local_dry_run_executes_nothing_and_changes_no_files(self):
        def snapshot():
            return {str(p.relative_to(self.root)): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        before = snapshot()
        for command in ["build", "run", "test", "package"]:
            with self.subTest(command=command), patch.object(app, "execute", side_effect=AssertionError("dry-run executed")), patch.object(subprocess, "run", side_effect=AssertionError("subprocess executed")), contextlib.redirect_stdout(io.StringIO()):
                result = app.main([command, "--dry-run"], root=self.root)
                self.assertTrue(result["commands"])
                self.assertEqual(snapshot(), before)
                self.assertFalse((self.root / "dist").exists())
                self.assertFalse((self.root / ".build").exists())

    def test_test_command_is_only_swift_test(self):
        self.call(["test", "--arch", "arm64"])
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0][:2], ["swift", "test"])
        self.assertNotIn("--product", self.calls[0])

    def test_swiftpm_offline_regressions_follow_primary_tests_in_declared_order(self):
        (self.root / "tools").mkdir()
        for name in ("wrapper.sh", "fixture.py"):
            (self.root / "tools" / name).write_text("# isolated regression\n")
        self.config["testScripts"] = ["tools/wrapper.sh", "tools/fixture.py"]
        self.write_config()
        self.call(["test", "--arch", "arm64"])
        self.assertEqual(self.calls[0][:2], ["swift", "test"])
        cache = f"TEST_MODULE_CACHE_PATH={self.root / '.build/app/debug/arm64/test-module-cache'}"
        self.assertEqual(self.calls[1:], [
            ["env", "TEST_ARCH=arm64", cache, "bash", str(self.root / "tools/wrapper.sh")],
            ["env", "TEST_ARCH=arm64", cache, "python3", "-B", str(self.root / "tools/fixture.py")]])

    def test_offline_regression_failure_stops_later_checks_and_propagates(self):
        (self.root / "tools").mkdir()
        (self.root / "tools/fail.sh").write_text("#!/bin/bash\nexit 17\n")
        (self.root / "tools/later.py").write_text("from pathlib import Path\nPath('unexpected').write_text('ran')\n")
        self.config["testScripts"] = ["tools/fail.sh", "tools/later.py"]
        self.write_config()
        original_execute = app.execute
        def run(argv, root, capture=False):
            if list(map(str, argv))[:2] == ["swift", "test"]:
                return types.SimpleNamespace(returncode=0)
            return original_execute(argv, root, capture)
        with patch.object(app, "execute", run), self.assertRaises(subprocess.CalledProcessError) as error:
            app.main(["test", "--arch", "arm64"], root=self.root)
        self.assertEqual(error.exception.returncode, 17)
        self.assertFalse((self.root / "unexpected").exists())

    def test_offline_regressions_reject_invalid_or_foreign_scripts_before_running(self):
        (self.root / "tools").mkdir()
        (self.root / "tools/unsupported.rb").write_text("exit 0\n")
        for scripts in ("tools/check.sh", ["../outside.sh"], ["tools/missing.sh"], ["tools/unsupported.rb"]):
            with self.subTest(scripts=scripts):
                self.config["testScripts"] = scripts
                self.write_config()
                with self.assertRaises(ValueError):
                    self.call(["test"])
                self.assertEqual(self.calls, [])

    def test_native_acceptance_does_not_run_default_offline_regressions(self):
        bundle, output = self.native_fixture()
        (self.root / "tools/offline.sh").write_text("exit 17\n")
        self.config["testScripts"] = ["tools/offline.sh"]
        self.write_config()
        self.call(["test", "--native", "--app", str(bundle), "--output", str(output)])
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0][0], "bash")
        self.assertNotIn(str(self.root / "tools/offline.sh"), self.calls[0])

    def native_fixture(self):
        script = self.root / "tools/localization.sh"
        script.parent.mkdir()
        script.write_text("#!/bin/bash\nexit 0\n")
        self.config["nativeTests"] = {"localizationScript": "tools/localization.sh"}
        self.write_config()
        bundle = self.root / "prebuilt/Example App.app"
        binary = bundle / "Contents/MacOS/Example"
        binary.parent.mkdir(parents=True)
        binary.write_bytes(b"prebuilt")
        binary.chmod(0o755)
        (bundle / "Contents/Info.plist").write_bytes(plistlib.dumps({
            "CFBundlePackageType": "APPL", "CFBundleIdentifier": "dev.example.app",
            "CFBundleExecutable": "Example"}))
        output = self.root / "dist/localization"
        output.parent.mkdir()
        return bundle, output

    def test_native_localization_routes_prebuilt_bundle_without_build_or_lifecycle(self):
        bundle, output = self.native_fixture()
        result = self.call(["test", "--native", "--case", "localization", "--app", str(bundle),
                            "--output", str(output), "--timeout-seconds", "45"])
        expected = ["bash", str(self.root / "tools/localization.sh"), "--app", str(bundle),
                    "--output", str(output), "--case", "localization", "--timeout-seconds", "45.0"]
        self.assertEqual(self.calls, [expected])
        self.assertEqual(result["commands"], [expected])
        self.assertFalse(output.exists())  # The product runner reserves fresh evidence output.
        self.assertEqual((bundle / "Contents/MacOS/Example").read_bytes(), b"prebuilt")

    def test_native_localization_rejects_foreign_bundle_and_reused_output_before_execution(self):
        bundle, output = self.native_fixture()
        info = bundle / "Contents/Info.plist"
        original = info.read_bytes()
        values = plistlib.loads(original)
        values["CFBundleIdentifier"] = "foreign.app"
        info.write_bytes(plistlib.dumps(values))
        arguments = ["test", "--native", "--app", str(bundle), "--output", str(output)]
        with self.assertRaisesRegex(ValueError, "identity"):
            self.call(arguments)
        self.assertEqual(self.calls, [])
        info.write_bytes(original)
        output.mkdir(parents=True)
        (output / "manifest.json").write_text("previous evidence")
        with self.assertRaisesRegex(ValueError, "new or empty"):
            self.call(arguments)
        self.assertEqual((output / "manifest.json").read_text(), "previous evidence")
        self.assertEqual(self.calls, [])

    def test_native_localization_requires_explicit_paths_and_rejects_invalid_timeout(self):
        bundle, output = self.native_fixture()
        for options in (["--app", str(bundle)], ["--output", str(output)],
                        ["--app", "relative.app", "--output", str(output)],
                        ["--app", str(bundle), "--output", str(output), "--timeout-seconds", "nan"],
                        ["--app", str(bundle), "--output", str(output), "--timeout-seconds", "0"]):
            with self.subTest(options=options), self.assertRaises(ValueError):
                self.call(["test", "--native", *options])
        self.assertEqual(self.calls, [])

    def test_native_dry_run_is_read_only_and_runner_failure_is_propagated(self):
        bundle, output = self.native_fixture()
        arguments = ["test", "--native", "--app", str(bundle), "--output", str(output)]
        self.call([*arguments, "--dry-run"])
        self.assertEqual(self.calls, [])
        self.assertFalse(output.exists())
        self.fail_at = "bash"
        with self.assertRaises(subprocess.CalledProcessError):
            self.call(arguments)
        self.assertEqual(len(self.calls), 1)
        self.assertFalse(output.exists())

    def test_native_cli_cancellation_waits_for_runner_restoration(self):
        self.check_native_cancellation(signal.SIGTERM)

    def test_native_cli_ctrl_c_waits_for_runner_restoration(self):
        self.check_native_cancellation(signal.SIGINT)

    def check_native_cancellation(self, signum):
        bundle, output = self.native_fixture()
        helper = self.root / "tools/native_fixture.py"
        helper.write_text('''import json,signal,sys,time
from pathlib import Path
output=Path(sys.argv[1]); output.mkdir()
def restore(signum,frame):
    time.sleep(.6)
    (output/'restored.json').write_text(json.dumps({'restored': True}))
    sys.exit(0)
signal.signal(signal.SIGTERM,restore)
(output/'ready.json').write_text('{}')
while True: time.sleep(.05)
''')
        (self.root / "tools/localization.sh").write_text(
            '#!/bin/bash\nexec ' + shlex.quote(sys.executable) + ' ' + shlex.quote(str(helper)) + ' "$4"\n')
        source = Path(app.__file__)
        arguments = ["test", "--native", "--app", str(bundle), "--output", str(output)]
        program = ("import importlib.util,sys; "
                   f"s=importlib.util.spec_from_file_location('cli',{str(source)!r}); "
                   "m=importlib.util.module_from_spec(s);s.loader.exec_module(m); "
                   f"m.main({arguments!r},root=m.Path({str(self.root)!r}))")
        process = subprocess.Popen([sys.executable, "-B", "-c", program], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            deadline = time.monotonic() + 5
            while not (output / "ready.json").exists() and process.poll() is None and time.monotonic() < deadline:
                time.sleep(.02)
            self.assertTrue((output / "ready.json").exists())
            process.send_signal(signum)
            self.assertNotEqual(process.wait(timeout=5), 0)
            self.assertEqual(json.loads((output / "restored.json").read_text()), {"restored": True})
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=5)

    def test_failed_package_retains_previous_complete_release(self):
        output, previous = self.previous(package=True)
        (output / "Example-1.2.3-universal.dmg").write_bytes(b"old dmg")
        self.fail_at = "hdiutil"
        with self.assertRaises(subprocess.CalledProcessError):
            self.call(["package"])
        self.assertEqual((previous / "old-payload").read_bytes(), b"keep")
        self.assertEqual((output / "Example-1.2.3-universal.dmg").read_bytes(), b"old dmg")

    def test_signed_previous_package_refusal_has_safe_actionable_error(self):
        output, previous = self.previous(package=True)
        (output / "Example-1.2.3-universal.dmg").write_bytes(b"old signed dmg")
        # Exact guard emitted by signing.sh when the existing output is
        # Developer ID and the new default package request is ad-hoc.
        refusal = "Refusing to replace a Developer ID app with ad hoc or development signing. Use a separate development output directory."
        secret = "fixture-password-must-not-appear"
        real_mock_tool = self.tool
        def tool(argv, root, capture=False):
            if argv[0] == "bash":
                raise subprocess.CalledProcessError(1, ["security", "import", "-P", secret], stderr=refusal + "\n" + secret)
            return real_mock_tool(argv, root, capture)
        with patch.object(app, "execute", tool), contextlib.redirect_stdout(io.StringIO()), self.assertRaisesRegex(RuntimeError, "existing output uses Developer ID") as failure:
            app.main(["package"], root=self.root)
        self.assertIn("--output", str(failure.exception))
        self.assertIn("--sign developer-id", str(failure.exception))
        self.assertNotIn(secret, str(failure.exception))
        self.assertEqual((previous / "old-payload").read_bytes(), b"keep")
        self.assertEqual((output / "Example-1.2.3-universal.dmg").read_bytes(), b"old signed dmg")

    def test_other_fixed_signing_guards_are_safe_and_unknown_stderr_is_not_echoed(self):
        secret = "fixture-private-certificate-name"
        for guard, expected in [("Signing team mismatch", "MACOS_TEAM_ID"),
                                ("Multiple signing identities match; specify a full name or SHA-1:", "MACOS_SIGNING_IDENTITY"),
                                ("New app does not satisfy the previous signing identity; refusing to replace it.", "--output")]:
            with self.subTest(guard=guard):
                failure = subprocess.CalledProcessError(1, ["codesign", secret], stderr=guard + "\n" + secret)
                with patch.object(app, "execute", side_effect=failure), self.assertRaises(RuntimeError) as caught:
                    app.sign_app(self.root, self.config, types.SimpleNamespace(sign="auto"), self.root / "staged.app", self.root / "previous.app", self.root / "state")
                self.assertIn(expected, str(caught.exception))
                self.assertNotIn(secret, str(caught.exception))
        failure = subprocess.CalledProcessError(1, ["security", "-P", secret], stderr="Unknown failure: " + secret)
        with patch.object(app, "execute", side_effect=failure), self.assertRaises(subprocess.CalledProcessError) as caught:
            app.sign_app(self.root, self.config, types.SimpleNamespace(sign="auto"), self.root / "staged.app", self.root / "previous.app", self.root / "state")
        self.assertIs(caught.exception, failure)

    def test_package_manifest_and_sidecar_describe_final_signed_dmg(self):
        def manifest(config, root, bundle, dmg, version, build, arch, signed, team):
            self.assertEqual(dmg.read_bytes(), b"final dmg")
            self.assertEqual(bundle.name, "Example App.app")
            self.assertEqual((version, build, arch, signed), ("1.2.3", "1", "universal", False))
            return {"artifact": {"sha256": "a" * 64}}
        with patch.object(app, "import_local", return_value=types.SimpleNamespace(create_manifest=manifest)):
            self.call(["package"])
        output = self.root / "dist/release/universal"
        self.assertTrue((output / "app/Example App.app/Contents/Info.plist").is_file())
        self.assertEqual((output / "Example-1.2.3-universal.dmg.sha256").read_text(), "a" * 64 + "  Example-1.2.3-universal.dmg\n")
        self.assertTrue(any(c[0] == "codesign" and c[-1].endswith(".dmg") for c in self.calls))

    def test_release_forwards_all_arguments_including_dry_run_to_pipeline(self):
        captured = []
        pipeline = types.SimpleNamespace(main=lambda args: captured.append(args))
        with patch.object(app, "import_local", return_value=pipeline), patch.object(app, "execute", side_effect=AssertionError("tool executed")):
            app.main(["release", "--version", "1.2.3", "--notarize", "--dry-run"], root=self.root)
        self.assertEqual(captured, [["--version", "1.2.3", "--notarize", "--dry-run"]])

    def test_release_after_options_is_rejected_without_executing_tools(self):
        with patch.object(app, "execute") as execute, contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as rejected:
            app.main(["--version", "0.1.0", "release"], root=self.root)
        self.assertEqual(rejected.exception.code, 2)
        execute.assert_not_called()
        self.assertFalse((self.root / "dist").exists())

    def test_failed_final_rename_restores_previous_bytes(self):
        _, previous = self.previous()
        staged = self.root / "candidate.app"
        staged.mkdir()
        (staged / "new-payload").write_bytes(b"new")
        original = Path.rename
        def rename(path, destination):
            if path == staged:
                raise OSError("Simulated final rename failure")
            return original(path, destination)
        with patch.object(Path, "rename", rename), self.assertRaises(OSError):
            app.replace_atomically(staged, previous)
        self.assertEqual((previous / "old-payload").read_bytes(), b"keep")
        self.assertEqual((staged / "new-payload").read_bytes(), b"new")

    def test_invalid_version_or_destructive_output_runs_nothing(self):
        for arguments in [["build", "--version", "01.2.3"], ["package", "--output", str(self.root)],
                          ["build", "--output", str(self.root / "Sources")]]:
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                self.call(arguments)
        self.assertEqual(self.calls, [])
        self.assertFalse((self.root / "dist").exists())

    def test_cli_external_failure_does_not_print_secret_argv(self):
        # Exercise the actual executable exception boundary with a mocked failing
        # pipeline; no subprocess, compiler, signing or remote tool is started.
        secret = "fixture-private-password-do-not-print"
        pipeline = types.SimpleNamespace(main=lambda argv: (_ for _ in ()).throw(subprocess.CalledProcessError(1, ["security", "import", "-P", secret], stderr="Unknown private failure: " + secret)))
        stderr = io.StringIO()
        source = Path(app.__file__).read_text()
        environment = {"__name__": "__main__", "__file__": app.__file__}
        with patch.object(importlib.util, "module_from_spec", return_value=pipeline), patch.object(importlib.util, "spec_from_file_location", return_value=types.SimpleNamespace(loader=types.SimpleNamespace(exec_module=lambda module: None))), patch.object(sys, "argv", ["app.py", "release"]), contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit) as stopped:
            exec(compile(source, app.__file__, "exec"), environment)
        self.assertEqual(stopped.exception.code, 1)
        self.assertEqual(stderr.getvalue(), "app: External command failed\n")
        self.assertNotIn(secret, stderr.getvalue())


@unittest.skipUnless(sys.platform == "darwin" and shutil.which("swiftc"), "macOS Swift recorder acceptance")
class LocalizationRecorderAcceptance(unittest.TestCase):
    """Exercise real child termination and CFPreferences without opening apps."""

    @classmethod
    def setUpClass(cls):
        cls.tools_temporary = tempfile.TemporaryDirectory(prefix="localization-recorder-tools-")
        cls.tools = Path(cls.tools_temporary.name).resolve()
        cls.recorder = cls.tools / "recorder"
        cls.child = cls.tools / "child"
        child_source = cls.tools / "Child.swift"
        child_source.write_text('''import Foundation
import CoreFoundation
import Darwin
let args = Array(CommandLine.arguments.dropFirst())
if args[0] == "preferences" {
    let domain = args[1] as CFString
    if args[2] == "set" {
        CFPreferencesSetValue("sentinel" as CFString, "preserve" as CFString, domain, kCFPreferencesCurrentUser, kCFPreferencesAnyHost)
        guard CFPreferencesSynchronize(domain, kCFPreferencesCurrentUser, kCFPreferencesAnyHost) else { exit(2) }
    }
    let keys = CFPreferencesCopyKeyList(domain, kCFPreferencesCurrentUser, kCFPreferencesAnyHost) as? [String] ?? []
    print(String(decoding: try JSONSerialization.data(withJSONObject: keys.sorted()), as: UTF8.self))
    exit(0)
}
let env = ProcessInfo.processInfo.environment
let suite = env["MACOS_LOCALIZATION_TEST_SUITE"]!
let directory = env["MACOS_LOCALIZATION_TEST_DIRECTORY"]!
let progress = env["MACOS_LOCALIZATION_TEST_PROGRESS"]!
CFPreferencesSetValue("language" as CFString, "en" as CFString, suite as CFString, kCFPreferencesCurrentUser, kCFPreferencesAnyHost)
guard CFPreferencesSynchronize(suite as CFString, kCFPreferencesCurrentUser, kCFPreferencesAnyHost) else { exit(3) }
try Data("isolated notes".utf8).write(to: URL(fileURLWithPath: directory).appendingPathComponent("notes.sqlite"))
let checkpoint: [String: Any] = ["stage": "english-visible", "pid": getpid(), "suite": suite, "directory": directory]
try JSONSerialization.data(withJSONObject: checkpoint).write(to: URL(fileURLWithPath: progress), options: .atomic)
if args[0] == "fail" { exit(7) }
try Data("{\\"passed\\":true}".utf8).write(to: URL(fileURLWithPath: args[1]), options: .atomic)
if args[0] == "hold" {
    // Force the recorder's bounded child-only SIGKILL path, after real writes.
    signal(SIGTERM, SIG_IGN)
    while true { Thread.sleep(forTimeInterval: 0.05) }
}
''')
        recorder_source = Path(app.__file__).parent / "lib/VerificationEvidence.swift"
        for source, binary in [(recorder_source, cls.recorder), (child_source, cls.child)]:
            result = subprocess.run(["swiftc", "-module-cache-path", str(cls.tools / "module-cache"), str(source), "-o", str(binary)],
                                    text=True, capture_output=True, timeout=120)
            if result.returncode:
                cls.tools_temporary.cleanup()
                raise RuntimeError(result.stderr)

    @classmethod
    def tearDownClass(cls):
        cls.tools_temporary.cleanup()

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="localization-recorder-case-")
        self.root = Path(self.temporary.name).resolve()
        self.repo = self.root / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "--quiet", str(self.repo)], check=True, capture_output=True)
        (self.repo / "source.txt").write_text("unchanged source")
        subprocess.run(["git", "-C", str(self.repo), "add", "source.txt"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(self.repo), "-c", "user.name=Recorder Test", "-c", "user.email=recorder-test@example.invalid",
                        "commit", "--quiet", "-m", "fixture"], check=True, capture_output=True)
        self.bundle = self.root / "Fixture.app"
        binary = self.bundle / "Contents/MacOS/Fixture"
        binary.parent.mkdir(parents=True)
        shutil.copy2(self.child, binary)
        (self.bundle / "Contents/Info.plist").write_bytes(plistlib.dumps({
            "CFBundlePackageType": "APPL", "CFBundleIdentifier": "dev.example.app", "CFBundleExecutable": "Fixture"}))
        self.output = self.root / "evidence"
        self.suite = "dev.example.app.localization-test." + hashlib.sha256(str(self.output).encode()).hexdigest()
        self.sibling = self.root / "user-data.sqlite"
        self.sibling.write_bytes(b"unrelated user data")
        self.process = None

    def tearDown(self):
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            self.process.wait(timeout=10)
        progress = self.output / "localization-progress.json"
        if progress.is_file():
            pid = json.loads(progress.read_text())["pid"]
            expected = f"{self.child} hold {self.output / 'localization-flow.json'}"
            observed = subprocess.run(["/bin/ps", "-ww", "-p", str(pid), "-o", "command="],
                                      capture_output=True, text=True, timeout=5)
            # If a future regression leaves the stubborn fixture alive, clean up
            # only this test's exact executable and report argument, never a PID
            # that has already been reused for another process.
            if observed.returncode == 0 and observed.stdout.strip() == expected:
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        # defaults delete asks the preferences daemon to remove this exact owned
        # domain. A CFPreferences sync followed by unlink can race a later flush.
        subprocess.run(["/usr/bin/defaults", "delete", self.suite],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
        # This fresh suite was created by this case, including the collision
        # fixture. Keep failed cleanup regressions from leaving empty plists.
        preference_file = Path.home() / "Library/Preferences" / (self.suite + ".plist")
        try:
            metadata = os.lstat(preference_file)
        except FileNotFoundError:
            pass
        else:
            self.assertTrue(stat.S_ISREG(metadata.st_mode), "owned test preference file must be regular")
            preference_file.unlink()
        self.temporary.cleanup()

    def preferences(self, action="read"):
        result = subprocess.run([str(self.child), "preferences", self.suite, action],
                                check=True, capture_output=True, text=True, timeout=10)
        return json.loads(result.stdout)

    def start(self, mode, timeout=10):
        report = self.output / "localization-flow.json"
        arguments = [str(self.recorder), "--project", "fixture", "--repo", str(self.repo), "--app", str(self.bundle),
                     "--output", str(self.output), "--report", str(report), "--success-field", "passed",
                     "--expected-bundle-id", "dev.example.app", "--localization-cleanup", "true",
                     "--timeout-seconds", str(timeout), "--termination-grace-seconds", "0.15", "--", str(self.child), mode, str(report)]
        self.process = subprocess.Popen(arguments, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                        start_new_session=True)
        return self.process

    def checkpoint(self):
        progress = self.output / "localization-progress.json"
        deadline = time.monotonic() + 15
        while not progress.exists() and self.process.poll() is None and time.monotonic() < deadline:
            time.sleep(.02)
        self.assertTrue(progress.is_file(), "fixture must mutate isolated data before cancellation")
        return json.loads(progress.read_text())

    def assert_cleanup(self, successful):
        result = self.process.wait(timeout=15)
        self.assertEqual(result == 0, successful)
        manifest = json.loads((self.output / "manifest.json").read_text())
        self.assertEqual(manifest["status"], "passed" if successful else "failed")
        fixture = manifest["localizationFixture"]
        self.assertEqual(fixture["suite"], self.suite)
        self.assertEqual(fixture["directory"], str(self.output / "fixture-data"))
        self.assertEqual(fixture["progress"], str(self.output / "localization-progress.json"))
        for key in ["preferencesEmpty", "preferencesSynchronized", "preferencesFileAbsent", "directoryAbsent", "passed"]:
            self.assertIs(fixture[key], True, (key, manifest))
        self.assertEqual(fixture["errors"], [])
        checkpoint = self.checkpoint()
        self.assertEqual(checkpoint["suite"], self.suite)
        self.assertEqual(checkpoint["directory"], fixture["directory"])
        with self.assertRaises(ProcessLookupError):
            os.kill(checkpoint["pid"], 0)
        self.assertFalse((self.output / "fixture-data").exists())
        # Immediate file absence misses a delayed empty-plist write from the
        # preferences daemon. Check durable cleanup before an independent read.
        time.sleep(1)
        with self.assertRaises(FileNotFoundError):
            os.lstat(Path.home() / "Library/Preferences" / (self.suite + ".plist"))
        self.assertEqual(self.preferences(), [])
        with self.assertRaises(FileNotFoundError):
            os.lstat(Path.home() / "Library/Preferences" / (self.suite + ".plist"))
        self.assertEqual(self.sibling.read_bytes(), b"unrelated user data")
        return manifest

    def test_normal_completion_removes_owned_preferences_and_data_only(self):
        self.start("normal")
        self.assert_cleanup(successful=True)

    def test_missing_report_failure_still_removes_owned_preferences_and_data(self):
        self.start("fail")
        manifest = self.assert_cleanup(successful=False)
        self.assertFalse((self.output / "localization-flow.json").exists())
        self.assertEqual(manifest["command"]["exitCode"], 7)

    def test_timeout_removes_data_after_stubborn_owned_child_is_gone(self):
        self.start("hold", timeout=1)
        manifest = self.assert_cleanup(successful=False)
        self.assertIs(manifest["command"]["timedOut"], True)

    def test_ctrl_c_removes_data_without_promoting_preexisting_passed_report(self):
        self.check_interruption(signal.SIGINT)

    def test_sigterm_removes_data_without_promoting_preexisting_passed_report(self):
        self.check_interruption(signal.SIGTERM)

    def test_terminal_ctrl_c_to_owned_process_group_removes_isolated_data(self):
        self.check_interruption(signal.SIGINT, process_group=True)

    def check_interruption(self, signum, process_group=False):
        self.start("hold")
        self.checkpoint()
        # The fixture already wrote a successful report. Interruption must win.
        deadline = time.monotonic() + 2
        while not (self.output / "localization-flow.json").exists() and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertTrue((self.output / "localization-flow.json").exists())
        if process_group:
            # start_new_session isolates the test from this Python process and
            # all user apps. This models a terminal's Ctrl-C delivery.
            self.assertEqual(os.getpgid(self.process.pid), self.process.pid)
            os.killpg(self.process.pid, signum)
        else:
            self.process.send_signal(signum)
        manifest = self.assert_cleanup(successful=False)
        self.assertIs(manifest["command"]["interrupted"], True)

    def test_existing_isolated_suite_is_rejected_without_deleting_it(self):
        self.assertEqual(self.preferences("set"), ["sentinel"])
        self.start("normal")
        self.assertNotEqual(self.process.wait(timeout=15), 0)
        manifest = json.loads((self.output / "manifest.json").read_text())
        self.assertFalse(manifest["command"]["started"])
        self.assertFalse((self.output / "localization-progress.json").exists())
        self.assertEqual(self.preferences(), ["sentinel"])
        self.assertEqual(self.sibling.read_bytes(), b"unrelated user data")


if __name__ == "__main__":
    unittest.main()
