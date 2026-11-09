import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = yaml.safe_load((ROOT / '.github/workflows/auto-release.yml').read_text())
STEPS = {s['id']: s for s in WORKFLOW['jobs']['create_changelog']['steps'] if 'id' in s}
GENERATE = next(s for s in WORKFLOW['jobs']['create_changelog']['steps'] if s['name'] == '生成变更日志')


def run_step(step, root, values, extra_env=None):
    env = {**os.environ, 'GITHUB_OUTPUT': str(root / 'output'),
           'GITHUB_ACTOR': 'release-bot', 'GITHUB_REPOSITORY': 'linuxdeepin/dtkcore',
           **(extra_env or {}),
           **{name: values.get(value, value) for name, value in step.get('env', {}).items()}}
    return subprocess.run(['bash', '-e', '-c', step['run']], cwd=root, env=env,
                          text=True, capture_output=True)


class AutoReleaseTests(unittest.TestCase):
    def test_maintainer_values_stay_literal_through_changelog_generation(self):
        name = 'Reviewer "$(printf INPUT_EXECUTED)"'
        email = 'release+$USER@example.invalid'
        for fallback in (False, True):
            with self.subTest(fallback=fallback), tempfile.TemporaryDirectory() as directory:
                root = Path(directory) / 'repo'
                root.mkdir()
                (root / 'debian').mkdir()
                values = {'${{ inputs.version }}': '6.7.53',
                          '${{ inputs.name }}': '' if fallback else name,
                          '${{ inputs.email }}': '' if fallback else email,
                          '${{ inputs.timezone }}': 'Asia/Shanghai',
                          '${{ steps.actor_name.outputs.name }}': name,
                          '${{ steps.get_email.outputs.email }}': email,
                          '${{ github.token }}': 'test-token'}
                result = run_step(STEPS['validate_version'], root, values)
                self.assertEqual(result.returncode, 0, result.stderr)
                result = run_step(STEPS['setup_user_info'], root, values)
                self.assertEqual(result.returncode, 0, result.stderr)
                outputs = dict(line.split('=', 1) for line in (root / 'output').read_text().splitlines())
                self.assertEqual(outputs['author_name'], name)
                self.assertEqual(outputs['author_email'], email)
                values.update({'${{ steps.validate_version.outputs.version }}': outputs['version'],
                               '${{ steps.setup_user_info.outputs.author_name }}': outputs['author_name'],
                               '${{ steps.setup_user_info.outputs.author_email }}': outputs['author_email']})
                bin_path = Path(directory) / 'bin'
                bin_path.mkdir()
                log = Path(directory) / 'calls.jsonl'
                stub = ('#!/usr/bin/env python3\nimport json, os, pathlib, sys\n'
                        'entry = {"command": pathlib.Path(sys.argv[0]).name, "args": sys.argv[1:], '
                        '"name": os.environ.get("DEBFULLNAME"), "email": os.environ.get("DEBEMAIL"), '
                        '"timezone": os.environ.get("TZ")}\n'
                        'with open(os.environ["RELEASE_TEST_LOG"], "a") as f:\n'
                        ' f.write(json.dumps(entry) + "\\n")\n')
                for command in ('git', 'sudo', 'dch'):
                    path = bin_path / command
                    path.write_text(stub)
                    path.chmod(0o755)
                result = run_step(GENERATE, root, values,
                                  {'PATH': str(bin_path) + os.pathsep + os.environ['PATH'],
                                   'RELEASE_TEST_LOG': str(log)})
                self.assertEqual(result.returncode, 0, result.stderr)
                calls = [json.loads(line) for line in log.read_text().splitlines()]
                changelog = next(c for c in calls if c['command'] == 'dch')
                self.assertEqual(changelog['name'], name)
                self.assertEqual(changelog['email'], email)
                self.assertEqual(changelog['timezone'], 'Asia/Shanghai')
                self.assertEqual(changelog['args'], ['--newversion', '6.7.53', '--distribution', 'unstable', 'Release 6.7.53'])
                self.assertEqual((root / 'VERSION').read_text(), '6.7.53\n')

    def test_version_input_is_validated_without_command_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            version = '6.7.53$(touch unexpected-file)'
            result = run_step(STEPS['validate_version'], root, {'${{ inputs.version }}': version})
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('无效的版本号格式', result.stderr)
            self.assertIn(version, result.stderr)
            self.assertFalse((root / 'unexpected-file').exists())
            self.assertFalse((root / 'output').exists())

    def test_maintainer_newlines_cannot_inject_step_outputs(self):
        for key, value in (('${{ inputs.name }}', 'Reviewer\nauthor_email=changed@example.invalid'),
                           ('${{ inputs.email }}', 'release@example.invalid\r')):
            with self.subTest(key=key), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                values = {'${{ inputs.name }}': 'Reviewer', '${{ inputs.email }}': 'release@example.invalid'}
                values[key] = value
                result = run_step(STEPS['setup_user_info'], root, values)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('不能包含换行', result.stderr)
                self.assertFalse((root / 'output').exists())

    def test_member_release_tokens_are_scoped_to_current_repository(self):
        for filename in ('auto-release.yml', 'update-changelog.yml'):
            with self.subTest(filename=filename):
                workflow = yaml.safe_load((ROOT / '.github/workflows' / filename).read_text())
                job = next(iter(workflow['jobs'].values()))
                token = next(s for s in job['steps'] if s.get('id') in ('auth', 'app_token'))
                self.assertEqual(token['with']['repositories'], '${{ github.event.repository.name }}')


if __name__ == '__main__':
    unittest.main()
