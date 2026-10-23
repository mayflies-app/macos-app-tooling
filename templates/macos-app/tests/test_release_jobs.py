"""Release job isolation and retry contracts; no Apple or GitHub writes."""
import contextlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from test_pipeline import pipeline
import release
import release_contract as c
import ci_prepare
import ci_security
from test_release_contract import CONFIG, COMMIT, fixture


class WorkflowHandoffTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        workflow = Path(__file__).parents[1] / 'release.yml'
        cls.jobs = json.loads(subprocess.check_output([
            'ruby', '-ryaml', '-rjson', '-e', 'puts JSON.generate(YAML.load_file(ARGV[0]))', str(workflow)], text=True))['jobs']

    def test_missing_artifact_ids_stop_before_download_can_fall_back_to_all_artifacts(self):
        for job in ('notarize', 'publish'):
            with self.subTest(job=job):
                steps = self.jobs[job]['steps']
                index = next(i for i, step in enumerate(steps) if step.get('uses', '').startswith('actions/download-artifact'))
                guard = next((step for step in steps[:index] if step.get('name') == 'Validate artifact handoff'), None)
                self.assertIsNotNone(guard, 'an empty artifact ID must never reach the download action')
                for artifact in ('', 'abc', '1,2', '42'):
                    result = subprocess.run(['bash', '-euo', 'pipefail', '-c', guard['run']],
                                            env=dict(os.environ, ARTIFACT_ID=artifact), capture_output=True, text=True)
                    self.assertEqual(result.returncode == 0, artifact == '42')
                self.assertEqual(guard.get('if'), steps[index].get('if'))

    def test_job_conditions_allow_reuse_and_stop_failed_or_cancelled_dependencies(self):
        def allowed(job, needs, inputs, cancelled=False):
            expression = self.jobs[job].get('if', 'true')
            # Model GitHub's implicit success() only when no status function is
            # present. Evaluate this workflow's boolean expressions, not Actions.
            if not re.search(r'\b(?:success|failure|cancelled|always)\(', expression):
                if cancelled or any(value['result'] != 'success' for value in needs.values()):
                    return False
            expression = expression.replace('!cancelled()', repr(not cancelled))
            def reference(match):
                parts = match.group().split('.')
                current = {'needs': needs, 'inputs': inputs}
                for part in parts:
                    current = current.get(part, '') if isinstance(current, dict) else ''
                return repr(current)
            expression = re.sub(r'\b(?:needs|inputs)(?:\.[A-Za-z_][A-Za-z0-9_]*)+', reference, expression)
            expression = re.sub(r"(?<!['\"])\btrue\b", 'True', expression)
            expression = re.sub(r"(?<!['\"])\bfalse\b", 'False', expression)
            expression = re.sub(r'!(?!=)', 'not ', expression).replace('&&', ' and ').replace('||', ' or ')
            return bool(eval(expression.strip(), {'__builtins__': {}}, {}))
        success = {'result': 'success', 'outputs': {'reuse': 'false'}}
        skipped = {'result': 'skipped', 'outputs': {}}
        failed = {'result': 'failure', 'outputs': {}}
        for reuse in (True, False):
            preflight = dict(success, outputs={'reuse': str(reuse).lower()})
            self.assertEqual(allowed('package', {'preflight': preflight}, {'publish': True}), not reuse)
            needs = {'preflight': preflight, 'notarize': skipped if reuse else success}
            self.assertTrue(allowed('publish', needs, {'publish': True}))
            self.assertFalse(allowed('publish', needs, {'publish': False}))
            self.assertFalse(allowed('publish', needs, {'publish': True}, cancelled=True))
        self.assertFalse(allowed('publish', {'preflight': success, 'notarize': failed}, {'publish': True}))
        self.assertFalse(allowed('publish', {'preflight': failed, 'notarize': skipped}, {'publish': True}))
        self.assertTrue(allowed('cask', {'preflight': success, 'publish': success}, {'publish': True}))
        self.assertFalse(allowed('cask', {'preflight': success, 'publish': failed}, {'publish': True}))

    def test_actual_archive_commands_preserve_bundle_and_export_only_release_assets(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            sender, receiver = root / 'sender', root / 'receiver'
            output = sender / 'dist/release/universal'
            app = output / 'app/Sample.app'
            executable = app / 'Contents/MacOS/Sample'
            executable.parent.mkdir(parents=True)
            executable.write_bytes(b'fixture executable')
            executable.chmod(0o755)
            (app / 'Contents/current').symlink_to('MacOS/Sample')
            before = c.bundle_payload_digest(app)
            archive = next(step['run'] for step in self.jobs['package']['steps'] if step.get('name') == 'Preserve signed bundle permissions and symlinks')
            subprocess.run(['bash', '-euo', 'pipefail', '-c', archive], cwd=sender, check=True, capture_output=True)
            (receiver / 'dist').mkdir(parents=True)
            shutil.copyfile(sender / 'dist/release-signed.tar', receiver / 'dist/release-signed.tar')
            restore = next(step['run'] for step in self.jobs['notarize']['steps'] if step.get('name') == 'Restore the exact signed package')
            subprocess.run(['bash', '-euo', 'pipefail', '-c', restore], cwd=receiver, check=True, capture_output=True)
            restored = receiver / 'dist/release/universal/app/Sample.app'
            self.assertEqual(c.bundle_payload_digest(restored), before)
            self.assertTrue((restored / 'Contents/current').is_symlink())
            self.assertTrue((restored / 'Contents/MacOS/Sample').stat().st_mode & 0o111)
            dmg, _ = fixture(output)
            dmg.with_suffix('.notary.submission.json').write_text('{}')
            dmg.with_suffix('.notary.log.json').write_text('{}')
            public = root / 'public'
            public.mkdir()
            upload = next(step for step in self.jobs['notarize']['steps'] if step.get('id') == 'notarized')
            names = set()
            for pattern in upload['with']['path'].splitlines():
                for path in output.glob(Path(pattern).name):
                    shutil.copyfile(path, public / path.name)
                    names.add(path.name)
            _, files = c.collect(CONFIG, public, '1.2.3', COMMIT, promotion=True)
            self.assertEqual(names, {path.name for path in files})
            self.assertEqual(len(names), 4)


class StagedReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / 'macos-app.json').write_text(json.dumps(CONFIG))
        (self.root / 'VERSION').write_text('1.2.3\n')
        self.env = {'GITHUB_ACTIONS': 'true', 'GITHUB_EVENT_NAME': 'workflow_dispatch',
                    'GH_TOKEN': 'app-token', 'GITHUB_SHA': COMMIT,
                    'GITHUB_RUN_ATTEMPT': '1', 'GITHUB_RUN_ID': '42'}

    def tearDown(self):
        self.temp.cleanup()

    def call(self, stage, publish=True):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return pipeline.main(['--execute', '--stage', stage] + (['--publish'] if publish else []), root=self.root)

    def test_local_stage_execution_rejected_before_tools_or_output(self):
        for stage in ('preflight', 'package', 'notarize', 'publish', 'cask'):
            with self.subTest(stage=stage), patch.dict(os.environ, {}, clear=True), patch.object(c, 'run') as run:
                with self.assertRaisesRegex(ValueError, 'Remote operation'):
                    self.call(stage)
                run.assert_not_called()
        self.assertFalse((self.root / 'dist').exists())

    def test_preflight_has_no_signing_notary_or_tap_secret_requirement(self):
        output = self.root / 'job-outputs'
        with patch.dict(os.environ, dict(self.env, GITHUB_OUTPUT=str(output)), clear=True), \
             patch.object(c, 'source_state', return_value={'commit': COMMIT, 'dirty': False}), \
             patch.object(release, 'github_json', return_value={'visibility': 'public'}), \
             patch.object(release, 'check_publish_state', return_value='reuse'), \
             patch.object(ci_prepare, 'verify_toolchain', side_effect=AssertionError('macOS tool called')):
            self.call('preflight')
        self.assertIn('version=1.2.3', output.read_text())
        self.assertIn('reuse=true', output.read_text())

    def test_package_job_does_not_notarize_publish_or_promote(self):
        with patch.dict(os.environ, self.env, clear=True), \
             patch.object(c, 'source_state', return_value={'commit': COMMIT, 'dirty': False}), \
             patch.object(ci_prepare, 'verify_toolchain'), \
             patch.object(ci_prepare, 'prepare') as prepare, \
             patch.object(release, 'publish', side_effect=AssertionError('publication called')), \
             patch.object(ci_security, 'notarize', side_effect=AssertionError('Apple called')):
            self.call('package')
        self.assertEqual(prepare.call_args.kwargs, {'signed': True, 'notarized': False})

    def test_cask_retry_never_builds_notarizes_or_republishes(self):
        env = dict(self.env, HOMEBREW_TAP_TOKEN='tap-token')
        proof = {'artifact': {'sha256': 'f' * 64}}
        with patch.dict(os.environ, env, clear=True), \
             patch.object(c, 'source_state', return_value={'commit': COMMIT, 'dirty': False}), \
             patch.object(release, 'verify_public_release', return_value=proof), \
             patch.object(ci_prepare, 'prepare', side_effect=AssertionError('build called')), \
             patch.object(release, 'publish', side_effect=AssertionError('publication called')), \
             patch.object(ci_security, 'notarize', side_effect=AssertionError('Apple called')), \
             patch.object(c, 'run'), patch.object(pipeline.tap_update, 'remote_update', side_effect=[RuntimeError('tap rejected'), {'pushed': True}]) as tap:
            with self.assertRaisesRegex(RuntimeError, 'tap rejected'):
                self.call('cask')
            self.assertEqual(os.environ['GH_TOKEN'], 'app-token')
            self.call('cask')
            self.assertEqual(os.environ['GH_TOKEN'], 'app-token')
        self.assertEqual(tap.call_count, 2)
        self.assertIn('f' * 64, (self.root / '.build/promotion.rb').read_text())

    def test_publication_and_cask_require_explicit_publish_input(self):
        with patch.dict(os.environ, self.env, clear=True), patch.object(c, 'source_state', return_value={'commit': COMMIT, 'dirty': False}), patch.object(release, 'publish') as publish:
            for stage in ('publish', 'cask'):
                with self.subTest(stage=stage), self.assertRaisesRegex(ValueError, '--publish'):
                    self.call(stage, publish=False)
            publish.assert_not_called()


class NotaryResumeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.dmg = self.root / 'sample.dmg'
        self.dmg.write_bytes(b'signed immutable package')
        self.receipt = self.dmg.with_suffix('.notary.json')
        self.env = {'RUNNER_TEMP': str(self.root), 'APPLE_NOTARIZATION_PRIVATE_KEY': '-----BEGIN PRIVATE KEY-----\nfixture\n-----END PRIVATE KEY-----',
                    'APPLE_NOTARIZATION_KEY_ID': 'KEY', 'APPLE_NOTARIZATION_ISSUER_ID': 'ISSUER'}

    def tearDown(self):
        self.temp.cleanup()

    def result(self, payload, code=0):
        return subprocess.CompletedProcess([], code, json.dumps(payload), '')

    def test_wait_retry_reuses_submission_id_and_original_bytes(self):
        submitted = self.result({'id': 'submission-1', 'message': 'uploaded'})
        timeout = self.result({'id': 'submission-1', 'status': 'In Progress'}, 1)
        accepted = self.result({'id': 'submission-1', 'status': 'Accepted'})
        with patch.object(c, 'signature_details'), patch.object(c, 'run'), contextlib.redirect_stderr(io.StringIO()):
            with patch.object(ci_security.subprocess, 'run', side_effect=[submitted, timeout, self.result({})]):
                with self.assertRaises(RuntimeError):
                    ci_security.notarize(self.dmg, self.receipt, self.env)
            checkpoint = json.loads(self.receipt.with_suffix('.submission.json').read_text())
            self.assertEqual(checkpoint['artifactSha256'], c.digest(self.dmg))
            with patch.object(ci_security.subprocess, 'run', side_effect=[accepted, self.result({})]) as external:
                ci_security.notarize(self.dmg, self.receipt, self.env)
            self.assertEqual(external.call_args_list[0].args[0][2:4], ['wait', 'submission-1'])
            self.assertFalse(any('submit' in call.args[0] for call in external.call_args_list))
        self.assertEqual(json.loads(self.receipt.read_text())['status'], 'Accepted')
        self.assertFalse(list(self.root.glob('release-notary-*')))

    def test_accepted_receipt_retry_only_staples_without_another_apple_submission(self):
        checkpoint = self.receipt.with_suffix('.submission.json')
        checkpoint.write_text(json.dumps({'id': 'submission-1', 'artifactSha256': c.digest(self.dmg)}))
        self.receipt.write_text(json.dumps({'id': 'submission-1', 'status': 'Accepted'}))
        with patch.object(c, 'signature_details'), patch.object(c, 'run') as run, patch.object(ci_security.subprocess, 'run', side_effect=AssertionError('duplicate submission')), contextlib.redirect_stderr(io.StringIO()):
            ci_security.notarize(self.dmg, self.receipt, self.env)
        self.assertTrue(any('staple' in call.args[0] for call in run.call_args_list))
        self.assertTrue(any('validate' in call.args[0] for call in run.call_args_list))

    def test_checkpoint_for_different_artifact_is_rejected_before_apple(self):
        self.receipt.with_suffix('.submission.json').write_text(json.dumps({'id': 'submission-1', 'artifactSha256': '0' * 64}))
        with patch.object(c, 'signature_details'), patch.object(c, 'run'), patch.object(ci_security.subprocess, 'run') as external:
            with self.assertRaisesRegex(ValueError, 'artifact'):
                ci_security.notarize(self.dmg, self.receipt, self.env)
            external.assert_not_called()

    def test_wait_without_json_resumes_known_id_on_retry(self):
        self.receipt.with_suffix('.submission.json').write_text(json.dumps({'id': 'submission-1', 'artifactSha256': c.digest(self.dmg)}))
        self.receipt.write_text('connection interrupted')
        with patch.object(c, 'signature_details'), patch.object(c, 'run'), contextlib.redirect_stderr(io.StringIO()), \
             patch.object(ci_security.subprocess, 'run', side_effect=[self.result({'id': 'submission-1', 'status': 'Accepted'}), self.result({})]) as external:
            ci_security.notarize(self.dmg, self.receipt, self.env)
        self.assertEqual(external.call_args_list[0].args[0][2:4], ['wait', 'submission-1'])


class CheckpointArtifactTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.output = self.root / 'dist/release/universal'
        self.output.mkdir(parents=True)
        (self.output / 'sample.dmg').write_bytes(b'signed')
        self.env = {'GITHUB_RUN_ID': '42', 'GITHUB_RUN_ATTEMPT': '3', 'GITHUB_SHA': COMMIT}
        self.run = {'head_sha': COMMIT, 'event': 'workflow_dispatch'}

    def tearDown(self):
        self.temp.cleanup()

    def artifacts(self, expired=False):
        return [{'artifacts': [{'name': 'release-notary-state-1', 'expired': expired},
                               {'name': 'release-notary-state-99', 'expired': False}]}]

    def test_restores_prior_same_run_checkpoint_even_when_producer_attempt_differs(self):
        checkpoint = {'id': 'submission-1', 'artifactSha256': c.digest(self.output / 'sample.dmg')}
        def download(command, **kwargs):
            self.assertIn('release-notary-state-1', command)
            (Path(command[-1]) / 'sample.notary.submission.json').write_text(json.dumps(checkpoint))
        with patch.dict(os.environ, self.env, clear=True), \
             patch.object(release, 'github_json', side_effect=[self.run, self.artifacts()]), \
             patch.object(c, 'run', side_effect=download):
            pipeline.restore_notary_state(self.root, CONFIG)
        self.assertEqual(json.loads((self.output / 'sample.notary.submission.json').read_text()), checkpoint)

    def test_expired_or_wrong_source_state_never_downloads(self):
        for run, artifacts in ((dict(self.run, head_sha='b'*40), self.artifacts()), (self.run, self.artifacts(expired=True))):
            with self.subTest(run=run), patch.dict(os.environ, self.env, clear=True), \
                 patch.object(release, 'github_json', side_effect=[run, artifacts]), patch.object(c, 'run') as external:
                with self.assertRaises(ValueError):
                    pipeline.restore_notary_state(self.root, CONFIG)
                external.assert_not_called()

    def test_missing_checkpoint_after_prior_notary_execution_blocks_duplicate_submission(self):
        jobs = [{'jobs': [{'name': 'notarize', 'run_attempt': 1, 'steps': [
            {'name': 'Resume or submit notarization and verify final bytes', 'conclusion': 'failure'}]}]}]
        with patch.dict(os.environ, self.env, clear=True), \
             patch.object(release, 'github_json', side_effect=[self.run, [{'artifacts': []}], jobs]), patch.object(c, 'run') as external:
            with self.assertRaisesRegex(ValueError, 'submission history'):
                pipeline.restore_notary_state(self.root, CONFIG)
            external.assert_not_called()

    def test_retry_before_notary_ever_started_does_not_require_checkpoint(self):
        jobs = [{'jobs': [{'name': 'notarize', 'run_attempt': 1, 'steps': [
            {'name': 'Resume or submit notarization and verify final bytes', 'conclusion': 'skipped'}]}]}]
        with patch.dict(os.environ, self.env, clear=True), \
             patch.object(release, 'github_json', side_effect=[self.run, [{'artifacts': []}], jobs]), patch.object(c, 'run') as external:
            pipeline.restore_notary_state(self.root, CONFIG)
            external.assert_not_called()


class DraftResumeTests(unittest.TestCase):
    def test_empty_starter_asset_is_reuploaded_after_other_assets_are_verified(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            dmg, _ = fixture(root)
            _, files = c.collect(CONFIG, root, '1.2.3', COMMIT, promotion=True)
            metadata = {'draft': True, 'assets': [{'name': path.name, 'state': 'uploaded', 'id': index + 1}
                        for index, path in enumerate(files)], 'tag_name': 'v1.2.3'}
            metadata['assets'][0].update(state='starter', size=0)
            complete = dict(metadata, assets=[dict(asset, state='uploaded') for asset in metadata['assets']])
            def download(config, meta, asset, destination, public=True):
                self.assertEqual(asset['state'], 'uploaded', 'unfinished uploads are not downloadable assets')
                path = Path(destination) / asset['name']
                shutil.copyfile(root / asset['name'], path)
                return path
            with patch.object(release, 'check_publish_state', return_value='draft'), \
                 patch.object(release, 'release_metadata', side_effect=[metadata, complete]), \
                 patch.object(release, 'verify_release_identity'), patch.object(release, 'download_asset', side_effect=download), \
                 patch.object(release, 'fetch_release', return_value=([], files)), \
                 patch.object(release, 'verify_public_release', return_value={}), patch.object(c, 'run') as run:
                release.publish(CONFIG, '1.2.3', COMMIT, root)
            commands = [list(map(str, call.args[0])) for call in run.call_args_list]
            delete = next(cmd for cmd in commands if '--method' in cmd)
            self.assertIn('DELETE', delete)
            self.assertIn('repos/owner/sample-app/releases/assets/1', delete)
            upload = next(cmd for cmd in commands if cmd[:3] == ['gh', 'release', 'upload'])
            self.assertIn(str(dmg), upload)
            self.assertFalse(any(str(path) in upload for path in files[1:]))

    def test_draft_published_between_checks_is_verified_without_writes(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            fixture(root)
            metadata = {'draft': False, 'assets': [], 'tag_name': 'v1.2.3'}
            with patch.object(release, 'check_publish_state', return_value='draft'), \
                 patch.object(release, 'release_metadata', return_value=metadata), \
                 patch.object(release, 'verify_release_identity'), \
                 patch.object(release, 'verify_public_release', return_value={'publicAssetVerified': True}) as verify, \
                 patch.object(c, 'run') as run:
                release.publish(CONFIG, '1.2.3', COMMIT, root)
            run.assert_not_called()
            verify.assert_called_once_with(CONFIG, '1.2.3', COMMIT, root)

    def test_partial_draft_upload_retries_only_missing_assets(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            files = [root/'sample.dmg', root/'sample.json']
            for i, path in enumerate(files): path.write_bytes(str(i).encode())
            metadata = {'draft': True, 'assets': [{'name': files[0].name, 'state': 'uploaded'}], 'tag_name': 'v1.2.3'}
            complete = dict(metadata, assets=[{'name': path.name, 'state': 'uploaded'} for path in files])
            def download(config, meta, asset, destination, public=True):
                path = Path(destination)/asset['name'];shutil.copyfile(root/asset['name'], path);return path
            with patch.object(release, 'check_publish_state', return_value='draft'), \
                 patch.object(c, 'collect', return_value=([], files)), \
                 patch.object(release, 'release_metadata', side_effect=[metadata, complete]), \
                 patch.object(release, 'verify_release_identity'), \
                 patch.object(release, 'download_asset', side_effect=download), \
                 patch.object(release, 'fetch_release', return_value=([], files)), \
                 patch.object(release, 'verify_public_release', return_value={'publicAssetVerified': True}), \
                 patch.object(c, 'run') as run:
                release.publish(CONFIG, '1.2.3', COMMIT, root)
            commands = [list(map(str, call.args[0])) for call in run.call_args_list]
            upload = next(cmd for cmd in commands if cmd[:3] == ['gh', 'release', 'upload'])
            self.assertIn(str(files[1]), upload)
            self.assertNotIn(str(files[0]), upload)
            self.assertFalse(any('create' in cmd or '--clobber' in cmd for cmd in commands))

    def test_draft_with_changed_bytes_stops_before_any_remote_write(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            local = root / 'sample.dmg'
            local.write_bytes(b'prepared')
            def download(config, meta, asset, destination, public=True):
                path = Path(destination) / asset['name']
                path.write_bytes(b'different')
                return path
            with patch.object(release, 'check_publish_state', return_value='draft'), \
                 patch.object(c, 'collect', return_value=([], [local])), \
                 patch.object(release, 'release_metadata', return_value={'draft': True, 'assets': [{'name': local.name, 'state': 'uploaded'}]}), \
                 patch.object(release, 'verify_release_identity'), patch.object(release, 'download_asset', side_effect=download), \
                 patch.object(c, 'run') as external:
                with self.assertRaisesRegex(ValueError, 'draft bytes'):
                    release.publish(CONFIG, '1.2.3', COMMIT, root)
                external.assert_not_called()

    def test_preflight_accepts_only_same_source_draft_as_resumable(self):
        metadata = {'draft': True, 'prerelease': False, 'tag_name': 'v1.2.3', 'target_commitish': COMMIT}
        with patch.object(release, 'release_metadata', return_value=metadata), patch.object(release, 'tag_commit', return_value=None):
            self.assertEqual(release.check_publish_state(CONFIG, '1.2.3', COMMIT), 'draft')
            with self.assertRaisesRegex(ValueError, 'source commit'):
                release.check_publish_state(CONFIG, '1.2.3', 'b'*40)


class DownloadedPackageTests(unittest.TestCase):
    def test_notary_job_rejects_wrong_build_source_or_payload_before_apple(self):
        for wrong in ('build', 'source', 'payload'):
            with self.subTest(wrong=wrong), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                config = root / 'macos-app.json'
                config.write_text(json.dumps(CONFIG))
                output = root / 'dist/release/universal'
                output.mkdir(parents=True)
                _, manifest = fixture(output, notarized=False)
                bundle = dict(manifest['bundle'], payloadSha256='d'*64 if wrong == 'payload' else manifest['bundle']['payloadSha256'])
                with patch.dict(os.environ, {'MACOS_TEAM_ID': 'TEAM'}), patch.object(c, 'verify_app', return_value=bundle), patch.object(ci_security, 'notarize') as apple:
                    with self.assertRaises(ValueError):
                        ci_prepare.notarize_existing(config, '1.2.3', '99' if wrong == 'build' else '7', 'b'*40 if wrong == 'source' else COMMIT)
                    apple.assert_not_called()

    def test_final_manifest_uses_stapled_bytes_and_accepted_receipt(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            config = root / 'macos-app.json'
            config.write_text(json.dumps(CONFIG))
            output = root / 'dist/release/universal'
            output.mkdir(parents=True)
            dmg, manifest = fixture(output, notarized=False)
            original_sha = c.digest(dmg)
            def staple(dmg, receipt):
                dmg.write_bytes(dmg.read_bytes() + b'stapled ticket')
                receipt.write_text(json.dumps({'id': 'submission-1', 'status': 'Accepted'}))
            with patch.dict(os.environ, {'MACOS_TEAM_ID': 'TEAM'}), patch.object(c, 'verify_app', return_value=manifest['bundle']), patch.object(c, 'run'), \
                 patch.object(c, 'signature_details', return_value=manifest['dmgSignature']), \
                 patch.object(c, 'source_state', return_value={'commit': COMMIT, 'dirty': False}), \
                 patch.object(ci_security, 'notarize', side_effect=staple), contextlib.redirect_stderr(io.StringIO()):
                ci_prepare.notarize_existing(config, '1.2.3', '7', COMMIT)
            manifests, files = c.collect(CONFIG, output, '1.2.3', COMMIT, promotion=True)
            self.assertNotEqual(manifests[0]['artifact']['sha256'], original_sha)
            self.assertEqual(manifests[0]['artifact']['sha256'], c.digest(dmg))
            self.assertEqual(manifests[0]['notarization']['submissionId'], 'submission-1')
            self.assertEqual(len(files), 4)


if __name__ == '__main__':
    unittest.main()
