import importlib.util
from pathlib import Path
import unittest

from fastapi.testclient import TestClient

APP_PATH = Path('/opt/hermes-webui-git/app.py')


def load_app_module():
    spec = importlib.util.spec_from_file_location('hermes_webui_app_changelog_test', APP_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class ChangelogPageTests(unittest.TestCase):
    def test_changelog_page_shows_version_time_and_update_items(self):
        module = load_app_module()
        module.SETTINGS['WEBUI_AUTH_ENABLED'] = 'false'
        client = TestClient(module.app)

        response = client.get('/changelog')

        self.assertEqual(response.status_code, 200)
        html = response.text
        self.assertIn('版本更新日志', html)
        self.assertIn('v0.0.3', html)
        self.assertIn('v0.0.2', html)
        self.assertIn('v0.0.1', html)
        self.assertIn('更新时间', html)
        self.assertIn('更新内容', html)
        self.assertIn('从 0.0.1 开始记录', html)
        self.assertIn('/api/changelog', html)

    def test_changelog_api_returns_structured_versions(self):
        module = load_app_module()
        module.SETTINGS['WEBUI_AUTH_ENABLED'] = 'false'
        client = TestClient(module.app)

        response = client.get('/api/changelog')

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn('versions', data)
        self.assertGreaterEqual(len(data['versions']), 1)
        latest = data['versions'][0]
        self.assertEqual(latest['version'], 'v0.0.5')
        self.assertIn('updated_at', latest)
        self.assertIn('changes', latest)
        self.assertTrue(any('新增设置页' in item for item in latest['changes']))
        self.assertTrue(any(version['version'] == 'v0.0.4' for version in data['versions']))
        self.assertTrue(any(version['version'] == 'v0.0.3' for version in data['versions']))
        self.assertTrue(any(version['version'] == 'v0.0.1' for version in data['versions']))


if __name__ == '__main__':
    unittest.main()
