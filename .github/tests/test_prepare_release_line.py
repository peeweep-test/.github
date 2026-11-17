import contextlib
import copy
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

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
                    self.github.repo_changes[repo] = {"permissions": {}}
                else:
                    self.github.protections[repo, "master"] = None if failure == "protected404" else []
                with self.assertRaisesRegex(RuntimeError, "全部成员预检未通过"):
                    self.run_prepare()
                self.assertEqual(self.github.writes, [])
                if failure == "commit":
                    self.github.commits[repo, self.github.bases[member]] = deleted
                elif failure == "permission":
                    self.github.repo_changes.clear()
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

    def test_protected_template_install_uses_pr_and_waits_for_merge(self):
        member, repo = self.github.members[0]
        base = self.github.bases[member]
        files = {"debian/changelog": self.github.files[repo, base]["debian/changelog"]}
        old = self.github.add_commit(repo, files, [base])
        self.github.target(repo, sha=old)
        with self.assertRaisesRegex(RuntimeError, "尚未全部 READY"):
            self.run_prepare()
        templates = [pr for pr in self.github.prs if pr["repo"] == repo]
        self.assertEqual(len(templates), 1)
        self.assertEqual(templates[0]["base"]["ref"], self.github.branch)
        self.assertIn(templates[0]["html_url"], self.output.getvalue())
        self.assertEqual(self.github.refs[repo, self.github.branch], old)
        self.github.writes.clear()
        with self.assertRaises(RuntimeError):
            self.run_prepare()
        self.assertEqual(len(self.github.prs), 1)
        self.assertEqual(self.github.refs[repo, self.github.branch], old)
        self.assertEqual(self.github.writes, [])
        self.assertFalse(any(path.endswith("/git/refs/heads/" + self.github.branch)
                             for path, _, _ in self.github.writes))
        self.github.merge(templates[0])
        self.assertTrue(all(row["state"] == "READY" for row in self.run_prepare()))

    def test_missing_default_and_target_templates_both_require_merged_prs(self):
        member, repo = self.github.members[0]
        base = self.github.bases[member]
        self.github.files[repo, base].pop(".github/workflows/call-update-changelog.yml")
        with self.assertRaises(RuntimeError):
            self.run_prepare()
        prs = [pr for pr in self.github.prs if pr["repo"] == repo]
        self.assertEqual({pr["base"]["ref"] for pr in prs}, {"master", self.github.branch})
        for pr in prs:
            self.github.merge(pr)
        self.assertTrue(all(row["state"] == "READY" for row in self.run_prepare()))

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

    def test_pr_creation_uncertainty_requires_exact_head_identity(self):
        for bad in ("repo", "ref", "sha", None):
            github = GitHub()
            member, repo = github.members[0]
            files = {"debian/changelog": github.changelog("1:6.1.113")}
            github.target(repo, sha=github.add_commit(repo, files, [github.bases[member]]))
            original = github.api
            saved = None
            def api(url, data=None, **kwargs):
                nonlocal saved
                if url == f"repos/{repo}/pulls" and data:
                    saved = github.add_pr(repo, data["head"], data["base"])
                    if bad == "repo":
                        saved["head"]["repo"]["full_name"] = "contributor/go-lib"
                    elif bad == "ref":
                        saved["head"]["ref"] = "different-tool-branch"
                    elif bad == "sha":
                        saved["head"]["sha"] = "f" * 40
                    raise RuntimeError("PR response lost")
                if url.startswith(f"repos/{repo}/pulls?") and saved:
                    return [copy.deepcopy(saved)]
                return original(url, data=data, **kwargs)
            with self.subTest(bad=bad):
                if bad:
                    with self.assertRaises(RuntimeError):
                        prepare.install_templates(api, repo, github.branch, github.line, github.templates)
                else:
                    link = prepare.install_templates(api, repo, github.branch, github.line, github.templates)
                    self.assertEqual(link, saved["html_url"])
                self.assertEqual(len(github.prs), 1)

    def test_workflow_trust_permissions_and_no_extra_workflow(self):
        workflow = yaml.safe_load((ROOT / ".github/workflows/prepare-release-line.yml").read_text())
        self.assertEqual(set(workflow[True]), {"workflow_dispatch"})
        self.assertIs(workflow[True]["workflow_dispatch"]["inputs"]["dry_run"]["default"], True)
        steps = workflow["jobs"]["prepare"]["steps"]
        tokens = [step for step in steps if step.get("uses", "").startswith("actions/create-github-app-token@")]
        self.assertTrue(all(step["with"]["permission-workflows"] == "write" for step in tokens))
        self.assertEqual(tokens[0]["with"]["permission-administration"], "read")
        self.assertTrue(tokens[1]["continue-on-error"])
        checkout = next(step for step in steps if step.get("with", {}).get("path") == "coordinator")
        self.assertNotIn("ref", checkout["with"])
        self.assertIs(checkout["with"]["persist-credentials"], False)
        self.assertFalse(any("action-sync" in step.get("uses", "") for step in steps))


if __name__ == "__main__":
    unittest.main()
