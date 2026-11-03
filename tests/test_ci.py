import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import urllib.error

spec = importlib.util.spec_from_file_location('ci', Path(__file__).resolve().parents[1] / 'scripts/ci.py')
ci = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ci)


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1])
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = self.root / 'build-config.json'
        self.config.write_text(json.dumps({
            'java': {'repository': 'example/java', 'branch': 'main', 'commit': 'a' * 40},
            'native': {'repository': 'example/native', 'branch': 'main', 'commit': 'b' * 40},
            'mdspan_commit': 'c' * 40}))
        self.output = self.root / 'output'
        self.env = {'GITHUB_SHA': 'd' * 40, 'GITHUB_OUTPUT': str(self.output),
                    'GITHUB_REPOSITORY': 'example/builds', 'GH_TOKEN': 'test-only',
                    'GITHUB_EVENT_NAME': 'workflow_dispatch', 'PUBLISH': 'false'}

    def resolve(self, env=None, response=None):
        with patch.object(ci, 'CONFIG', self.config), patch.dict(os.environ, self.env | (env or {}), clear=True):
            with patch.object(ci.urllib.request, 'urlopen', side_effect=response) as network:
                ci.resolve()
        return dict(line.split('=', 1) for line in self.output.read_text().splitlines()), network

    def test_manual_preview_uses_pins_and_never_publishes(self):
        values, network = self.resolve()
        self.assertEqual(values['java'], 'a' * 40)
        self.assertEqual(values['publish'], 'false')
        self.assertEqual(values['build'], 'true')
        network.assert_not_called()

    def test_pull_request_cannot_request_publication(self):
        values, network = self.resolve({'GITHUB_EVENT_NAME': 'pull_request', 'PUBLISH': 'true'})
        self.assertEqual(values['publish'], 'false')
        network.assert_not_called()

    def test_published_release_is_skipped(self):
        from io import BytesIO
        values, _ = self.resolve({'PUBLISH': 'true'}, response=lambda *a, **k: BytesIO(b'{"draft":false}'))
        self.assertEqual(values['build'], 'false')

    def test_only_404_is_treated_as_missing_release(self):
        error = urllib.error.HTTPError('https://example.invalid', 403, 'Forbidden', {}, None)
        with self.assertRaises(urllib.error.HTTPError):
            self.resolve({'PUBLISH': 'true'}, response=error)

    def test_interrupted_draft_release_is_rebuilt_for_retry(self):
        from io import BytesIO
        values, _ = self.resolve({'PUBLISH': 'true'}, response=lambda *a, **k: BytesIO(b'{"draft":true}'))
        self.assertEqual(values['build'], 'true')

    def test_version_tag_publishes_exact_tag_after_build(self):
        missing = urllib.error.HTTPError('https://example.invalid', 404, 'Missing', {}, None)
        values, _ = self.resolve({'GITHUB_EVENT_NAME': 'push', 'GITHUB_REF': 'refs/tags/v1.0'}, response=missing)
        self.assertEqual(values['tag'], 'v1.0')
        self.assertEqual(values['publish'], 'true')
        self.assertEqual(values['build'], 'true')

    def test_daily_check_resolves_both_upstreams(self):
        missing = urllib.error.HTTPError('https://example.invalid', 404, 'Missing', {}, None)
        with patch.object(ci, 'run', side_effect=['e' * 40 + '\trefs/heads/main', 'f' * 40 + '\trefs/heads/main']) as git:
            values, _ = self.resolve({'GITHUB_EVENT_NAME': 'schedule'}, response=missing)
        self.assertEqual(git.call_count, 2)
        self.assertEqual(values['java'], 'e' * 40)
        self.assertEqual(values['native'], 'f' * 40)

    def test_non_commit_revision_is_rejected(self):
        with self.assertRaises(ValueError):
            ci.validate_sha('main')


class ArtifactTests(unittest.TestCase):
    def test_mixed_native_abis_and_source_pairs_are_rejected(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]) as directory:
            root = Path(directory)
            for target, name in ci.NATIVE_FILES.items():
                folder = root / f'native-{target}'
                folder.mkdir()
                library = folder / name
                library.write_bytes(target.encode())
                (folder / 'metadata.json').write_text(json.dumps({
                    'target': target, 'file': name, 'sha256': ci.digest(library),
                    'java_commit': 'a' * 40, 'native_commit': 'b' * 40, 'abi': '0.1.0-unstable'}))
            with patch.dict(os.environ, {'EXPECTED_JAVA_SHA': 'a' * 40, 'EXPECTED_NATIVE_SHA': 'b' * 40}):
                self.assertEqual(len(ci.platform_metadata(root)), 3)
                path = root / 'native-macos-arm64/metadata.json'
                meta = json.loads(path.read_text())
                meta['abi'] = '0.2.0'
                path.write_text(json.dumps(meta))
                with self.assertRaisesRegex(RuntimeError, 'different ABIs'):
                    ci.platform_metadata(root)
                meta['abi'] = '0.1.0-unstable'
                meta['native_commit'] = 'f' * 40
                path.write_text(json.dumps(meta))
                with self.assertRaisesRegex(RuntimeError, 'Mixed native'):
                    ci.platform_metadata(root)


if __name__ == '__main__':
    unittest.main()
