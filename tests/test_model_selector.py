import importlib.util
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from fastapi.testclient import TestClient

APP_PATH = Path('/opt/hermes-webui-git/app.py')


class ModelsResponse:
    status_code = 200
    text = 'ok'

    def json(self):
        return {
            'object': 'list',
            'data': [
                {'id': 'hermes-agent'},
                {'id': 'gpt-4.1'},
                {'id': 'claude-sonnet-4'},
            ],
        }


class ChatResponse:
    status_code = 200
    text = 'ok'

    def json(self):
        return {'choices': [{'message': {'content': 'ok'}}]}


class RecordingAsyncClient:
    last_json = None
    last_get_url = None

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def get(self, url, *args, **kwargs):
        RecordingAsyncClient.last_get_url = url
        return ModelsResponse()

    async def post(self, *args, **kwargs):
        RecordingAsyncClient.last_json = kwargs.get('json')
        return ChatResponse()


def load_app_module():
    spec = importlib.util.spec_from_file_location('hermes_webui_app_model_selector_test', APP_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class ModelSelectorTests(unittest.TestCase):
    def test_top_toolbar_contains_model_selector_and_model_loading_script(self):
        source = APP_PATH.read_text()

        self.assertIn("id='modelSelect'", source)
        self.assertIn("class='model-toolbar'", source)
        self.assertIn("fetch('/api/models')", source)
        self.assertIn('loadModels', source)
        self.assertIn('selectedModel', source)
        self.assertIn("model: selectedModel", source)

    def test_models_api_returns_available_models_with_current_default(self):
        module = load_app_module()
        module.httpx.AsyncClient = RecordingAsyncClient
        module.SETTINGS['WEBUI_AUTH_ENABLED'] = 'false'
        client = TestClient(module.app)

        response = client.get('/api/models')

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data['current_model'], module.SETTINGS['HERMES_MODEL'])
        self.assertEqual(data['models'], ['hermes-agent', 'gpt-4.1', 'claude-sonnet-4'])
        self.assertTrue(RecordingAsyncClient.last_get_url.endswith('/v1/models'))

    def test_chat_api_forwards_selected_model_from_payload(self):
        module = load_app_module()
        module.httpx.AsyncClient = RecordingAsyncClient
        module.SETTINGS['WEBUI_AUTH_ENABLED'] = 'false'

        with TemporaryDirectory() as tmpdir:
            module.HISTORY_FILE = Path(tmpdir) / 'history.json'
            client = TestClient(module.app)
            response = client.post('/api/chat', json={
                'model': 'gpt-4.1',
                'messages': [{'role': 'user', 'content': 'hello'}],
            })

        self.assertEqual(response.status_code, 200)
        self.assertEqual(RecordingAsyncClient.last_json['model'], 'gpt-4.1')


if __name__ == '__main__':
    unittest.main()
