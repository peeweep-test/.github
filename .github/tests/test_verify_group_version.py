import copy
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import mock_open, patch
from urllib.parse import urlsplit

import yaml


ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = yaml.safe_load((ROOT / '.github/workflows/verify-group-version.yml').read_text())
RUN = next(s['run'] for s in WORKFLOW['jobs']['verify']['steps'] if s.get('id') == 'verify')
CODE = RUN.split("<<'PY'", 1)[1].split('\n', 1)[1].rsplit('\nPY', 1)[0]


class VerifyGroupVersionTests(unittest.TestCase):
    def setUp(self):
        self.repo = 'linuxdeepin/go-lib'
        self.cfg = {'groups': {'G2': {'version': '6.0.49', 'members': ['go-lib']}}}
        self.pr = {'number': 1, 'state': 'open', 'created_at': '2026-11-05T10:01:00Z',
                   'base': {'ref': 'master', 'sha': 'member-base'},
                   'head': {'ref': 'automation/update-changelog/6.0.50', 'sha': 'member-head',
                            'repo': {'full_name': self.repo}}}
        self.release = {'group': 'G2', 'version': '6.0.50', 'base_sha': 'baseline-base',
                        'base_branch': 'master', 'started_at': '2026-11-05T10:00:00Z',
                        'members': [{'repo': self.repo, 'base': 'master',
                                     'default_branch': 'master', 'workflow': 'call-update-changelog.yml'}]}
        self.baseline_pr = {'base': {'ref': 'master'},
                            'head': {'ref': 'baseline/G2', 'sha': 'baseline-head',
                                     'repo': {'full_name': 'linuxdeepin/dde'}}}
        self.baseline_prs = [self.baseline_pr]
        self.versions = {'member-base': '6.0.49', 'member-head': '6.0.50'}
        self.commits = {}
        self.calls = []
        self.ns = {'__name__': 'verify_test'}
        exec(compile(CODE, 'verify-group-version.py', 'exec'), self.ns)
        self.ns.update(api=self.api, content=self.content,
                       head_version=lambda repo, sha: self.versions[sha])

    def api(self, url):
        self.calls.append(url)
        path = urlsplit(url).path
        baseline = self.repo.split('/', 1)[0] + '/dde'
        if path == f'repos/{baseline}':
            return {'default_branch': 'master'}
        if path == f'repos/{baseline}/pulls':
            return copy.deepcopy(self.baseline_prs)
        if path.startswith(f'repos/{baseline}/git/commits/'):
            sha = path.rsplit('/', 1)[1]
            return self.commits.get(sha, {
                'message': self.ns['MARKER'] + json.dumps(self.release) + '\n-->',
                'parents': [{'sha': 'baseline-base'}],
            })
        raise AssertionError(url)

    def content(self, repo, path, sha):
        baseline = self.repo.split('/', 1)[0] + '/dde'
        self.assertEqual((repo, path, sha), (baseline, 'groups.yaml', 'master'))
        return yaml.safe_dump(self.cfg)

    def check(self):
        return self.ns['check'](self.repo, self.pr)

    def test_ordinary_pr_without_version_change_needs_no_baseline_access(self):
        self.versions['member-head'] = '6.0.49'
        self.pr['head']['repo']['full_name'] = 'contributor/go-lib'
        self.pr['head']['ref'] = 'fix/service'
        self.assertEqual(self.check(), 'changelog 版本未变化')
        self.assertEqual(self.calls, [])

    def test_authorized_release_and_first_mixed_version_release_pass(self):
        for current in ('6.0.49', '6.0.15'):
            with self.subTest(current=current):
                self.versions['member-base'] = current
                self.assertIn('G2 本轮发布 6.0.50', self.check())

    def test_next_version_cannot_merge_during_original_release(self):
        self.versions.update({'member-base': '6.0.50', 'member-head': '6.0.51'})
        for branch in ('feature/bump', 'automation/update-changelog/6.0.51',
                       'automation/update-changelog/6.0.50'):
            with self.subTest(branch=branch):
                self.pr['head']['ref'] = branch
                with self.assertRaisesRegex(RuntimeError, '不属于 G2 本轮 6.0.50'):
                    self.check()

    def test_version_change_without_canonical_release_is_blocked(self):
        self.baseline_prs = []
        with self.assertRaisesRegex(RuntimeError, '没有统一发布记录'):
            self.check()

    def test_wrong_member_pr_identity_is_blocked(self):
        original = copy.deepcopy(self.pr)
        cases = (
            {'head': {**original['head'], 'ref': 'feature/bump'}},
            {'head': {**original['head'], 'repo': {'full_name': 'contributor/go-lib'}}},
            {'base': {'ref': 'other-branch', 'sha': 'member-base'}},
            {'created_at': '2026-11-05T09:59:59Z'},
        )
        for changes in cases:
            with self.subTest(changes=changes):
                self.pr = {**copy.deepcopy(original), **changes}
                with self.assertRaisesRegex(RuntimeError, '不属于 G2 本轮'):
                    self.check()

    def test_custom_member_branch_matches_original_release(self):
        self.release['members'][0]['base'] = 'release/stable'
        self.pr['base']['ref'] = 'release/stable'
        self.assertIn('发布记录一致', self.check())

    def test_closed_baseline_does_not_cancel_original_member_release(self):
        self.baseline_pr.update(state='closed', merged_at=None)
        self.assertIn('发布记录一致', self.check())

    def test_prematurely_merged_baseline_can_finish_original_member_pr(self):
        self.baseline_pr.update(state='closed', merged_at='2026-11-05T10:02:00Z')
        self.cfg['groups']['G2']['version'] = '6.0.50'
        self.assertIn('发布记录一致', self.check())
        self.versions.update({'member-base': '6.0.50', 'member-head': '6.0.51'})
        with self.assertRaisesRegex(RuntimeError, '不属于 G2 本轮'):
            self.check()

    def test_g1_branch_and_canonical_repo_mapping(self):
        self.repo = 'linuxdeepin/dde-qtintegration'
        self.cfg = {'repo_overrides': {'qt5integration': 'dde-qtintegration'},
                    'groups': {'G1': {'version': '6.7.52', 'members': ['qt5integration']}}}
        self.release.update(group='G1', version='6.7.53')
        self.release['members'][0]['repo'] = self.repo
        self.baseline_pr['head']['ref'] = 'baseline/G1'
        self.pr['head'].update(ref='release-6.7.53', repo={'full_name': self.repo})
        self.versions.update({'member-base': '6.7.52', 'member-head': '6.7.53'})
        self.assertIn('G1 本轮发布 6.7.53', self.check())

    def test_merge_update_keeps_original_release_target(self):
        self.commits['baseline-head'] = {'message': 'Merge master into baseline',
                                         'parents': [{'sha': 'original-release'}, {'sha': 'new-base'}]}
        self.assertIn('发布记录一致', self.check())

    def test_merge_update_limit_matches_baseline_controller(self):
        for depth in (100, 101):
            with self.subTest(depth=depth):
                self.commits.clear()
                self.baseline_pr['head']['sha'] = 'merge-0'
                for i in range(depth):
                    self.commits[f'merge-{i}'] = {
                        'message': 'Merge master into baseline',
                        'parents': [{'sha': f'merge-{i + 1}'}, {'sha': 'new-base'}],
                    }
                if depth == 100:
                    self.assertIn('发布记录一致', self.check())
                else:
                    with self.assertRaisesRegex(RuntimeError, '缺少原始发布记录'):
                        self.check()

    def test_test_organization_uses_its_own_baseline(self):
        self.repo = 'peeweep-test/go-lib'
        self.pr['head']['repo']['full_name'] = self.repo
        self.release['members'][0]['repo'] = self.repo
        self.baseline_pr['head']['repo']['full_name'] = 'peeweep-test/dde'
        self.assertIn('发布记录一致', self.check())
        self.assertIn('repos/peeweep-test/dde', self.calls)

    def test_missing_or_rewritten_release_record_is_blocked(self):
        self.commits['baseline-head'] = {'message': 'manual baseline update', 'parents': [{'sha': 'baseline-base'}]}
        with self.assertRaisesRegex(RuntimeError, '缺少原始发布记录'):
            self.check()
        self.commits.clear()
        self.release['base_sha'] = 'rewritten-base'
        with self.assertRaisesRegex(RuntimeError, '发布记录已改变'):
            self.check()

    def test_stale_release_and_changed_members_are_blocked(self):
        self.cfg['groups']['G2']['version'] = '6.0.51'
        with self.assertRaisesRegex(RuntimeError, '发布记录已过期'):
            self.check()
        self.cfg['groups']['G2']['version'] = '6.0.49'
        self.cfg['groups']['G2']['members'].append('dde-api')
        with self.assertRaisesRegex(RuntimeError, '本轮成员或目标分支已改变'):
            self.check()

    def test_wrong_baseline_source_or_target_is_blocked(self):
        for field, value in (('head', {'sha': 'baseline-head', 'repo': {'full_name': 'contributor/dde'}}),
                             ('base', {'ref': 'other-branch'})):
            with self.subTest(field=field):
                old = self.baseline_pr[field]
                self.baseline_pr[field] = value
                with self.assertRaisesRegex(RuntimeError, '来源或目标分支不符'):
                    self.check()
                self.baseline_pr[field] = old

    def test_api_failure_is_not_permission_to_change_version(self):
        def unavailable(path):
            raise RuntimeError('GitHub API unavailable')
        self.ns['api'] = unavailable
        with self.assertRaisesRegex(RuntimeError, 'GitHub API unavailable'):
            self.check()

    def test_newer_pr_head_cannot_get_a_result_under_the_old_sha(self):
        self.ns['api'] = lambda path: self.pr
        with patch.dict(os.environ, GITHUB_REPOSITORY=self.repo, GITHUB_EVENT_PATH='event.json',
                        PR_HEAD_SHA='previous-head'):
            with patch('builtins.open', mock_open(read_data=json.dumps({'number': 1}))):
                with self.assertRaisesRegex(RuntimeError, 'PR 提交已变化'):
                    self.ns['main']()

    def test_epoch_version_cannot_be_silently_downgraded(self):
        self.versions['member-base'] = '1:6.0.49'
        with self.assertRaisesRegex(RuntimeError, '成员版本只能升版'):
            self.check()

    def test_authorized_epoch_target_matches_frozen_member_record(self):
        self.release['members'][0]['version'] = '1:6.0.50'
        self.pr['head']['ref'] = 'automation/update-changelog/1-6.0.50'
        self.versions['member-head'] = '1:6.0.50'
        for current in ('1:6.0.15', '1:6.0.49'):
            with self.subTest(current=current):
                self.versions['member-base'] = current
                self.assertIn('G2 本轮发布 1:6.0.50', self.check())
        self.cfg['groups']['G2']['version'] = '6.0.50'
        self.assertIn('发布记录一致', self.check())

    def test_epoch_change_and_independent_next_version_are_blocked(self):
        self.release['members'][0]['version'] = '1:6.0.50'
        self.versions['member-base'] = '1:6.0.49'
        for after, branch in (
                ('6.0.50', 'automation/update-changelog/6.0.50'),
                ('2:6.0.50', 'automation/update-changelog/2-6.0.50'),
                ('1:6.0.51', 'automation/update-changelog/1-6.0.51'),
                ('1:6.0.50', 'automation/update-changelog/6.0.50')):
            with self.subTest(after=after, branch=branch):
                self.versions['member-head'] = after
                self.pr['head']['ref'] = branch
                with self.assertRaises(RuntimeError):
                    self.check()

    def test_member_target_cannot_disagree_with_group_record(self):
        self.release['members'][0]['version'] = '1:6.0.51'
        self.versions.update({'member-base': '1:6.0.49', 'member-head': '1:6.0.51'})
        self.pr['head']['ref'] = 'automation/update-changelog/1-6.0.51'
        with self.assertRaisesRegex(RuntimeError, '成员目标版本与组版本不符'):
            self.check()

    def test_legacy_epoch_group_record_keeps_original_full_target(self):
        self.release['version'] = '1:6.0.50'
        self.versions.update({'member-base': '1:6.0.49', 'member-head': '1:6.0.50'})
        self.pr['head']['ref'] = 'automation/update-changelog/1-6.0.50'
        self.assertIn('G2 本轮发布 1:6.0.50', self.check())

    def test_bare_group_release_can_follow_legacy_epoch_baseline(self):
        self.release['version'] = '6.0.51'
        self.release['members'][0]['version'] = '1:6.0.51'
        self.pr['head']['ref'] = 'automation/update-changelog/1-6.0.51'
        self.versions.update({'member-base': '1:6.0.50', 'member-head': '1:6.0.51'})
        for current in ('1:6.0.50', '1:6.0.51', '6.0.51'):
            with self.subTest(current=current):
                self.cfg['groups']['G2']['version'] = current
                self.assertIn('G2 本轮发布 1:6.0.51', self.check())
        for current in ('1:6.0.52', '6.0.52'):
            with self.subTest(current=current):
                self.cfg['groups']['G2']['version'] = current
                with self.assertRaisesRegex(RuntimeError, '发布记录已过期'):
                    self.check()

    def test_trusted_workflow_and_status_cover_all_prs(self):
        caller = yaml.safe_load((ROOT / 'workflow-templates/call-verify-group-version.yml').read_text())
        events = caller.get('on', caller.get(True))
        self.assertEqual(set(events), {'pull_request_target'})
        self.assertIn('edited', events['pull_request_target']['types'])
        self.assertIn('closed', events['pull_request_target']['types'])
        self.assertNotIn('paths', events['pull_request_target'])
        self.assertEqual(caller['permissions'], {'contents': 'read', 'pull-requests': 'read', 'statuses': 'write'})
        self.assertEqual(caller['jobs']['verify']['uses'],
                         'linuxdeepin/.github/.github/workflows/verify-group-version.yml@master')
        self.assertNotIn('actions/checkout', str(WORKFLOW))
        self.assertFalse(WORKFLOW['jobs']['verify']['concurrency']['cancel-in-progress'])
        steps = WORKFLOW['jobs']['verify']['steps']
        self.assertIn('state=pending', steps[0]['run'])
        self.assertEqual(steps[-1]['if'], 'always()')

    def test_failed_or_skipped_validation_publishes_failure_on_pr_head(self):
        step = WORKFLOW['jobs']['verify']['steps'][-1]
        with tempfile.TemporaryDirectory() as directory:
            gh = Path(directory) / 'gh'
            log = Path(directory) / 'calls.json'
            gh.write_text('#!/usr/bin/env python3\nimport json, os, pathlib, sys\n'
                          'pathlib.Path(os.environ["TEST_GH_LOG"]).write_text(json.dumps(sys.argv[1:]))\n')
            gh.chmod(0o755)
            for outcome in ('success', 'failure', 'skipped', 'cancelled'):
                with self.subTest(outcome=outcome):
                    env = {**os.environ, 'PATH': directory + os.pathsep + os.environ['PATH'],
                           'TEST_GH_LOG': str(log), 'VERIFY_OUTCOME': outcome,
                           'GITHUB_REPOSITORY': self.repo, 'PR_HEAD_SHA': 'member-head',
                           'GITHUB_SERVER_URL': 'https://github.com', 'GITHUB_RUN_ID': '1'}
                    subprocess.run(['bash', '-e', '-c', step['run']], env=env, check=True)
                    args = json.loads(log.read_text())
                    self.assertEqual(args[1], 'repos/linuxdeepin/go-lib/statuses/member-head')
                    self.assertIn('state=' + ('success' if outcome == 'success' else 'failure'), args)
                    self.assertIn('context=group-version-guard', args)

    def test_same_head_sha_rechecks_all_open_prs_and_blocks_wrong_target(self):
        wrong = {**copy.deepcopy(self.pr), 'number': 2, 'state': 'open',
                 'base': {'ref': 'review/forbidden', 'sha': 'member-base'}}
        original = self.ns['api']
        def api_with_wrong_pr(path):
            path_only = urlsplit(path).path
            if path_only == f'repos/{self.repo}/pulls/1':
                return self.pr
            if path_only == f'repos/{self.repo}/commits/member-head/pulls':
                return [wrong]
            return original(path)
        self.ns['api'] = api_with_wrong_pr
        with patch.dict(os.environ, GITHUB_REPOSITORY=self.repo, GITHUB_EVENT_PATH='event.json',
                        PR_HEAD_SHA='member-head'):
            with patch('builtins.open', mock_open(read_data=json.dumps({'number': 1}))):
                with self.assertRaisesRegex(RuntimeError, '不属于 G2 本轮'):
                    self.ns['main']()
        self.calls.append(f'repos/{self.repo}/commits/member-head/pulls?per_page=100')

    def test_shared_sha_status_is_failure_when_any_open_pr_is_closed(self):
        step = WORKFLOW['jobs']['verify']['steps'][-1]
        with tempfile.TemporaryDirectory() as directory:
            gh = Path(directory) / 'gh'
            log = Path(directory) / 'calls.json'
            gh.write_text('#!/usr/bin/env python3\nimport json, os, pathlib, sys\n'
                          'pathlib.Path(os.environ["TEST_GH_LOG"]).write_text(json.dumps(sys.argv[1:]))\n'
                          'if sys.argv[1] == "api":\n'
                          '    print("1")\n'
                          'elif sys.argv[1] == "pr" and sys.argv[2] == "view":\n'
                          '    print(os.environ["REVIEW_PR_STATE"])\n')
            gh.chmod(0o755)
            env = {**os.environ, 'PATH': directory + os.pathsep + os.environ['PATH'],
                   'TEST_GH_LOG': str(log), 'VERIFY_OUTCOME': 'success',
                   'GITHUB_REPOSITORY': self.repo, 'PR_HEAD_SHA': 'member-head',
                   'GITHUB_SERVER_URL': 'https://github.com', 'GITHUB_RUN_ID': '1',
                   'REVIEW_PR_STATE': 'closed'}
            subprocess.run(['bash', '-e', '-c', step['run']], env=env, check=True)
            args = json.loads(log.read_text())
            self.assertIn('state=failure', args)

    def test_shared_sha_status_accepts_open_pr_state_from_gh_cli(self):
        step = WORKFLOW['jobs']['verify']['steps'][-1]
        with tempfile.TemporaryDirectory() as directory:
            gh = Path(directory) / 'gh'
            log = Path(directory) / 'calls.json'
            gh.write_text('#!/usr/bin/env python3\nimport json, os, pathlib, sys\n'
                          'pathlib.Path(os.environ["TEST_GH_LOG"]).write_text(json.dumps(sys.argv[1:]))\n'
                          'if sys.argv[1] == "api":\n'
                          '    print("1")\n'
                          'elif sys.argv[1] == "pr" and sys.argv[2] == "view":\n'
                          '    print("OPEN")\n')
            gh.chmod(0o755)
            env = {**os.environ, 'PATH': directory + os.pathsep + os.environ['PATH'],
                   'TEST_GH_LOG': str(log), 'VERIFY_OUTCOME': 'success',
                   'GITHUB_REPOSITORY': self.repo, 'PR_HEAD_SHA': 'member-head',
                   'GITHUB_SERVER_URL': 'https://github.com', 'GITHUB_RUN_ID': '1'}
            subprocess.run(['bash', '-e', '-c', step['run']], env=env, check=True)
            args = json.loads(log.read_text())
            self.assertIn('state=success', args)

    def test_closed_pr_recomputes_only_remaining_open_prs(self):
        closed = {**copy.deepcopy(self.pr), 'number': 2, 'state': 'closed'}
        valid = {**copy.deepcopy(self.pr), 'state': 'open'}
        original = self.ns['api']
        def api_with_closed_pr(path):
            path_only = urlsplit(path).path
            if path_only == f'repos/{self.repo}/pulls/2':
                return closed
            if path_only == f'repos/{self.repo}/commits/member-head/pulls':
                return [closed, valid]
            return original(path)
        self.ns['api'] = api_with_closed_pr
        with patch.dict(os.environ, GITHUB_REPOSITORY=self.repo, GITHUB_EVENT_PATH='event.json',
                        PR_HEAD_SHA='member-head'):
            with patch('builtins.open', mock_open(read_data=json.dumps({'number': 2, 'action': 'closed'}))):
                self.ns['main']()


if __name__ == '__main__':
    unittest.main()
