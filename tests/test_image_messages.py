import importlib.util
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from fastapi.testclient import TestClient

APP_PATH = Path('/opt/hermes-webui-git/app.py')


class RecordingResponse:
    status_code = 200
    text = 'ok'

    def json(self):
        return {'choices': [{'message': {'content': '我看到了图片'}}]}


class RecordingAsyncClient:
    last_json = None

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, *args, **kwargs):
        RecordingAsyncClient.last_json = kwargs.get('json')
        return RecordingResponse()

    async def get(self, *args, **kwargs):
        return RecordingResponse()


def load_app_module():
    spec = importlib.util.spec_from_file_location('hermes_webui_app_image_test', APP_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class ImageMessageSupportTests(unittest.TestCase):
    def test_multimodal_user_message_is_preserved_in_api_payload_and_history(self):
        module = load_app_module()
        module.httpx.AsyncClient = RecordingAsyncClient
        module.SETTINGS['WEBUI_AUTH_ENABLED'] = 'false'

        user_message = {
            'role': 'user',
            'content': [
                {'type': 'text', 'text': '这张图是什么？'},
                {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,aGVsbG8='}},
            ],
        }

        with TemporaryDirectory() as tmpdir:
            module.HISTORY_FILE = Path(tmpdir) / 'history.json'
            client = TestClient(module.app)

            response = client.post('/api/chat', json={'messages': [user_message]})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(RecordingAsyncClient.last_json['messages'][0], user_message)

            history = client.get('/api/history').json()['messages']
            self.assertEqual(history[0], user_message)
            self.assertEqual(history[1], {'role': 'assistant', 'content': '我看到了图片'})
            self.assertEqual(json.loads(module.HISTORY_FILE.read_text()), history)

    def test_frontend_contains_image_picker_preview_and_multimodal_payload_builder(self):
        source = APP_PATH.read_text()
        self.assertIn("type='file'", source)
        self.assertIn("accept='image/*'", source)
        self.assertIn('selectedImages', source)
        self.assertIn('readAsDataURL', source)
        self.assertIn('image_url', source)
        self.assertIn('renderContent', source)
        self.assertIn('attachment-preview', source)


if __name__ == '__main__':
    unittest.main()
