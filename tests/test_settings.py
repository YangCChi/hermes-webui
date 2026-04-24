import importlib.util
from pathlib import Path
import unittest

from fastapi.testclient import TestClient

APP_PATH = Path('/opt/hermes-webui-git/app.py')


def load_app_module():
    spec = importlib.util.spec_from_file_location('hermes_webui_app_settings_test', APP_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class SettingsPageTests(unittest.TestCase):
    def test_settings_page_shows_configuration_summary_and_changelog(self):
        module = load_app_module()
        module.SETTINGS['WEBUI_AUTH_ENABLED'] = 'false'
        client = TestClient(module.app)

        response = client.get('/settings')

        self.assertEqual(response.status_code, 200)
        html = response.text
        self.assertIn('设置', html)
        self.assertIn('基础信息', html)
        self.assertIn('WebUI 版本', html)
        self.assertIn(module.APP_VERSION, html)
        self.assertIn('Hermes API', html)
        self.assertIn('版本更新日志', html)
        self.assertIn('v0.0.5', html)
        self.assertIn('新增设置页', html)
        self.assertIn('/api/settings', html)

    def test_sidebar_links_to_settings(self):
        source = APP_PATH.read_text()

        self.assertIn("href='/settings'", source)
        self.assertIn('设置</a>', source)

    def test_settings_api_returns_safe_structured_configuration(self):
        module = load_app_module()
        module.SETTINGS['WEBUI_AUTH_ENABLED'] = 'false'
        module.SETTINGS['HERMES_API_KEY'] = 'sk-test-secret-value'
        client = TestClient(module.app)

        response = client.get('/api/settings')

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data['version'], 'v0.0.5')
        self.assertEqual(data['api_base'], module.SETTINGS['HERMES_API_BASE'])
        self.assertEqual(data['model'], module.SETTINGS['HERMES_MODEL'])
        self.assertNotEqual(data['api_key'], 'sk-test-secret-value')
        self.assertIn('...', data['api_key'])
        self.assertIn('changelog', data)
        self.assertEqual(data['changelog'][0]['version'], 'v0.0.5')


if __name__ == '__main__':
    unittest.main()
