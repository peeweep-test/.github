import copy
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import mock_open, patch
from urllib.parse import parse_qs, urlsplit

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
                            'head': {'ref': 'baseline-test-formal-prs-20261113/G2', 'sha': 'baseline-head',
                                     'repo': {'full_name': 'linuxdeepin/dde'}}}
        self.baseline_prs = [self.baseline_pr]
        self.versions = {'member-base': '6.0.49', 'member-head': '6.0.50'}
        self.files = {'member-base': {}, 'member-head': {}}
        self.trees = {}
        self.commits = {}
        self.calls = []
        self.ns = {'__name__': 'verify_test'}
        exec(compile(CODE, 'verify-group-version.py', 'exec'), self.ns)
        self.ns.update(api=self.api, content=self.content,
                       head_version=lambda repo, sha: self.versions[sha])

    def api(self, url, **kwargs):
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
        if '/git/trees/' in path:
            sha = path.rsplit('/', 1)[1]
            return self.trees.get(sha, {
                'truncated': False,
                'tree': [{'path': name, 'type': 'blob', 'mode': '100644'}
                         for name in self.files[sha]],
            })
        raise AssertionError(url)

    def content(self, repo, path, sha):
        if path != 'groups.yaml':
            return self.files[sha][path]
        baseline = self.repo.split('/', 1)[0] + '/dde'
        self.assertEqual((repo, path, sha), (baseline, 'groups.yaml', 'master'))
        return yaml.safe_dump(self.cfg)

    def check(self):
        return self.ns['check'](self.repo, self.pr)

    def use_release_line(self, line='develop-2500u1'):
        self.cfg['groups']['G2'].pop('version')
        self.cfg['groups']['G2']['versions'] = {
            'master': '6.0.49', 'develop-2500u1': '6.0.49', 'develop-2500u2': '6.0.49'}
        self.cfg['release_lines'] = {
            'master': {'member_branch': 'master'},
            'develop-2500u1': {'member_branch': 'develop/2500u1'},
            'develop-2500u2': {'member_branch': 'develop/2500u2'}}
        branch = self.cfg['release_lines'][line]['member_branch']
        self.pr['base']['ref'] = branch
        self.release['release_line'] = line
        self.release['members'][0]['base'] = branch
        self.baseline_pr['head']['ref'] = f'baseline-test-formal-prs-20261113/{line}/G2'

    def test_ordinary_pr_without_version_change_needs_no_release_record(self):
        self.versions['member-head'] = '6.0.49'
        self.pr['head']['repo']['full_name'] = 'contributor/go-lib'
        self.pr['head']['ref'] = 'fix/service'
        self.assertEqual(self.check(), '版本信息未变化')
        self.assertFalse(any('/pulls?' in call or '/git/commits/' in call for call in self.calls))

    def test_each_g2_line_uses_its_own_version_and_baseline(self):
        for line in ('master', 'develop-2500u1', 'develop-2500u2'):
            with self.subTest(line=line):
                self.setUp()
                self.use_release_line(line)
                self.cfg['groups']['G2']['versions'].update(
                    {other: '99.0.0' for other in self.cfg['release_lines'] if other != line})
                self.assertIn('发布记录一致', self.check())
                queries = [parse_qs(urlsplit(call).query) for call in self.calls if '/pulls?' in call]
                self.assertEqual(queries[0]['head'], [f'linuxdeepin:baseline-test-formal-prs-20261113/{line}/G2'])
                self.cfg['groups']['G2']['versions'][line] = '6.0.51'
                with self.assertRaisesRegex(RuntimeError, '发布记录已过期'):
                    self.check()

    def test_legacy_g2_ignores_new_line_metadata_until_versions_migrate(self):
        self.cfg['release_lines'] = {'master': {'member_branch': 'master'}}
        self.assertIn('发布记录一致', self.check())
        query = next(call for call in self.calls if '/pulls?' in call)
        self.assertEqual(parse_qs(urlsplit(query).query)['head'], ['linuxdeepin:baseline-test-formal-prs-20261113/G2'])

    def test_multiline_release_record_and_pr_identity_must_match(self):
        self.use_release_line()
        original_release, original_pr = copy.deepcopy(self.release), copy.deepcopy(self.pr)
        cases = (
            lambda: self.release.update(release_line='master'),
            lambda: self.release.pop('release_line'),
            lambda: self.release.update(group='G1'),
            lambda: self.release['members'][0].update(base='master'),
            lambda: self.release.update(started_at=''),
            lambda: self.release.update(started_at='2026-00-05T10:00:00Z'),
            lambda: self.release.pop('started_at'),
            lambda: self.pr.update(created_at='2026-11-05T09:59:59Z'),
            lambda: self.pr['head'].update(ref='automation/update-changelog/develop-2500u1/6.0.50'),
            lambda: self.pr['head'].update(repo={'full_name': 'contributor/go-lib'}),
        )
        for index, change in enumerate(cases):
            with self.subTest(case=index):
                self.release, self.pr = copy.deepcopy(original_release), copy.deepcopy(original_pr)
                change()
                with self.assertRaises(RuntimeError):
                    self.check()
        self.release, self.pr = original_release, original_pr
        self.baseline_pr['head']['ref'] = 'baseline-test-formal-prs-20261113/master/G2'
        with self.assertRaisesRegex(RuntimeError, '来源或目标分支不符'):
            self.check()

    def test_multiline_mapping_must_be_complete_unique_and_valid(self):
        self.use_release_line()
        original = copy.deepcopy(self.cfg)
        cases = (
            lambda: self.pr['base'].update(ref='develop/unknown'),
            lambda: self.cfg['release_lines']['master'].update(member_branch='develop/2500u1'),
            lambda: self.cfg['release_lines']['master'].update(member_branch='develop/main'),
            lambda: self.cfg['release_lines'].update({'ghost': {'member_branch': 'develop/ghost'}}),
            lambda: self.cfg['release_lines']['develop-2500u2'].update(member_branch='release/2500u2'),
            lambda: self.cfg['release_lines']['develop-2500u2'].update(member_branch='develop-2500u2'),
            lambda: self.cfg['release_lines'].pop('develop-2500u1'),
            lambda: self.cfg['groups']['G2']['versions'].pop('develop-2500u1'),
            lambda: self.cfg['release_lines'].update({'unsafe/line': {'member_branch': 'develop/unsafe'}}),
            lambda: self.cfg['release_lines']['develop-2500u1'].update(member_branch='develop/../bad'),
            lambda: self.cfg.pop('release_lines'),
            lambda: self.cfg['groups']['G2'].update(versions={}),
        )
        for index, change in enumerate(cases):
            with self.subTest(case=index):
                self.cfg = copy.deepcopy(original)
                self.pr['base']['ref'] = 'develop/2500u1'
                change()
                with self.assertRaisesRegex(RuntimeError, 'G2'):
                    self.check()

    def test_release_line_id_is_independent_of_nested_develop_branch(self):
        self.use_release_line()
        branch = 'develop/foo/bar'
        self.cfg['release_lines']['develop-2500u1']['member_branch'] = branch
        self.pr['base']['ref'] = branch
        self.release['members'][0]['base'] = branch
        self.assertIn('发布记录一致', self.check())

    def test_linglong_config_changes_with_unchanged_versions_pass_without_release(self):
        self.use_release_line('master')
        self.versions['member-head'] = '6.0.49'
        self.pr['head'].update(ref='integrate/fix', repo={'full_name': 'contributor/go-lib'})
        version = self.versions['member-base'] + '.1'
        self.files['member-base']['linglong.yaml'] = f'package:\n  version: {version}\nbuild: old\n'
        self.files['member-head']['linglong.yaml'] = f'package:\n  version: "{version}"\nbuild: new\n'
        self.baseline_prs = []
        self.assertEqual(self.check(), '版本信息未变化')
        self.assertFalse(any('/pulls?' in call for call in self.calls))

    def test_linglong_rollback_or_deletion_fails_even_without_changelog_change(self):
        self.versions['member-head'] = '6.0.49'
        version = self.versions['member-base'] + '.1'
        self.files['member-base'] = {
            'linglong.yaml': f'package:\n  version: {version}\n',
            'linglong-arm64.yaml': f'package:\n  version: {version}\n'}
        for after in ('6.0.48' + '.1', None):
            with self.subTest(after=after):
                self.files['member-head'] = copy.deepcopy(self.files['member-base'])
                if after is None:
                    self.files['member-head'].pop('linglong-arm64.yaml')
                else:
                    self.files['member-head']['linglong-arm64.yaml'] = f'package:\n  version: {after}\n'
                with self.assertRaisesRegex(RuntimeError, '版本只能升版|版本文件不能删除'):
                    self.check()

    def test_linglong_only_version_changes_still_require_current_release(self):
        self.versions.update({'member-base': '6.0.50', 'member-head': '6.0.50'})
        self.files['member-base']['linglong.yaml'] = 'package:\n  version: ' + '6.0.49' + '.1\n'
        self.files['member-head']['linglong.yaml'] = f'package:\n  version: {self.release["version"]}.1\n'
        self.assertIn('发布记录一致', self.check())
        self.pr['head']['ref'] = 'fix/linglong'
        with self.assertRaisesRegex(RuntimeError, '不属于 G2 本轮'):
            self.check()
        self.baseline_prs = []
        with self.assertRaisesRegex(RuntimeError, '没有统一发布记录'):
            self.check()

    def test_all_linglong_versions_must_match_full_member_target(self):
        self.release['members'][0]['version'] = '1:6.0.50'
        self.pr['head']['ref'] = 'automation/update-changelog/1-6.0.50'
        self.versions.update({'member-base': '1:6.0.49', 'member-head': '1:6.0.50'})
        before, after = self.versions['member-base'], self.versions['member-head']
        self.files['member-base'] = {
            'linglong.yaml': 'package:\n  version: 1:6.0.49\n',
            'linglong-arm64.yaml': f'package:\n  version: {before}.1\n'}
        self.files['member-head'] = {
            'linglong.yaml': 'package:\n  version: 1:6.0.50\n',
            'linglong-arm64.yaml': f'package:\n  version: {after}.1\n'}
        self.assertIn('发布记录一致', self.check())
        for version in (before + '.1', '1:6.0.51' + '.1', after + '.1.2'):
            with self.subTest(version=version):
                self.files['member-head']['linglong-arm64.yaml'] = f'package:\n  version: {version}\n'
                with self.assertRaisesRegex(RuntimeError, '与本轮成员目标'):
                    self.check()

    def test_missing_or_invalid_linglong_version_fails_on_either_side(self):
        self.versions['member-head'] = '6.0.49'
        for sha in ('member-base', 'member-head'):
            for text in ('', 'package: {}\n', 'package:\n  version: []\n',
                         'package:\n  version: ""\n', 'package:\n  version: invalid\n', 'package: [\n'):
                with self.subTest(sha=sha, text=text):
                    self.files = {'member-base': {}, 'member-head': {}}
                    self.files[sha]['linglong.yaml'] = text
                    with self.assertRaisesRegex(RuntimeError, '无法读取|version 无效'):
                        self.check()

    def test_version_file_detection_failure_cannot_pass_as_an_empty_file_set(self):
        self.versions['member-head'] = '6.0.49'
        for tree in (
                {'truncated': True, 'tree': []},
                {'truncated': False},
                {'truncated': False, 'tree': [
                    {'path': 'linglong.yaml', 'type': 'blob', 'mode': '120000'}]},
                {'truncated': False, 'tree': [
                    {'path': 'linglong.yaml', 'type': 'tree', 'mode': '040000'}]}):
            with self.subTest(tree=tree):
                self.trees['member-head'] = tree
                with self.assertRaisesRegex(RuntimeError, '无法完整检测|不是普通版本文件'):
                    self.check()
        self.trees.clear()
        self.files['member-head']['linglong.yaml'] = 'package:\n  version: 6.0.49\n'
        original = self.ns['content']
        def unavailable(repo, path, sha):
            if path == 'linglong.yaml':
                raise RuntimeError('GitHub API unavailable')
            return original(repo, path, sha)
        self.ns['content'] = unavailable
        with self.assertRaisesRegex(RuntimeError, 'GitHub API unavailable'):
            self.check()

    def test_missing_or_invalid_changelog_cannot_skip_validation(self):
        ns = {'__name__': 'changelog_test'}
        exec(compile(CODE, 'verify-group-version.py', 'exec'), ns)
        for text in ('', 'not a changelog\n'):
            with self.subTest(text=text):
                ns['content'] = lambda *args: text
                with self.assertRaisesRegex(RuntimeError, '无法读取'):
                    ns['head_version'](self.repo, 'member-head')
        def missing(*args):
            raise RuntimeError('changelog missing')
        ns['content'] = missing
        with self.assertRaisesRegex(RuntimeError, 'changelog missing'):
            ns['head_version'](self.repo, 'member-head')

    def test_g1_keeps_changelog_only_behavior_with_multiline_config_present(self):
        self.cfg = {
            'groups': {'G1': {'version': '6.0.49', 'members': ['go-lib']}},
            'release_lines': {'invalid/line': {'member_branch': 'bad branch'}}}
        self.versions['member-head'] = '6.0.49'
        self.files['member-head']['linglong.yaml'] = 'invalid: [\n'
        self.assertEqual(self.check(), 'changelog 版本未变化')
        self.assertFalse(any('/git/trees/' in call or '/pulls?' in call for call in self.calls))

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
        self.baseline_pr['head']['ref'] = 'baseline-test-formal-prs-20261113/G1'
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

    def test_paginated_api_flattens_all_open_pr_pages(self):
        ns = {'__name__': 'api_test'}
        exec(compile(CODE, 'verify-group-version.py', 'exec'), ns)
        response = subprocess.CompletedProcess(
            [], 0, '[[{"number": 1}], [{"number": 2}]]', '')
        with patch('subprocess.run', return_value=response) as run:
            self.assertEqual(ns['api']('repos/linuxdeepin/go-lib/pulls', paginate=True),
                             [{'number': 1}, {'number': 2}])
        self.assertIn('--paginate', run.call_args.args[0])
        self.assertIn('--slurp', run.call_args.args[0])

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
                         'peeweep-test/.github/.github/workflows/verify-group-version.yml@test/formal-prs-20261113')
        self.assertNotIn('actions/checkout', str(WORKFLOW))
        self.assertFalse(WORKFLOW['jobs']['verify']['concurrency']['cancel-in-progress'])
        steps = WORKFLOW['jobs']['verify']['steps']
        self.assertIn('state=pending', steps[0]['run'])
        self.assertEqual(steps[-1]['if'], 'always()')
        self.assertIn('pulls?state=open', steps[-1]['run'])
        self.assertIn('--paginate --slurp', steps[-1]['run'])

    def test_failed_or_skipped_validation_publishes_failure_on_pr_head(self):
        step = WORKFLOW['jobs']['verify']['steps'][-1]
        with tempfile.TemporaryDirectory() as directory:
            gh = Path(directory) / 'gh'
            log = Path(directory) / 'calls.json'
            verified = Path(directory) / 'verified-prs.json'
            current = Path(directory) / 'current.json'
            identity = {'number': 1, 'head_sha': 'member-head', 'head_repo': self.repo,
                        'head_ref': 'automation/update-changelog/6.0.50',
                        'base_ref': 'master'}
            verified.write_text(json.dumps([identity]))
            current.write_text(json.dumps([{**self.pr, 'state': 'open'}]))
            gh.write_text('#!/usr/bin/env python3\nimport json, os, pathlib, sys\n'
                          'pathlib.Path(os.environ["TEST_GH_LOG"]).write_text(json.dumps(sys.argv[1:]))\n'
                          'if sys.argv[1] == "api" and "/pulls?" in sys.argv[2]:\n'
                          '    print(pathlib.Path(os.environ["CURRENT_PRS"]).read_text(), end="")\n')
            gh.chmod(0o755)
            for outcome in ('success', 'failure', 'skipped', 'cancelled'):
                with self.subTest(outcome=outcome):
                    env = {**os.environ, 'PATH': directory + os.pathsep + os.environ['PATH'],
                           'TEST_GH_LOG': str(log), 'VERIFY_OUTCOME': outcome,
                           'GITHUB_REPOSITORY': self.repo, 'PR_HEAD_SHA': 'member-head',
                           'GITHUB_SERVER_URL': 'https://github.com', 'GITHUB_RUN_ID': '1',
                           'RUNNER_TEMP': directory,
                           'CURRENT_PRS': str(current)}
                    result = subprocess.run(['bash', '-e', '-c', step['run']], env=env)
                    self.assertEqual(result.returncode, 0 if outcome == 'success' else 1)
                    args = json.loads(log.read_text())
                    self.assertEqual(args[1], 'repos/linuxdeepin/go-lib/statuses/member-head')
                    self.assertIn('state=' + ('success' if outcome == 'success' else 'failure'), args)
                    self.assertIn('context=group-version-guard', args)

    def test_same_head_sha_rechecks_all_open_prs_and_blocks_wrong_target(self):
        wrong = {**copy.deepcopy(self.pr), 'number': 2, 'state': 'open',
                 'base': {'ref': 'review/forbidden', 'sha': 'member-base'}}
        original = self.ns['api']
        def api_with_wrong_pr(path, **kwargs):
            path_only = urlsplit(path).path
            if path_only == f'repos/{self.repo}/pulls/1':
                return self.pr
            if path_only == f'repos/{self.repo}/pulls':
                self.assertEqual(kwargs, {'paginate': True})
                return [wrong]
            return original(path)
        self.ns['api'] = api_with_wrong_pr
        with patch.dict(os.environ, GITHUB_REPOSITORY=self.repo, GITHUB_EVENT_PATH='event.json',
                        PR_HEAD_SHA='member-head'):
            with patch('builtins.open', mock_open(read_data=json.dumps({'number': 1}))):
                with self.assertRaisesRegex(RuntimeError, '不属于 G2 本轮'):
                    self.ns['main']()
        self.calls.append(f'repos/{self.repo}/pulls?state=open&per_page=100')

    def test_snapshot_written_by_validation_is_accepted_by_final_publication(self):
        step = WORKFLOW['jobs']['verify']['steps'][-1]
        original = self.ns['api']
        def api_with_pulls(path, **kwargs):
            path_only = urlsplit(path).path
            if path_only == f'repos/{self.repo}/pulls/1':
                return self.pr
            if path_only == f'repos/{self.repo}/pulls':
                self.assertEqual(kwargs, {'paginate': True})
                return [self.pr]
            return original(path)
        self.ns['api'] = api_with_pulls
        with tempfile.TemporaryDirectory() as directory:
            event = Path(directory) / 'event.json'
            event.write_text(json.dumps({'number': 1}))
            current = Path(directory) / 'current.json'
            current.write_text(json.dumps([self.pr]))
            with patch.dict(os.environ, GITHUB_REPOSITORY=self.repo, GITHUB_EVENT_PATH=str(event),
                            PR_HEAD_SHA='member-head', RUNNER_TEMP=directory):
                self.ns['main']()
            gh = Path(directory) / 'gh'
            log = Path(directory) / 'calls.json'
            gh.write_text('#!/usr/bin/env python3\nimport json, os, pathlib, sys\n'
                          'pathlib.Path(os.environ["TEST_GH_LOG"]).write_text(json.dumps(sys.argv[1:]))\n'
                          'if sys.argv[1] == "api":\n'
                          '    print(pathlib.Path(os.environ["CURRENT_PRS"]).read_text(), end="")\n')
            gh.chmod(0o755)
            env = {**os.environ, 'PATH': directory + os.pathsep + os.environ['PATH'],
                   'TEST_GH_LOG': str(log), 'VERIFY_OUTCOME': 'success',
                   'GITHUB_REPOSITORY': self.repo, 'PR_HEAD_SHA': 'member-head',
                   'GITHUB_SERVER_URL': 'https://github.com', 'GITHUB_RUN_ID': '1',
                   'RUNNER_TEMP': directory, 'CURRENT_PRS': str(current)}
            subprocess.run(['bash', '-e', '-c', step['run']], env=env, check=True)
            args = json.loads(log.read_text())
            self.assertIn('state=success', args)

    def test_final_refresh_failure_publishes_failure_and_fails_the_step(self):
        step = WORKFLOW['jobs']['verify']['steps'][-1]
        with tempfile.TemporaryDirectory() as directory:
            gh = Path(directory) / 'gh'
            log = Path(directory) / 'calls.jsonl'
            verified = Path(directory) / 'verified-prs.json'
            current = Path(directory) / 'current.json'
            identity = {'number': 1, 'head_sha': 'member-head', 'head_repo': self.repo,
                        'head_ref': 'automation/update-changelog/6.0.50',
                        'base_ref': 'master'}
            verified.write_text(json.dumps([identity]))
            current.write_text(json.dumps([{**self.pr, 'state': 'open'}]))
            gh.write_text('''#!/usr/bin/env python3
import json, os, pathlib, sys
args = sys.argv[1:]
with pathlib.Path(os.environ['TEST_GH_LOG']).open('a') as output:
    output.write(json.dumps(args) + '\\n')
if args[0] == 'api' and '/pulls?' in args[1]:
    if os.environ['FAIL_REFRESH'] == 'list':
        print(os.environ['PARTIAL_OUTPUT'], end='')
        print('gh: HTTP 503', file=sys.stderr)
        sys.exit(1)
    print(pathlib.Path(os.environ['CURRENT_PRS']).read_text(), end='')
''')
            gh.chmod(0o755)
            for phase, partial in [('list', ''), ('list', '[]\n')]:
                with self.subTest(phase=phase, partial=partial):
                    log.write_text('')
                    env = {**os.environ, 'PATH': directory + os.pathsep + os.environ['PATH'],
                           'TEST_GH_LOG': str(log), 'VERIFY_OUTCOME': 'success',
                           'GITHUB_REPOSITORY': self.repo, 'PR_HEAD_SHA': 'member-head',
                           'GITHUB_SERVER_URL': 'https://github.com', 'GITHUB_RUN_ID': '1',
                           'FAIL_REFRESH': phase, 'PARTIAL_OUTPUT': partial,
                           'RUNNER_TEMP': directory,
                           'CURRENT_PRS': str(current)}
                    result = subprocess.run(['bash', '-e', '-c', step['run']], env=env,
                                            capture_output=True, text=True)
                    self.assertNotEqual(result.returncode, 0)
                    calls = [json.loads(line) for line in log.read_text().splitlines()]
                    self.assertEqual(calls[-1][1], f'repos/{self.repo}/statuses/member-head')
                    self.assertIn('state=failure', calls[-1])
                    self.assertFalse(any('state=success' in call for call in calls))

    def test_shared_sha_status_is_failure_when_any_open_pr_is_closed(self):
        step = WORKFLOW['jobs']['verify']['steps'][-1]
        with tempfile.TemporaryDirectory() as directory:
            gh = Path(directory) / 'gh'
            log = Path(directory) / 'calls.json'
            verified = Path(directory) / 'verified-prs.json'
            verified.write_text(json.dumps([{
                'number': 1, 'head_sha': 'member-head', 'head_repo': self.repo,
                'head_ref': 'automation/update-changelog/6.0.50',
                'base_ref': 'master'}]))
            gh.write_text('#!/usr/bin/env python3\nimport json, os, pathlib, sys\n'
                          'pathlib.Path(os.environ["TEST_GH_LOG"]).write_text(json.dumps(sys.argv[1:]))\n'
                          'if sys.argv[1] == "api":\n'
                          '    print("[]")\n')
            gh.chmod(0o755)
            env = {**os.environ, 'PATH': directory + os.pathsep + os.environ['PATH'],
                   'TEST_GH_LOG': str(log), 'VERIFY_OUTCOME': 'success',
                   'GITHUB_REPOSITORY': self.repo, 'PR_HEAD_SHA': 'member-head',
                   'GITHUB_SERVER_URL': 'https://github.com', 'GITHUB_RUN_ID': '1',
                   'RUNNER_TEMP': directory}
            result = subprocess.run(['bash', '-e', '-c', step['run']], env=env)
            self.assertNotEqual(result.returncode, 0)
            args = json.loads(log.read_text())
            self.assertIn('state=failure', args)

    def test_shared_sha_status_accepts_open_pr_state_from_gh_cli(self):
        step = WORKFLOW['jobs']['verify']['steps'][-1]
        with tempfile.TemporaryDirectory() as directory:
            gh = Path(directory) / 'gh'
            log = Path(directory) / 'calls.json'
            verified = Path(directory) / 'verified-prs.json'
            current = Path(directory) / 'current.json'
            identity = {'number': 1, 'head_sha': 'member-head', 'head_repo': self.repo,
                        'head_ref': 'automation/update-changelog/6.0.50',
                        'base_ref': 'master'}
            verified.write_text(json.dumps([identity]))
            current.write_text(json.dumps([{**self.pr, 'state': 'open'}]))
            gh.write_text('#!/usr/bin/env python3\nimport json, os, pathlib, sys\n'
                          'pathlib.Path(os.environ["TEST_GH_LOG"]).write_text(json.dumps(sys.argv[1:]))\n'
                          'if sys.argv[1] == "api":\n'
                          '    print(pathlib.Path(os.environ["CURRENT_PRS"]).read_text(), end="")\n')
            gh.chmod(0o755)
            env = {**os.environ, 'PATH': directory + os.pathsep + os.environ['PATH'],
                   'TEST_GH_LOG': str(log), 'VERIFY_OUTCOME': 'success',
                   'GITHUB_REPOSITORY': self.repo, 'PR_HEAD_SHA': 'member-head',
                   'GITHUB_SERVER_URL': 'https://github.com', 'GITHUB_RUN_ID': '1',
                   'RUNNER_TEMP': directory,
                   'CURRENT_PRS': str(current)}
            subprocess.run(['bash', '-e', '-c', step['run']], env=env, check=True)
            args = json.loads(log.read_text())
            self.assertIn('state=success', args)

    def test_final_publication_rejects_pr_retargeted_after_validation(self):
        step = WORKFLOW['jobs']['verify']['steps'][-1]
        with tempfile.TemporaryDirectory() as directory:
            gh = Path(directory) / 'gh'
            log = Path(directory) / 'calls.json'
            verified = Path(directory) / 'verified-prs.json'
            current = Path(directory) / 'current.json'
            verified.write_text(json.dumps([{
                'number': 1, 'head_sha': 'member-head', 'head_repo': self.repo,
                'head_ref': 'automation/update-changelog/6.0.50',
                'base_ref': 'review/preview'}]))
            changed = copy.deepcopy(self.pr)
            changed['base'] = {'ref': 'master', 'sha': 'master-sha'}
            current.write_text(json.dumps([changed]))
            gh.write_text('#!/usr/bin/env python3\nimport json, os, pathlib, sys\n'
                          'pathlib.Path(os.environ["TEST_GH_LOG"]).write_text(json.dumps(sys.argv[1:]))\n'
                          'if sys.argv[1] == "api":\n'
                          '    print(pathlib.Path(os.environ["CURRENT_PRS"]).read_text(), end="")\n')
            gh.chmod(0o755)
            env = {**os.environ, 'PATH': directory + os.pathsep + os.environ['PATH'],
                   'TEST_GH_LOG': str(log), 'VERIFY_OUTCOME': 'success',
                   'GITHUB_REPOSITORY': self.repo, 'PR_HEAD_SHA': 'member-head',
                   'GITHUB_SERVER_URL': 'https://github.com', 'GITHUB_RUN_ID': '1',
                   'RUNNER_TEMP': directory,
                   'CURRENT_PRS': str(current)}
            result = subprocess.run(['bash', '-e', '-c', step['run']], env=env,
                                    capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('目标、提交或开放集合发生变化', result.stderr)
            args = json.loads(log.read_text())
            self.assertIn('state=failure', args)

    def test_closed_pr_recomputes_only_remaining_open_prs(self):
        closed = {**copy.deepcopy(self.pr), 'number': 2, 'state': 'closed'}
        valid = {**copy.deepcopy(self.pr), 'state': 'open'}
        original = self.ns['api']
        def api_with_closed_pr(path, **kwargs):
            path_only = urlsplit(path).path
            if path_only == f'repos/{self.repo}/pulls/2':
                return closed
            if path_only == f'repos/{self.repo}/pulls':
                self.assertEqual(kwargs, {'paginate': True})
                return [closed, valid]
            return original(path)
        self.ns['api'] = api_with_closed_pr
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, GITHUB_REPOSITORY=self.repo, GITHUB_EVENT_PATH='event.json',
                            PR_HEAD_SHA='member-head',
                            RUNNER_TEMP=directory):
                with patch('builtins.open', mock_open(read_data=json.dumps({'number': 2, 'action': 'closed'}))):
                    self.ns['main']()


if __name__ == '__main__':
    unittest.main()
