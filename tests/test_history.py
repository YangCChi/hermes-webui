import importlib.util
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from fastapi.testclient import TestClient

APP_PATH = Path('/opt/hermes-webui-git/app.py')


class FakeResponse:
    status_code = 200
    text = 'ok'

    def json(self):
        return {'choices': [{'message': {'content': 'pong'}}]}


class FakeAsyncClient:
    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, *args, **kwargs):
        return FakeResponse()

    async def get(self, *args, **kwargs):
        return FakeResponse()


def load_app_module():
    spec = importlib.util.spec_from_file_location('hermes_webui_app_under_test', APP_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class HistoryPersistenceTests(unittest.TestCase):
    def test_chat_messages_are_persisted_and_returned_by_history_api(self):
        module = load_app_module()
        module.httpx.AsyncClient = FakeAsyncClient
        module.SETTINGS['WEBUI_AUTH_ENABLED'] = 'false'

        with TemporaryDirectory() as tmpdir:
            module.HISTORY_FILE = Path(tmpdir) / 'history.json'
            client = TestClient(module.app)

            self.assertEqual(client.get('/api/history').json(), {'messages': []})

            response = client.post('/api/chat', json={'messages': [{'role': 'user', 'content': '只回复 pong'}]})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()['content'], 'pong')

            history = client.get('/api/history').json()['messages']
            self.assertEqual(history, [
                {'role': 'user', 'content': '只回复 pong'},
                {'role': 'assistant', 'content': 'pong'},
            ])

            disk_history = json.loads(module.HISTORY_FILE.read_text())
            self.assertEqual(disk_history, history)


if __name__ == '__main__':
    unittest.main()
