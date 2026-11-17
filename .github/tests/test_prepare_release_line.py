import contextlib
import copy
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from urllib.parse import parse_qs, urlsplit

import yaml


ROOT = Path(__file__).resolve().parents[2]
DDE = Path(os.environ.get("COORDINATOR_DIR", str(ROOT.parent / "dde"))).resolve()
sys.path.insert(0, str(DDE / ".github/scripts"))
sys.path.insert(0, str(DDE / ".github/tests"))
sys.path.insert(0, str(ROOT / ".github/scripts"))
os.environ.setdefault("COORDINATOR_DIR", str(DDE))
from test_release_lines import GitHub
import prepare_release_line as prepare


class PrepareTests(unittest.TestCase):
    def setUp(self):
        self.github = GitHub()
        self.installed = [repo for _, repo in self.github.members]
        self.sync_runs = []
        self.sync_dispatches = 0
        self.original_api = self.github.api
        self.github.api = self.api
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.templates = Path(self.temp.name)
        for filename, text in self.github.templates.items():
            path = self.templates / "workflow-templates" / filename
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        for _, repo in self.github.members:
            path = self.templates / "repos" / (repo + ".json")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps([
                {"src": f"workflow-templates/{filename}", "dest": f"{repo}/.github/workflows/{filename}",
                 "branches": ["master", "develop/.+"]} for filename in self.github.templates]))
        self.config_sha = self.github.with_line()
        self.output = io.StringIO()

    def api(self, url, data=None, **kwargs):
        parsed = urlsplit(url)
        if parsed.path == "installation/repositories":
            self.github.calls.append((url, data, kwargs.get("method")))
            page = int(parse_qs(parsed.query)["page"][0])
            return {"total_count": len(self.installed), "repositories": [
                {"full_name": repo} for repo in self.installed[(page - 1) * 100:page * 100]]}
        prefix = f"repos/{self.github.shared_repo}/actions/workflows/templates-sync.yml"
        if parsed.path == prefix + "/runs":
            self.github.calls.append((url, data, kwargs.get("method")))
            return {"workflow_runs": copy.deepcopy(self.sync_runs)}
        if parsed.path == prefix + "/dispatches":
            self.github.calls.append((url, data, kwargs.get("method")))
            self.github.writes.append((parsed.path, copy.deepcopy(data), kwargs.get("method")))
            self.sync_dispatches += 1
            for _, repo in self.github.members:
                for branch in ("master", self.github.branch):
                    sha = self.github.refs.get((repo, branch))
                    if sha is None:
                        continue
                    files = self.github.files[repo, sha]
                    files.update({f".github/workflows/{name}": text
                                  for name, text in self.github.templates.items()})
                    self.github.add_commit(repo, files, [sha], branch=branch, message="template sync")
            self.sync_runs.insert(0, {
                "id": 900 + self.sync_dispatches, "status": "completed", "conclusion": "success",
                "html_url": f"https://github.com/{self.github.shared_repo}/actions/runs/900"})
            return None
        return self.original_api(url, data=data, **kwargs)

    def run_prepare(self, dry_run=False, admin_write=True, api=None):
        with contextlib.redirect_stdout(self.output):
            return prepare.prepare(api or self.github.api, self.github.coordinator,
                                   self.config_sha, self.templates, self.github.line,
                                   dry_run=dry_run, can_create=True, app_id=self.github.app_id,
                                   admin_write=admin_write)

    def test_dry_run_reads_all_17_members_and_creates_nothing(self):
        rows = self.run_prepare(dry_run=True)
        self.assertEqual(len(rows), 17)
        self.assertTrue(all(row["operation"] == "create" and row["state"] == "BRANCH_MISSING" for row in rows))
        self.assertEqual(self.github.writes, [])

    def test_app_token_without_user_permissions_preflights_all_members(self):
        for _, repo in self.github.members:
            self.github.target(repo)
        def app_api(url, data=None, **kwargs):
            value = self.github.api(url, data=data, **kwargs)
            if url.startswith("repos/") and url.count("/") == 2:
                value.pop("permissions", None)
            return value
        rows = self.run_prepare(dry_run=True, api=app_api)
        self.assertEqual(len(rows), 17)
        self.assertTrue(all(row["state"] == "READY" for row in rows))
        self.assertEqual(self.github.writes, [])

    def test_installation_scope_is_paginated_before_any_writes(self):
        self.installed = [f"test/other-{i}" for i in range(100)] + self.installed
        rows = self.run_prepare(dry_run=True)
        self.assertEqual(len(rows), 17)
        self.assertIn(("installation/repositories?per_page=100&page=2", None, None),
                      self.github.calls)
        self.assertEqual(self.github.writes, [])

    def test_malformed_installation_scope_stops_all_writes(self):
        def api(url, data=None, **kwargs):
            if url.startswith("installation/repositories?"):
                return {"total_count": 1, "repositories": [{"full_name": None}]}
            return self.github.api(url, data=data, **kwargs)
        with self.assertRaisesRegex(RuntimeError, "App.*仓库范围"):
            self.run_prepare(api=api)
        self.assertEqual(self.github.writes, [])

    def test_creates_fixed_bases_only_after_full_preflight_then_verifies_ready(self):
        rows = self.run_prepare()
        self.assertTrue(all(row["state"] == "READY" for row in rows))
        for member, repo in self.github.members:
            self.assertEqual(self.github.refs[repo, self.github.branch], self.github.bases[member])
        first_write = next(i for i, (path, data, method) in enumerate(self.github.calls)
                           if path != "graphql" and (data is not None or method))
        before = self.github.calls[:first_write]
        self.assertIn((f"repos/{self.github.members[-1][1]}/git/commits/"
                       f"{self.github.bases[self.github.members[-1][0]]}", None, None), before)
        protections = [data for path, data, _ in self.github.writes if path.endswith("/protection")]
        self.assertEqual(len(protections), 17)
        self.assertEqual(protections[0]["required_pull_request_reviews"]["required_approving_review_count"], 2)
        self.assertTrue(protections[0]["required_pull_request_reviews"]["require_code_owner_reviews"])
        self.assertTrue(protections[0]["required_pull_request_reviews"]["require_last_push_approval"])
        self.assertIn({"context": "unit-test", "app_id": 111}, protections[0]["required_status_checks"]["checks"])
        self.assertIn({"context": "group-version-guard", "app_id": self.github.actions_id},
                      protections[0]["required_status_checks"]["checks"])
        self.assertEqual(protections[0]["restrictions"], {"users": [], "teams": ["desktop"], "apps": []})
        self.assertFalse(any(data and data.get("force") or method == "DELETE"
                             for _, data, method in self.github.writes))

    def test_invalid_last_member_sha_or_permission_stops_all_writes(self):
        member, repo = self.github.members[-1]
        for failure in ("commit", "permission", "protected404", "malformed"):
            with self.subTest(failure=failure):
                if failure == "commit":
                    deleted = self.github.commits.pop((repo, self.github.bases[member]))
                elif failure == "permission":
                    self.installed.remove(repo)
                else:
                    self.github.protections[repo, "master"] = None if failure == "protected404" else []
                with self.assertRaisesRegex(RuntimeError, "全部成员预检未通过"):
                    self.run_prepare()
                self.assertEqual(self.github.writes, [])
                if failure == "commit":
                    self.github.commits[repo, self.github.bases[member]] = deleted
                elif failure == "permission":
                    self.installed.append(repo)
                else:
                    self.github.protections[repo, "master"] = self.github.policy()

    def test_historical_sha_need_not_be_a_master_ancestor(self):
        member, repo = self.github.members[0]
        historic = self.github.add_commit(repo, self.github.files[repo, self.github.bases[member]])
        self.github.bases[member] = historic
        self.config_sha = self.github.with_line()
        self.assertNotIn(historic, self.github.ancestors(repo, self.github.refs[repo, "master"]))
        rows = self.run_prepare()
        self.assertTrue(all(row["state"] == "READY" for row in rows))
        self.assertEqual(self.github.refs[repo, self.github.branch], historic)

    def test_existing_must_contain_base_and_is_never_reset(self):
        member, repo = self.github.members[0]
        base = self.github.bases[member]
        newer = self.github.add_commit(repo, self.github.files[repo, base], [base])
        self.github.target(repo, sha=newer)
        self.run_prepare()
        self.assertEqual(self.github.refs[repo, self.github.branch], newer)
        self.github.writes.clear()
        unrelated = self.github.add_commit(repo, self.github.files[repo, base])
        self.github.set_ref(repo, self.github.branch, unrelated)
        with self.assertRaisesRegex(RuntimeError, "全部成员预检未通过"):
            self.run_prepare()
        self.assertEqual(self.github.writes, [])

    def test_partial_branch_failure_rerun_only_creates_missing_members(self):
        repo = self.github.members[1][1]
        self.github.fail_once[f"repos/{repo}/git/refs"] = RuntimeError("temporary API error")
        with self.assertRaisesRegex(RuntimeError, "分支创建中断"):
            self.run_prepare()
        first = self.github.members[0][1]
        fixed = self.github.refs[first, self.github.branch]
        self.github.writes.clear()
        self.run_prepare()
        self.assertEqual(self.github.refs[first, self.github.branch], fixed)
        creates = [data for _, data, _ in self.github.writes if data and "ref" in data]
        self.assertEqual(len(creates), 16)

    def test_concurrent_branch_creation_rechecks_ancestry(self):
        member, repo = self.github.members[0]
        def race():
            self.github.set_ref(repo, self.github.branch, self.github.bases[member])
            raise RuntimeError("reference already exists (HTTP 422)")
        self.github.fail_once[f"repos/{repo}/git/refs"] = race
        self.assertTrue(all(row["state"] == "READY" for row in self.run_prepare()))

    def test_template_sync_updates_existing_branch_without_pr(self):
        member, repo = self.github.members[0]
        base = self.github.bases[member]
        files = {"debian/changelog": self.github.files[repo, base]["debian/changelog"]}
        old = self.github.add_commit(repo, files, [base])
        self.github.target(repo, sha=old)
        rows = self.run_prepare()
        self.assertTrue(all(row["state"] == "READY" for row in rows))
        self.assertEqual(self.sync_dispatches, 1)
        self.assertEqual(self.github.prs, [])
        self.assertNotEqual(self.github.refs[repo, self.github.branch], old)
        synced = self.github.files[repo, self.github.refs[repo, self.github.branch]]
        for name, text in self.github.templates.items():
            self.assertEqual(synced[f".github/workflows/{name}"], text)
        self.github.writes.clear()
        rows = self.run_prepare()
        self.assertTrue(all(row["state"] == "READY" for row in rows))
        self.assertEqual(self.sync_dispatches, 1)
        self.assertEqual(self.github.writes, [])

    def test_missing_default_and_target_templates_use_one_sync(self):
        member, repo = self.github.members[0]
        base = self.github.bases[member]
        self.github.files[repo, base].pop(".github/workflows/call-update-changelog.yml")
        rows = self.run_prepare()
        self.assertTrue(all(row["state"] == "READY" for row in rows))
        self.assertEqual(self.sync_dispatches, 1)
        self.assertEqual(self.github.prs, [])
        for branch in ("master", self.github.branch):
            sha = self.github.refs[repo, branch]
            for name, text in self.github.templates.items():
                self.assertEqual(self.github.files[repo, sha][f".github/workflows/{name}"], text)

    def test_unknown_rule_creation_permission_stops_all_writes(self):
        repo = self.github.members[-1][1]
        self.github.rules[repo, self.github.branch] = [{"type": "creation", "ruleset_id": 1}]
        self.github.rulesets[repo, 1] = {"enforcement": "active"}
        with self.assertRaisesRegex(RuntimeError, "全部成员预检未通过"):
            self.run_prepare()
        self.assertEqual(self.github.writes, [])

    def test_classic_branch_creation_allowance_is_checked(self):
        repo = self.github.members[-1][1]
        self.github.creation_rules[repo] = [{"pattern": "develop/**", "blocksCreations": True,
                                           "restrictsPushes": True,
                                           "pushAllowances": {"totalCount": 0, "nodes": []}}]
        with self.assertRaises(RuntimeError):
            self.run_prepare()
        self.assertEqual(self.github.writes, [])
        self.github.creation_rules[repo][0]["pushAllowances"] = {
            "totalCount": 1, "nodes": [{"actor": {"__typename": "App", "databaseId": self.github.app_id}}]}
        self.assertTrue(all(row["state"] == "READY" for row in self.run_prepare()))

    def test_admin_read_without_write_allows_manual_protection_then_rerun(self):
        with self.assertRaisesRegex(RuntimeError, "尚未全部 READY"):
            self.run_prepare(admin_write=False)
        self.assertIn("Admin Write", self.output.getvalue())
        self.assertFalse(any(path.endswith("/protection") for path, _, _ in self.github.writes))
        for _, repo in self.github.members:
            self.github.protections[repo, self.github.branch] = self.github.policy()
        self.github.writes.clear()
        self.assertTrue(all(row["state"] == "READY" for row in self.run_prepare(admin_write=False)))
        self.assertEqual(self.github.writes, [])

    def test_protection_readback_cannot_drop_an_existing_check(self):
        self.github.drop_check = "unit-test"
        with self.assertRaises(RuntimeError):
            self.run_prepare()
        self.assertIn("读回丢失", self.output.getvalue())
        self.assertNotIn("| READY |", self.output.getvalue())

    def test_disabled_workflow_is_enabled_and_read_back(self):
        repo = self.github.members[0][1]
        self.github.workflows[repo, "call-update-changelog.yml"] = "disabled_manually"
        self.run_prepare()
        self.assertIn((f"repos/{repo}/actions/workflows/call-update-changelog.yml/enable", None, "PUT"),
                      self.github.writes)
        self.assertEqual(self.github.workflows[repo, "call-update-changelog.yml"], "active")

    def test_workflow_trust_permissions_and_no_extra_workflow(self):
        workflow = yaml.safe_load((ROOT / ".github/workflows/prepare-release-line.yml").read_text())
        self.assertEqual(set(workflow[True]), {"workflow_dispatch"})
        self.assertIs(workflow[True]["workflow_dispatch"]["inputs"]["dry_run"]["default"], True)
        steps = workflow["jobs"]["prepare"]["steps"]
        tokens = [step for step in steps if step.get("uses", "").startswith("actions/create-github-app-token@")]
        self.assertTrue(all(step["with"]["permission-contents"] == "write" for step in tokens))
        self.assertTrue(all(step["with"]["permission-actions"] == "write" for step in tokens))
        self.assertTrue(all(step["with"]["permission-pull-requests"] == "read" for step in tokens))
        self.assertFalse(any("permission-workflows" in step["with"] for step in tokens))
        self.assertEqual(tokens[0]["with"]["permission-administration"], "read")
        self.assertTrue(tokens[1]["continue-on-error"])
        checkout = next(step for step in steps if step.get("with", {}).get("path") == "coordinator")
        self.assertNotIn("ref", checkout["with"])
        self.assertIs(checkout["with"]["persist-credentials"], False)
        self.assertFalse(any("action-sync" in step.get("uses", "") for step in steps))


if __name__ == "__main__":
    unittest.main()
