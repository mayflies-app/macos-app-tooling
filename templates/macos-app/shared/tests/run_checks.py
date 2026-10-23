#!/usr/bin/env python3
"""Compile and run eight native AppKit windowless design component scenarios."""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import re
import resource
import signal
import subprocess
import sys
import tempfile
import uuid

HERE = Path(__file__).resolve().parent
SOURCE = HERE.parent / 'MacAppDesign.swift'
XCRUN = '/usr/bin/xcrun'
SCOPE = 'native AppKit component behavior without a window; test-only workspace substitute for both Reduce Transparency values; no product UI acceptance'


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def now():
    return datetime.now(timezone.utc).isoformat()


def positive_timeout(value):
    try: number = float(value)
    except ValueError: raise argparse.ArgumentTypeError('Timeout must be a finite positive number')
    if not math.isfinite(number) or number <= 0 or number > 3600:
        raise argparse.ArgumentTypeError('Timeout must be finite and in (0, 3600]')
    return number


def validate_output(path):
    path = Path(path)
    if not path.is_absolute():
        raise ValueError('--output must be absolute')
    if path.is_symlink():
        raise ValueError('Output cannot be a symlink')
    path = path.resolve()
    if path.exists():
        if not path.is_dir() or any(path.iterdir()):
            raise ValueError('Output must be a new directory or an existing empty directory')
    elif not path.parent.is_dir():
        raise ValueError('Output parent must already exist')
    if not os.access(path if path.exists() else path.parent, os.W_OK):
        raise ValueError('Output must be writable')
    return path


def atomic_json(path, value):
    temporary = path.with_name('.' + path.name + '.partial')
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False) + '\n')
    os.replace(temporary, path)


def run_command(report, output, label, argv, timeout, env=None):
    record = {'label': label, 'argv': [str(value) for value in argv], 'timeoutSeconds': timeout,
              'startedAt': now(), 'timedOut': False}
    try:
        result = subprocess.run(record['argv'], capture_output=True, text=True, env=env, timeout=timeout)
        record.update(code=result.returncode, stdout=result.stdout, stderr=result.stderr)
    except subprocess.TimeoutExpired as error:
        def text(value): return value.decode(errors='replace') if isinstance(value, bytes) else value or ''
        record.update(code=None, timedOut=True, stdout=text(error.stdout), stderr=text(error.stderr))
    record['endedAt'] = now()
    prefix = f'{len(report["commands"]):02d}-{label}'
    for stream in ('stdout', 'stderr'):
        path = output / f'{prefix}.{stream}.log'
        path.write_text(record[stream]); record[stream + 'Log'] = str(path); record[stream + 'SHA256'] = sha(path)
    report['commands'].append(record)
    if record['timedOut']:
        raise RuntimeError(f'{label} exceeded its per-command timeout; subprocess.run terminated its direct child')
    return record


def required_command(report, output, label, argv, timeout):
    record = run_command(report, output, label, argv, timeout)
    if record['code'] != 0:
        raise RuntimeError(f'{label} failed; see captured stderr')
    return record['stdout'].strip()


def execute(args, output):
    sources = [SOURCE, HERE / 'DesignChecks.swift', HERE / 'ControlledWorkspace.swift']
    report = {'schemaVersion': 1, 'runID': str(uuid.uuid4()), 'startedAt': now(), 'status': 'failed',
              'scope': SCOPE, 'source': str(SOURCE), 'sha256': sha(SOURCE),
              'inputs': {str(path): sha(path) for path in sources}, 'commands': [], 'cases': [], 'errors': [],
              'realUserPreferencesChanged': False, 'productApplicationsLaunched': False, 'systemRegistrationPerformed': False,
              'timeoutPolicy': 'per compiler/discovery/fixture command; direct child only; no process-name or user-app targeting',
              'platform': platform.platform(), 'runtimeMacOS': platform.mac_ver()[0], 'architecture': platform.machine()}
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    env = dict(os.environ, SWIFT_BACKTRACE='disable')
    try:
        sdk_version = required_command(report, output, 'sdk-version', [XCRUN, '--sdk', 'macosx', '--show-sdk-version'], args.timeout_seconds)
        if not re.fullmatch(r'\d+(?:\.\d+)*', sdk_version) or int(sdk_version.split('.')[0]) < 27:
            raise RuntimeError('macOS SDK >= 27 is required; select an installed Xcode with that SDK')
        sdk = required_command(report, output, 'sdk-path', [XCRUN, '--sdk', 'macosx', '--show-sdk-path'], args.timeout_seconds)
        swift = required_command(report, output, 'swift-version', [XCRUN, '--sdk', 'macosx', 'swiftc', '--version'], args.timeout_seconds)
        report['toolchain'] = {'sdkVersion': sdk_version, 'sdkPath': sdk, 'swiftVersion': swift,
                               'developerDirectoryOverride': os.environ.get('DEVELOPER_DIR', 'not set; current xcrun selection')}
        arch = platform.machine()
        if arch not in ('arm64', 'x86_64'): raise RuntimeError('Unsupported host architecture')
        with tempfile.TemporaryDirectory(prefix='macapp-design-build-', dir='/private/tmp') as td:
            build = Path(td)
            base = [XCRUN, '--sdk', 'macosx', 'swiftc', '-parse-as-library', '-sdk', sdk,
                    '-target', arch + '-apple-macosx14.0', '-module-cache-path', str(build / 'module-cache')]
            required_command(report, output, 'compile-availability-module', base + ['-emit-module', '-module-name', 'MacAppDesign', str(SOURCE), '-emit-module-path', str(build / 'MacAppDesign.swiftmodule')], args.timeout_seconds)
            for binary, extra in [('DesignChecks', []), ('ControlledDesignChecks', [str(HERE / 'ControlledWorkspace.swift')])]:
                required_command(report, output, 'compile-' + binary, base + [str(SOURCE)] + extra + [str(HERE / 'DesignChecks.swift'), '-o', str(build / binary)], args.timeout_seconds)
            report['binarySHA256'] = {name: sha(build / name) for name in ('DesignChecks', 'ControlledDesignChecks')}
            for binary, mode, expected in [('DesignChecks', 'actual', None), ('DesignChecks', 'forced', 'standard'),
                                            ('ControlledDesignChecks', 'reduced', 'standard'), ('ControlledDesignChecks', 'glass', 'liquid-glass')]:
                record = run_command(report, output, 'case-' + mode, [str(build / binary), mode], args.timeout_seconds, env)
                record.update(binary=binary, mode=mode, expectation={'code': 0, 'material': expected})
                report['cases'].append(record)
                if record['code'] != 0: raise RuntimeError(f'{mode} failed')
                facts = json.loads(record['stdout']); record['facts'] = facts
                required = ('contentIdentityPreserved', 'nativeLayoutFillsSurface', 'accessibilityAndEditorPreserved', 'activationUnchanged', 'paletteLightDarkExactSRGB')
                if facts.get('status') != 'passed' or any(facts.get(key) is not True for key in required):
                    raise RuntimeError(f'{mode} lacks its native behavior evidence')
                if facts.get('windowCreated') is not False or facts.get('focusCallbackCount') != 0:
                    raise RuntimeError(f'{mode} changed the window/focus contract')
                if expected is not None and facts.get('material') != expected: raise RuntimeError(f'{mode} selected the wrong material')
                record['passed'] = True
            for mode, reason in [('invalid-parent', 'requires unparented content'), ('repeat-content', 'requires unparented content'),
                                 ('repeat-surface', 'requires content, not another material surface'), ('invalid-radius', 'requires a finite, nonnegative corner radius')]:
                record = run_command(report, output, 'case-' + mode, [str(build / 'DesignChecks'), mode], args.timeout_seconds, env)
                record.update(binary='DesignChecks', mode=mode, expectation={'termination': 'Swift precondition signal', 'stderrReason': reason})
                report['cases'].append(record)
                if record['code'] not in (-signal.SIGTRAP, -signal.SIGILL, -signal.SIGABRT) or 'Precondition failed:' not in record['stderr'] or reason not in record['stderr']:
                    raise RuntimeError(f'{mode} did not produce the expected Swift precondition failure')
                record['passed'] = True
        for path, expected in report['inputs'].items():
            if sha(path) != expected: raise RuntimeError(f'Test input changed during execution: {path}')
        report['status'] = 'passed'
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as error:
        report['errors'].append(str(error))
    finally:
        report['endedAt'] = now()
        atomic_json(output / 'results.json', report)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, help='Absolute new/empty run directory; default is a fresh directory in /private/tmp')
    parser.add_argument('--timeout-seconds', type=positive_timeout, default=120, help='Per subprocess timeout, default 120; maximum 3600')
    args = parser.parse_args(argv)
    try:
        # All CLI and output validation completes before xcrun/compiler invocation.
        output = validate_output(args.output) if args.output is not None else Path(tempfile.mkdtemp(prefix='macapp-design-evidence-', dir='/private/tmp'))
        if not output.exists(): output.mkdir()
        descriptor = os.open(output / '.run-reservation', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(descriptor)
        report = execute(args, output)
        print(json.dumps({'status': report['status'], 'cases': len(report['cases']), 'scope': SCOPE, 'results': str(output / 'results.json')}, indent=2))
        return 0 if report['status'] == 'passed' else 1
    except (OSError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 2


if __name__ == '__main__': sys.exit(main())
