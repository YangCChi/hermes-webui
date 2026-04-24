import importlib.util
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from fastapi.testclient import TestClient

APP_PATH = Path('/opt/hermes-webui-git/app.py')


def load_app_module():
    spec = importlib.util.spec_from_file_location('hermes_webui_app_activity_test', APP_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class ActivityMonitorTests(unittest.TestCase):
    def test_activity_page_and_sidebar_link_are_rendered(self):
        module = load_app_module()
        module.SETTINGS['WEBUI_AUTH_ENABLED'] = 'false'
        client = TestClient(module.app)

        home = client.get('/')
        page = client.get('/activity')

        self.assertEqual(home.status_code, 200)
        self.assertEqual(page.status_code, 200)
        self.assertIn("href='/activity'", home.text)
        self.assertIn('过程查看', home.text)
        self.assertIn('Hermes 过程查看', page.text)
        self.assertIn("fetch('/api/activity')", page.text)
        self.assertIn('activity-list', page.text)
        self.assertIn('setInterval(loadActivity', page.text)

    def test_activity_page_contains_visual_timeline_dashboard(self):
        module = load_app_module()
        module.SETTINGS['WEBUI_AUTH_ENABLED'] = 'false'
        client = TestClient(module.app)

        page = client.get('/activity')

        self.assertEqual(page.status_code, 200)
        html = page.text
        for marker in [
            'activity-dashboard',
            'activity-commandbar',
            'activityFilters',
            'activityTimeline',
            'activityMetricTotal',
            'activityMetricTools',
            'activityRefreshToggle',
            'renderActivityTimeline',
            'summarizeActivity',
            'copyActivityContent',
            '过程可视化改进方案',
        ]:
            self.assertIn(marker, html)

    def test_activity_api_returns_recent_sessions_and_messages(self):
        module = load_app_module()
        module.SETTINGS['WEBUI_AUTH_ENABLED'] = 'false'
        client = TestClient(module.app)

        with TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / 'state.db'
            module.ACTIVITY_DB_FILE = db_path
            module.init_activity_db(db_path)
            module.record_activity_message('user', '帮我改一下 WebUI')
            module.record_activity_message('assistant', '我先检查代码结构')
            module.record_activity_message('tool', 'git status --short', tool_name='terminal')

            response = client.get('/api/activity')

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn('current_session_id', data)
        self.assertEqual(len(data['messages']), 3)
        self.assertEqual(data['messages'][0]['role'], 'user')
        self.assertEqual(data['messages'][1]['role'], 'assistant')
        self.assertEqual(data['messages'][2]['tool_name'], 'terminal')
        self.assertEqual(data['messages'][2]['content'], 'git status --short')
        self.assertIn('sessions', data)
        self.assertGreaterEqual(data['sessions'][0]['message_count'], 3)

    def test_activity_api_can_read_hermes_state_database_schema(self):
        module = load_app_module()
        module.SETTINGS['WEBUI_AUTH_ENABLED'] = 'false'
        client = TestClient(module.app)

        with TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / 'state.db'
            module.ACTIVITY_DB_FILE = db_path
            module.init_activity_db(db_path)
            module.record_activity_message('user', '当前在做什么？')

            response = client.get('/api/activity?limit=1')

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(len(data['messages']), 1)
        self.assertEqual(data['messages'][0]['content'], '当前在做什么？')
        self.assertIn('timestamp', data['messages'][0])


if __name__ == '__main__':
    unittest.main()
