"""Local multi-job retry acceptance. Apple/GitHub transports use isolated fixtures."""
import contextlib
import copy
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from test_pipeline import pipeline
from test_release_contract import CONFIG, COMMIT, fixture
import ci_prepare
import ci_security
import release
import release_contract as c


class LocalRelease:
    """Stateful remote boundary; actual files, hashes and release checks stay real."""
    def __init__(self, root):
        self.root = root
        self.assets = root / 'github-assets'
        self.assets.mkdir()
        self.metadata = None
        self.calls = []
        self.next_id = 1
        self.interrupt_upload = False
        self.lose_publish_reply = False
        self.original_run = c.run

    def add(self, path, starter=False):
        target = self.assets / path.name
        target.write_bytes(b'' if starter else path.read_bytes())
        asset = {'id': self.next_id, 'name': path.name, 'size': target.stat().st_size,
                 'state': 'starter' if starter else 'uploaded', 'digest': 'sha256:' + c.digest(target),
                 'browser_download_url': f"https://github.com/{CONFIG['repository']}/releases/download/v1.2.3/{path.name}"}
        self.next_id += 1
        self.metadata['assets'].append(asset)

    def api(self, endpoint, missing_ok=False, paginate=False):
        prefix = 'repos/' + CONFIG['repository']
        if endpoint == prefix:
            return {'visibility': 'public'}
        if endpoint == prefix + '/releases?per_page=100':
            return [[copy.deepcopy(self.metadata)] if self.metadata else []]
        if endpoint == prefix + '/releases/latest':
            return copy.deepcopy(self.metadata) if self.metadata and not self.metadata['draft'] else None
        if endpoint == prefix + '/git/ref/tags/v1.2.3':
            return {'object': {'type': 'commit', 'sha': COMMIT}} if self.metadata and not self.metadata['draft'] else None
        raise AssertionError('Unexpected GitHub read: ' + endpoint)

    def command(self, command, **kwargs):
        command = list(map(str, command))
        if command[0] == 'git':
            return self.original_run(command, **kwargs)
        self.calls.append(command)
        if command[:3] == ['gh', 'release', 'create']:
            self.metadata = {'tag_name': 'v1.2.3', 'draft': True, 'prerelease': False,
                             'target_commitish': COMMIT, 'assets': []}
            files = list(map(Path, command[4:command.index('--repo')]))
            for index, path in enumerate(files):
                self.add(path, starter=self.interrupt_upload and index == 1)
                if self.interrupt_upload and index == 1:
                    self.interrupt_upload = False
                    raise subprocess.CalledProcessError(1, command, stderr='HTTP 502 interrupted upload')
        elif command[:3] == ['gh', 'release', 'upload']:
            assert self.metadata['draft'] is True
            for path in map(Path, command[4:command.index('--repo')]):
                assert path.name not in {asset['name'] for asset in self.metadata['assets']}
                self.add(path)
        elif command[:4] == ['gh', 'api', '--method', 'DELETE']:
            assert self.metadata['draft'] is True
            asset_id = int(command[-1].split('/')[-1])
            asset = next(asset for asset in self.metadata['assets'] if asset['id'] == asset_id)
            assert asset['state'] == 'starter' and asset['size'] == 0
            (self.assets / asset['name']).unlink()
            self.metadata['assets'].remove(asset)
        elif command[:3] == ['gh', 'release', 'edit']:
            self.metadata['draft'] = False
            if self.lose_publish_reply:
                self.lose_publish_reply = False
                raise subprocess.CalledProcessError(1, command, stderr='connection lost after publication')
        elif command[:2] == ['gh', 'api'] and '/releases/assets/' in command[2]:
            asset = next(asset for asset in self.metadata['assets'] if asset['id'] == int(command[2].split('/')[-1]))
            kwargs['stdout'].write((self.assets / asset['name']).read_bytes())
        else:
            raise AssertionError('Unexpected external command: ' + repr(command))
        return subprocess.CompletedProcess(command, 0, '', '')

    def public_download(self, url, **kwargs):
        assert self.metadata['draft'] is False
        asset = next(asset for asset in self.metadata['assets'] if asset['browser_download_url'] == url)
        return io.BytesIO((self.assets / asset['name']).read_bytes())


class LocalJobRetries(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.input = self.root / 'notarized-artifact'
        self.input.mkdir()
        fixture(self.input)
        self.server = LocalRelease(self.root)
        self.env = {'GITHUB_ACTIONS': 'true', 'GITHUB_EVENT_NAME': 'workflow_dispatch',
                    'GITHUB_SHA': COMMIT, 'GITHUB_REPOSITORY': CONFIG['repository'],
                    'GH_TOKEN': 'fixture-app-token', 'HOMEBREW_TAP_TOKEN': 'fixture-tap-token',
                    'PATH': os.environ.get('PATH', os.defpath)}
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.dict(os.environ, self.env, clear=True))
        self.stack.enter_context(patch.object(c, 'source_state', return_value={'commit': COMMIT, 'dirty': False}))
        self.stack.enter_context(patch.object(release, 'github_json', side_effect=self.server.api))
        self.stack.enter_context(patch.object(release.urllib.request, 'urlopen', side_effect=self.server.public_download))
        self.stack.enter_context(patch.object(c, 'run', side_effect=self.server.command))
        self.stack.enter_context(patch.object(ci_prepare, 'prepare', side_effect=AssertionError('unexpected rebuild')))
        self.stack.enter_context(patch.object(ci_prepare, 'verify_toolchain', side_effect=AssertionError('unexpected macOS toolchain')))
        self.stack.enter_context(patch.object(ci_security, 'notarize', side_effect=AssertionError('unexpected Apple submission')))
        self.stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        self.stack.enter_context(contextlib.redirect_stderr(io.StringIO()))

    def job(self, name, with_input=True):
        root = self.root / name
        root.mkdir()
        (root / 'macos-app.json').write_text(json.dumps(CONFIG))
        (root / 'VERSION').write_text('1.2.3\n')
        if with_input:
            shutil.copytree(self.input, root / 'dist/release/universal')
        return root

    def run_stage(self, root, stage):
        return pipeline.main(['--execute', '--stage', stage, '--publish', '--build', '7'], root=root)

    def seed_public_release(self):
        self.server.metadata = {'tag_name': 'v1.2.3', 'draft': False, 'prerelease': False, 'assets': []}
        for path in sorted(self.input.iterdir()):
            self.server.add(path)

    def test_interrupted_draft_upload_resumes_from_same_artifact_in_fresh_job(self):
        before = {path.name: c.digest(path) for path in self.input.iterdir()}
        self.server.interrupt_upload = True
        with self.assertRaises(subprocess.CalledProcessError):
            self.run_stage(self.job('publish-1'), 'publish')
        completed_id = self.server.metadata['assets'][0]['id']
        self.assertTrue(any(asset['state'] == 'starter' for asset in self.server.metadata['assets']))
        self.run_stage(self.job('publish-2'), 'publish')
        self.assertFalse(self.server.metadata['draft'])
        self.assertEqual(self.server.metadata['assets'][0]['id'], completed_id)
        self.assertEqual(len(self.server.metadata['assets']), 4)
        self.assertEqual({asset['name']: c.digest(self.server.assets / asset['name']) for asset in self.server.metadata['assets']}, before)
        self.assertEqual(sum(cmd[:3] == ['gh', 'release', 'create'] for cmd in self.server.calls), 1)
        self.assertEqual(sum(cmd[:3] == ['gh', 'release', 'upload'] for cmd in self.server.calls), 1)
        self.assertFalse(any('--clobber' in cmd for cmd in self.server.calls))
        self.assertEqual(before, {path.name: c.digest(path) for path in self.input.iterdir()})

    def test_published_release_reuse_needs_no_artifact_or_signing_credentials(self):
        self.seed_public_release()
        root = self.job('reuse', with_input=False)
        outputs = self.root / 'preflight-output'
        with patch.dict(os.environ, {'GITHUB_OUTPUT': str(outputs)}):
            self.run_stage(root, 'preflight')
        self.assertIn('reuse=true', outputs.read_text())
        self.run_stage(root, 'publish')
        self.assertEqual(self.server.calls, [])
        self.assertFalse((root / 'dist').exists())

    def test_draft_starter_is_preserved_when_completed_asset_verification_fails(self):
        self.server.interrupt_upload = True
        with self.assertRaises(subprocess.CalledProcessError):
            self.run_stage(self.job('publish-1'), 'publish')
        completed = self.server.metadata['assets'][0]
        (self.server.assets / completed['name']).write_bytes(b'corrupted upload')
        def writes():
            return [cmd for cmd in self.server.calls if cmd[:2] == ['gh', 'release'] or '--method' in cmd]
        before = copy.deepcopy(writes())
        with self.assertRaises(ValueError):
            self.run_stage(self.job('publish-2'), 'publish')
        self.assertEqual(writes(), before)
        self.assertTrue(any(asset['state'] == 'starter' for asset in self.server.metadata['assets']))
        self.assertTrue(self.server.metadata['draft'])

    def test_reused_public_release_with_corrupt_bytes_cannot_be_promoted(self):
        self.seed_public_release()
        asset = self.server.metadata['assets'][0]
        (self.server.assets / asset['name']).write_bytes(b'corrupt published bytes')
        with self.assertRaises(ValueError):
            self.run_stage(self.job('reuse', with_input=False), 'publish')
        self.assertEqual(self.server.calls, [])

    def test_lost_publication_reply_retries_as_read_only_reuse(self):
        self.server.lose_publish_reply = True
        with self.assertRaises(subprocess.CalledProcessError):
            self.run_stage(self.job('publish-1'), 'publish')
        self.assertFalse(self.server.metadata['draft'])
        calls = copy.deepcopy(self.server.calls)
        self.run_stage(self.job('publish-2', with_input=False), 'publish')
        self.assertEqual(self.server.calls, calls)

    def test_cask_retry_after_rejected_or_uncertain_push_uses_fresh_real_git_checkout(self):
        self.seed_public_release()
        original_run = subprocess.run
        def git(*args):
            return original_run(['git', *map(str, args)], check=True, capture_output=True, text=True,
                                env=dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM='1', GIT_ALLOW_PROTOCOL='file')).stdout.strip()
        seed, remote = self.root / 'seed', self.root / 'tap.git'
        git('init', '-b', 'main', seed)
        (seed / 'Casks').mkdir()
        (seed / 'Casks/sample-app.rb').write_text(c.cask_text(CONFIG, '1.0.0', 'a'*64))
        (seed / 'Casks/other.rb').write_text('unrelated cask\n')
        git('-C', seed, 'add', '.')
        git('-C', seed, '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-m', 'seed')
        initial = git('-C', seed, 'rev-parse', 'HEAD')
        git('clone', '--bare', seed, remote)
        for accepted in (False, True):
            with self.subTest(server_accepted_before_failure=accepted):
                git('--git-dir', remote, 'update-ref', 'refs/heads/main', initial)
                pushes = 0
                def transport(command, **kwargs):
                    nonlocal pushes
                    command = list(map(str, command))
                    self.assertEqual(command[0], 'git', 'only isolated Git processes may execute')
                    kwargs['env'] = dict(kwargs.get('env', os.environ), GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM='1', GIT_ALLOW_PROTOCOL='file')
                    if 'clone' in command:
                        url = command.index('https://github.com/mayflies-app/homebrew-tap.git')
                        command[url] = str(remote)
                        result = original_run(command, **kwargs)
                        git('-C', command[-1], 'remote', 'set-url', 'origin', 'https://github.com/mayflies-app/homebrew-tap.git')
                        return result
                    if 'fetch' in command:
                        index = command.index('fetch')
                        command[index + 1:] = [str(remote), 'main:refs/remotes/origin/main']
                    if 'push' in command:
                        command[command.index('push') + 1] = str(remote)
                    if 'push' in command:
                        pushes += 1
                        if pushes == 1:
                            if accepted:
                                original_run(command, **kwargs)
                            return subprocess.CompletedProcess(command, 128, '', 'connection reset; fixture transport')
                    return original_run(command, **kwargs)
                with patch.object(subprocess, 'run', side_effect=transport):
                    with self.assertRaises(subprocess.CalledProcessError):
                        self.run_stage(self.job('cask-1-' + str(accepted), with_input=False), 'cask')
                    self.assertEqual(os.environ['GH_TOKEN'], 'fixture-app-token')
                    self.run_stage(self.job('cask-2-' + str(accepted), with_input=False), 'cask')
                    self.assertEqual(os.environ['GH_TOKEN'], 'fixture-app-token')
                self.assertEqual(git('--git-dir', remote, 'rev-list', '--count', 'main'), '2')
                text = git('--git-dir', remote, 'show', 'main:Casks/sample-app.rb')
                self.assertIn(c.digest(next(self.input.glob('*.dmg'))), text)
                self.assertEqual(git('--git-dir', remote, 'show', 'main:Casks/other.rb'), 'unrelated cask')
                self.assertEqual(pushes, 1 if accepted else 2)


class NotaryProcessRetry(unittest.TestCase):
    def test_timeout_retained_artifact_resumes_submission_in_fresh_process(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            tools = root / 'fixture-tools'
            tools.mkdir()
            events = root / 'tool-events.jsonl'
            code = '#!' + sys.executable + '\n' + '''import json, os, pathlib, sys, zipfile
name = pathlib.Path(sys.argv[0]).name
args = sys.argv[1:]
if name == 'codesign' and '--display' in args:
    print('Authority=Developer ID Application: Fixture\\nTeamIdentifier=TEAM\\nTimestamp=Oct 23, 2026', file=sys.stderr)
elif name == 'gh':
    assert args[:2] == ['run', 'download']
    with zipfile.ZipFile(os.environ['FIXTURE_CHECKPOINT_ZIP']) as archive:
        archive.extractall(args[-1])
elif name == 'xcrun':
    with open(os.environ['FIXTURE_EVENTS'], 'a') as stream:
        stream.write(json.dumps(args[:2]) + '\\n')
    if args[:2] == ['notarytool', 'submit']:
        print(json.dumps({'id': 'fixture-submission-1', 'status': 'In Progress'}))
    elif args[:2] == ['notarytool', 'wait']:
        assert args[2] == 'fixture-submission-1'
        if os.environ['FIXTURE_TIMEOUT'] == 'true':
            print(json.dumps({'id': 'fixture-submission-1', 'status': 'In Progress'}))
            sys.exit(69)
        print(json.dumps({'id': 'fixture-submission-1', 'status': 'Accepted'}))
    elif args[:2] == ['notarytool', 'log']:
        pathlib.Path(args[-1]).write_text('{}')
    elif args[:2] == ['stapler', 'staple']:
        with open(args[-1], 'ab') as stream:
            stream.write(b'fixture ticket')
'''
            for name in ('codesign', 'hdiutil', 'xcrun', 'gh'):
                path = tools / name
                path.write_text(code)
                path.chmod(0o755)
            env = dict(os.environ, PATH=str(tools) + os.pathsep + os.environ.get('PATH', ''),
                       RUNNER_TEMP=str(root), GITHUB_ACTIONS='true', GITHUB_EVENT_NAME='workflow_dispatch',
                       GITHUB_SHA=COMMIT, GITHUB_RUN_ID='42', GITHUB_RUN_ATTEMPT='3',
                       MACOS_TEAM_ID='TEAM', APPLE_NOTARIZATION_KEY_ID='FIXTURE', APPLE_NOTARIZATION_ISSUER_ID='FIXTURE',
                       APPLE_NOTARIZATION_PRIVATE_KEY='-----BEGIN PRIVATE KEY-----\nfixture-only-non-key\n-----END PRIVATE KEY-----',
                       FIXTURE_EVENTS=str(events), FIXTURE_TIMEOUT='true')
            first = root / 'notary-1'
            first.mkdir()
            dmg = first / 'sample.dmg'
            dmg.write_bytes(b'fixture signed input')
            original_sha = c.digest(dmg)
            receipt = dmg.with_suffix('.notary.json')
            def run_notary(dmg, receipt, environment):
                return subprocess.run([sys.executable, '-B', ci_security.__file__, 'notarize', '--ci', '--allow-remote',
                                       '--dmg', str(dmg), '--receipt', str(receipt)], env=environment,
                                      capture_output=True, text=True, timeout=10)
            failed = run_notary(dmg, receipt, env)
            self.assertNotEqual(failed.returncode, 0)
            self.assertEqual(c.digest(dmg), original_sha)
            self.assertEqual(json.loads(receipt.with_suffix('.submission.json').read_text())['artifactSha256'], original_sha)
            self.assertFalse(list(root.glob('release-notary-*')))
            archive_path = root / 'checkpoint.zip'
            import zipfile
            with zipfile.ZipFile(archive_path, 'w') as archive:
                for path in first.glob('*.json'):
                    archive.write(path, path.name)
            second = root / 'notary-3'
            output = second / 'dist/release/universal'
            output.mkdir(parents=True)
            shutil.copyfile(dmg, output / dmg.name)
            env.update(FIXTURE_TIMEOUT='false', FIXTURE_CHECKPOINT_ZIP=str(archive_path))
            with patch.dict(os.environ, env), patch.object(release, 'github_json', side_effect=[
                    {'head_sha': COMMIT, 'event': 'workflow_dispatch'},
                    [{'artifacts': [{'name': 'release-notary-state-1', 'expired': False}]}]]), contextlib.redirect_stderr(io.StringIO()):
                pipeline.restore_notary_state(second, CONFIG)
            succeeded = run_notary(output / dmg.name, output / receipt.name, env)
            self.assertEqual(succeeded.returncode, 0, succeeded.stderr)
            self.assertEqual(json.loads((output / receipt.name).read_text())['status'], 'Accepted')
            calls = [json.loads(line) for line in events.read_text().splitlines()]
            self.assertEqual(calls.count(['notarytool', 'submit']), 1)
            self.assertEqual(calls.count(['notarytool', 'wait']), 2)
            self.assertEqual(calls.count(['stapler', 'staple']), 1)
            self.assertFalse(list(root.glob('release-notary-*')))
            self.assertNotIn('fixture-only-non-key', failed.stdout + failed.stderr + succeeded.stdout + succeeded.stderr)
