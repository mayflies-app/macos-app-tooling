"""Release preview and execution boundaries; external services are never called."""
import contextlib
import importlib.util
import io
import json
import os
import subprocess
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.dont_write_bytecode = True
spec = importlib.util.spec_from_file_location('pipeline', Path(__file__).parents[1] / 'scripts/lib/pipeline.py')
pipeline = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pipeline)
COMMIT = 'a' * 40


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.config = {'schemaVersion': 1, 'product': 'SampleApp', 'appName': 'Sample App',
                       'executable': 'SampleApp', 'bundleIdentifier': 'dev.sample.app',
                       'repository': 'mayflies-app/sample-app', 'caskToken': 'sample-app',
                       'description': 'Sample', 'minimumSystemVersion': '14.0',
                       'buildSystem': 'swiftpm', 'architectures': ['arm64', 'x86_64', 'universal'],
                       'versionFile': 'VERSION', 'releaseAdapter': 'scripts/app.py'}
        (self.root / 'macos-app.json').write_text(json.dumps(self.config))
        (self.root / 'VERSION').write_text('1.2.3\n')

    def tearDown(self):
        self.temp.cleanup()

    def call(self, argv):
        with contextlib.redirect_stdout(io.StringIO()):
            return pipeline.main(argv, root=self.root)

    def test_default_and_explicit_preview_never_run_tools_or_write(self):
        before = {str(p): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        for args in ([], ['--dry-run'], ['--dry-run', '--publish']):
            with self.subTest(args=args), patch.object(pipeline.contract, 'run', side_effect=AssertionError('tool called')), \
                 patch.object(pipeline.release, 'github_json', side_effect=AssertionError('GitHub called')), \
                 patch.object(pipeline, 'execute', side_effect=AssertionError('execution called')):
                result = self.call(args)
                self.assertFalse(result['executed'])
                self.assertFalse(result['credentialsValidated'])
                self.assertEqual([s['architecture'] for s in result['steps'] if s['step'] == 'package'],
                                 ['universal'])
                for step in ('notarize', 'staple', 'verify-final-artifact'):
                    self.assertEqual([s['architecture'] for s in result['steps'] if s['step'] == step], ['universal'])
                self.assertEqual('update-cask' in [s['step'] for s in result['steps']], '--publish' in args)
                if '--publish' in args:
                    tap = next(s for s in result['steps'] if s['step'] == 'update-cask')
                    self.assertEqual(tap['repository'], 'mayflies-app/homebrew-tap')
        self.assertEqual(before, {str(p): p.read_bytes() for p in self.root.rglob('*') if p.is_file()})

    def test_local_execute_rejected_before_any_tool_or_output(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(pipeline.contract, 'run') as run:
            with self.assertRaisesRegex(ValueError, 'Remote operation'):
                self.call(['--execute'])
            run.assert_not_called()
        self.assertFalse((self.root / 'dist').exists())

    def test_execute_and_dry_run_are_mutually_exclusive(self):
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()), patch.object(pipeline, 'execute') as execute:
            self.call(['--execute', '--dry-run'])
        execute.assert_not_called()

    def test_private_publication_rejected_before_build_sign_or_apple(self):
        env = {'GITHUB_ACTIONS': 'true', 'GITHUB_EVENT_NAME': 'workflow_dispatch', 'GH_TOKEN': 'fixture', 'GITHUB_SHA': COMMIT}
        with patch.dict(os.environ, env, clear=True), \
             patch.object(pipeline.contract, 'source_state', return_value={'commit': COMMIT, 'dirty': False}), \
             patch.object(pipeline.release, 'github_json', return_value={'visibility': 'private'}), \
             patch.object(pipeline.ci_prepare, 'prepare') as prepare, \
             patch.object(pipeline.ci_prepare, 'verify_toolchain') as toolchain:
            with self.assertRaisesRegex(ValueError, 'public release repository'):
                self.call(['--execute', '--publish'])
            prepare.assert_not_called()
            toolchain.assert_not_called()

    def test_dirty_source_rejected_before_apple_or_publication(self):
        env = {'GITHUB_ACTIONS': 'true', 'GITHUB_EVENT_NAME': 'workflow_dispatch', 'GH_TOKEN': 'fixture', 'GITHUB_SHA': COMMIT}
        with patch.dict(os.environ, env, clear=True), \
             patch.object(pipeline.contract, 'source_state', return_value={'commit': COMMIT, 'dirty': True}), \
             patch.object(pipeline.release, 'github_json') as github, \
             patch.object(pipeline.ci_prepare, 'prepare') as prepare:
            with self.assertRaisesRegex(ValueError, 'clean checkout'):
                self.call(['--execute', '--publish'])
            github.assert_not_called()
            prepare.assert_not_called()

    def test_signed_private_artifact_execution_has_no_publication(self):
        env = {'GITHUB_ACTIONS': 'true', 'GITHUB_EVENT_NAME': 'workflow_dispatch', 'GH_TOKEN': 'fixture', 'GITHUB_SHA': COMMIT}
        with patch.dict(os.environ, env, clear=True), \
             patch.object(pipeline.contract, 'source_state', return_value={'commit': COMMIT, 'dirty': False}), \
             patch.object(pipeline.ci_prepare, 'verify_toolchain'), \
             patch.object(pipeline.release, 'main', return_value={'reuse': False}), \
             patch.object(pipeline.ci_prepare, 'prepare') as prepare, \
             patch.object(pipeline.contract, 'collect'), \
             patch.object(pipeline.release, 'publish') as publish, \
             patch.object(pipeline.tap_update, 'remote_update') as tap:
            self.call(['--execute', '--build', '42'])
            self.assertEqual([c.args[3] for c in prepare.call_args_list], ['universal'])
            self.assertTrue(all(c.kwargs == {'signed': True, 'notarized': True} for c in prepare.call_args_list))
            self.assertTrue(all(c.args[2] == '42' for c in prepare.call_args_list))
            publish.assert_not_called()
            tap.assert_not_called()

    def test_universal_prepare_failure_stops_collection_and_publication(self):
        env = {'GITHUB_ACTIONS': 'true', 'GITHUB_EVENT_NAME': 'workflow_dispatch', 'GH_TOKEN': 'fixture', 'GITHUB_SHA': COMMIT}
        with patch.dict(os.environ, env, clear=True), \
             patch.object(pipeline.contract, 'source_state', return_value={'commit': COMMIT, 'dirty': False}), \
             patch.object(pipeline.release, 'github_json', return_value={'visibility': 'public'}), \
             patch.object(pipeline.ci_prepare, 'verify_toolchain'), \
             patch.object(pipeline.release, 'main', return_value={'reuse': False}), \
             patch.object(pipeline.ci_prepare, 'prepare', side_effect=ValueError('fixture package failure')) as prepare, \
             patch.object(pipeline.contract, 'collect') as collect, \
             patch.object(pipeline.release, 'publish') as publish, \
             patch.object(pipeline.tap_update, 'remote_update') as tap:
            with self.assertRaisesRegex(ValueError, 'fixture package failure'):
                self.call(['--execute', '--publish'])
            self.assertEqual(prepare.call_args.args[3], 'universal')
            collect.assert_not_called()
            publish.assert_not_called()
            tap.assert_not_called()

    def test_new_publication_uses_publish_proof_for_cask_without_reverification(self):
        env = {'GITHUB_ACTIONS': 'true', 'GITHUB_EVENT_NAME': 'workflow_dispatch', 'GH_TOKEN': 'app-token',
               'HOMEBREW_TAP_TOKEN': 'tap-token', 'GITHUB_SHA': COMMIT}
        with patch.dict(os.environ, env, clear=True), \
             patch.object(pipeline.contract, 'source_state', return_value={'commit': COMMIT, 'dirty': False}), \
             patch.object(pipeline.release, 'github_json', return_value={'visibility': 'public'}), \
             patch.object(pipeline.ci_prepare, 'verify_toolchain'), \
             patch.object(pipeline.release, 'main', return_value={'reuse': False}), \
             patch.object(pipeline.ci_prepare, 'prepare') as prepare, \
             patch.object(pipeline.contract, 'collect') as collect, \
             patch.object(pipeline.release, 'publish', return_value={'artifact': {'sha256': 'f' * 64}}) as publish, \
             patch.object(pipeline.release, 'verify_public_release', side_effect=AssertionError('duplicate public download')), \
             patch.object(pipeline.contract, 'run'), \
             patch.object(pipeline.tap_update, 'remote_update') as tap:
            self.call(['--execute', '--publish'])
            self.assertEqual([c.args[3] for c in prepare.call_args_list], ['universal'])
            self.assertEqual(prepare.call_args.kwargs, {'signed': True, 'notarized': True})
            collect.assert_called_once_with(self.config, self.root / 'dist/release', '1.2.3', COMMIT, promotion=True)
            publish.assert_called_once_with(self.config, '1.2.3', COMMIT, self.root / 'dist/release')
            candidate = self.root / '.build/promotion.rb'
            self.assertEqual(candidate.read_text(), pipeline.contract.cask_text(self.config, '1.2.3', 'f' * 64))
            self.assertEqual(tap.call_args.args[2], candidate)

    def test_public_reuse_never_rebuilds_and_tap_uses_own_token(self):
        env = {'GITHUB_ACTIONS': 'true', 'GITHUB_EVENT_NAME': 'workflow_dispatch', 'GH_TOKEN': 'app-token',
               'HOMEBREW_TAP_TOKEN': 'tap-token', 'GITHUB_SHA': COMMIT}
        with patch.dict(os.environ, env, clear=True), \
             patch.object(pipeline.contract, 'source_state', return_value={'commit': COMMIT, 'dirty': False}), \
             patch.object(pipeline.release, 'github_json', return_value={'visibility': 'public'}), \
             patch.object(pipeline.ci_prepare, 'verify_toolchain'), \
             patch.object(pipeline.release, 'main', return_value={'reuse': True}), \
             patch.object(pipeline.ci_prepare, 'prepare') as prepare, \
             patch.object(pipeline.contract, 'collect') as collect, \
             patch.object(pipeline.release, 'publish', return_value={'artifact': {'sha256': 'e' * 64}}), \
             patch.object(pipeline.release, 'verify_public_release', side_effect=AssertionError('duplicate public download')), \
             patch.object(pipeline.contract, 'run') as run, \
             patch.object(pipeline.tap_update, 'remote_update', side_effect=lambda *a: self.assertEqual(os.environ['GH_TOKEN'], 'tap-token')) as tap:
            self.call(['--execute', '--publish'])
            prepare.assert_not_called()
            collect.assert_not_called()
            self.assertIn('e' * 64, (self.root / '.build/promotion.rb').read_text())
            self.assertEqual(os.environ['GH_TOKEN'], 'app-token')
            calls = [list(map(str, c.args[0])) for c in run.call_args_list]
            clone = next(c for c in calls if 'clone' in c)
            self.assertEqual(clone[-2], 'https://github.com/mayflies-app/homebrew-tap.git')
            self.assertEqual(tap.call_args.args[-1], 'mayflies-app/homebrew-tap')
            self.assertTrue(any('!gh auth git-credential' in c and 'config' in c for c in calls))
            self.assertFalse(any('tap-token' in ' '.join(c) for c in calls))

    def test_tap_failure_reports_publication_completed_and_restores_app_token(self):
        env = {'GITHUB_ACTIONS': 'true', 'GITHUB_EVENT_NAME': 'workflow_dispatch', 'GH_TOKEN': 'app-token',
               'HOMEBREW_TAP_TOKEN': 'tap-token', 'GITHUB_SHA': COMMIT}
        error = subprocess.CalledProcessError(128, ['git', 'SECRET-ARGUMENT'], stderr='remote: Bad credentials tap-token')
        with patch.dict(os.environ, env, clear=True), \
             patch.object(pipeline.contract, 'source_state', return_value={'commit': COMMIT, 'dirty': False}), \
             patch.object(pipeline.release, 'github_json', return_value={'visibility': 'public'}), \
             patch.object(pipeline.ci_prepare, 'verify_toolchain'), \
             patch.object(pipeline.release, 'main', return_value={'reuse': True}), \
             patch.object(pipeline.ci_prepare, 'prepare') as prepare, \
             patch.object(pipeline.release, 'publish', return_value={'artifact': {'sha256': 'e' * 64}}) as publish, \
             patch.object(pipeline.contract, 'run'), \
             patch.object(pipeline.tap_update, 'remote_update', side_effect=error), \
             contextlib.redirect_stderr(io.StringIO()) as output:
            with self.assertRaises(subprocess.CalledProcessError):
                self.call(['--execute', '--publish'])
            self.assertEqual(os.environ['GH_TOKEN'], 'app-token')
            publish.assert_called_once()
            prepare.assert_not_called()
        logs = output.getvalue()
        self.assertIn('stage=publish status=completed', logs)
        self.assertIn('https://github.com/mayflies-app/sample-app/releases/tag/v1.2.3', logs)
        self.assertIn('do not roll back this release', logs)
        self.assertIn('kind=authentication', logs)
        self.assertLess(logs.index('stage=publish status=completed'), logs.index('status=failed'))
        self.assertNotIn('tap-token', logs)
        self.assertNotIn('SECRET-ARGUMENT', logs)


if __name__ == '__main__':
    unittest.main()
