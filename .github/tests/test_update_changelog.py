import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = yaml.safe_load((ROOT / '.github/workflows/update-changelog.yml').read_text())
STEPS = {s['name']: s for s in WORKFLOW['jobs']['update_changelog']['steps']}


class UpdateChangelogTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('dch'), 'devscripts is required')
    def test_epoch_changelog_keeps_package_name_and_linglong_has_no_epoch(self):
        for epoch in ('', '1:', '2:'):
            with self.subTest(epoch=epoch), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / 'debian').mkdir()
                (root / 'debian/changelog').write_text(
                    f'dde-clipboard ({epoch}6.1.37) unstable; urgency=medium\n\n'
                    '  * Previous release\n\n'
                    ' -- Group Release Test <group-release@example.invalid>  Thu, 05 Nov 2026 10:00:00 +0000\n')
                (root / 'linglong.yaml').write_text('package:\n  version: 6.1.37.0\n')
                (root / 'linglong.aarch64.yaml').write_text('package:\n  version: 6.1.37\n')
                target = epoch + '6.1.114'
                env = {**os.environ, 'VERSION': target, 'CURRENT_VERSION': epoch + '6.1.37',
                       'DISTRIBUTION': 'unstable', 'TITLES_JSON': json.dumps(['Group release']),
                       'DEBFULLNAME': 'Group Release Test', 'DEBEMAIL': 'group-release@example.invalid',
                       'TZ': 'Asia/Shanghai', 'GITHUB_WORKSPACE': directory,
                       'GITHUB_OUTPUT': str(root / 'output')}
                for name in ('Generate changelog', 'Sync linglong.yaml version'):
                    subprocess.run([sys.executable, '-c', STEPS[name]['run']], cwd=directory,
                                   env=env, check=True, capture_output=True, text=True)
                for field, expected in (('Source', 'dde-clipboard'), ('Version', target)):
                    actual = subprocess.check_output(
                        ['dpkg-parsechangelog', '-l', str(root / 'debian/changelog'), '-S', field], text=True)
                    self.assertEqual(actual.strip(), expected)
                self.assertEqual(yaml.safe_load((root / 'linglong.yaml').read_text())['package']['version'], '6.1.114' + '.0')
                self.assertEqual(yaml.safe_load((root / 'linglong.aarch64.yaml').read_text())['package']['version'], '6.1.114')
                output = (root / 'output').read_text()
                self.assertIn('debian/changelog\n', output)
                self.assertIn('linglong.yaml\n', output)
                self.assertIn('linglong.aarch64.yaml\n', output)


if __name__ == '__main__':
    unittest.main()
