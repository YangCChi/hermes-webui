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


class MediaResponse:
    status_code = 200
    text = 'ok'

    def json(self):
        return {'choices': [{'message': {'content': '这是苹果图片：\n\nMEDIA:/tmp/apple.png'}}]}


class RecordingAsyncClient:
    last_json = None
    response_cls = RecordingResponse

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, *args, **kwargs):
        RecordingAsyncClient.last_json = kwargs.get('json')
        return self.response_cls()

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
        RecordingAsyncClient.response_cls = RecordingResponse
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

    def test_assistant_media_file_reference_is_returned_and_saved_as_renderable_image_part(self):
        module = load_app_module()
        module.httpx.AsyncClient = RecordingAsyncClient
        module.SETTINGS['WEBUI_AUTH_ENABLED'] = 'false'
        RecordingAsyncClient.response_cls = MediaResponse

        with TemporaryDirectory() as tmpdir:
            history_file = Path(tmpdir) / 'history.json'
            media_file = Path(tmpdir) / 'apple.png'
            media_file.write_bytes(
                b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01'
                b'\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc`````\x00\x00\x00\x05\x00\x01'
                b'\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82'
            )
            module.HISTORY_FILE = history_file
            client = TestClient(module.app)

            response = client.post('/api/chat', json={'messages': [{'role': 'user', 'content': '发苹果图片'}]})
            self.assertEqual(response.status_code, 200)
            content = response.json()['content']
            self.assertIsInstance(content, list)
            self.assertEqual(content[0], {'type': 'text', 'text': '这是苹果图片：'})
            self.assertEqual(content[1]['type'], 'image_url')
            self.assertTrue(content[1]['image_url']['url'].startswith('/api/media/'))

            history = client.get('/api/history').json()['messages']
            self.assertEqual(history[-1]['role'], 'assistant')
            self.assertEqual(history[-1]['content'], content)
            self.assertEqual(json.loads(history_file.read_text()), history)

    def test_frontend_contains_image_picker_preview_and_multimodal_payload_builder(self):
        source = APP_PATH.read_text()
        self.assertIn("type='file'", source)
        self.assertIn("accept='image/*'", source)
        self.assertIn('selectedImages', source)
        self.assertIn('readAsDataURL', source)
        self.assertIn('image_url', source)
        self.assertIn('renderContent', source)
        self.assertIn('attachment-preview', source)
        self.assertIn('MEDIA:', source)
        self.assertIn("alt='图片'", source)
        self.assertIn('compressImageFile', source)
        self.assertIn('MAX_IMAGE_UPLOAD_BYTES', source)

    def test_api_payload_drops_relative_media_urls_before_forwarding(self):
        RecordingAsyncClient.response_cls = RecordingResponse
        module = load_app_module()
        module.httpx.AsyncClient = RecordingAsyncClient
        module.SETTINGS['WEBUI_AUTH_ENABLED'] = 'false'

        messages = [
            {
                'role': 'assistant',
                'content': [
                    {'type': 'text', 'text': '这是上次生成的图'},
                    {'type': 'image_url', 'image_url': {'url': '/api/media/local%3A/tmp/apple.png'}},
                ],
            },
            {'role': 'user', 'content': '继续'},
        ]

        with TemporaryDirectory() as tmpdir:
            module.HISTORY_FILE = Path(tmpdir) / 'history.json'
            client = TestClient(module.app)
            response = client.post('/api/chat', json={'messages': messages})

        self.assertEqual(response.status_code, 200)
        forwarded = RecordingAsyncClient.last_json['messages']
        self.assertEqual(forwarded[0]['content'], '这是上次生成的图\n[图片已在网页显示，未再次发送给模型]')
        self.assertNotIn('/api/media/', json.dumps(forwarded, ensure_ascii=False))

    def test_old_data_url_images_are_not_resent_to_avoid_large_request_bodies(self):
        RecordingAsyncClient.response_cls = RecordingResponse
        module = load_app_module()
        module.httpx.AsyncClient = RecordingAsyncClient
        module.SETTINGS['WEBUI_AUTH_ENABLED'] = 'false'
        old_image = 'data:image/png;base64,' + ('a' * 120_000)
        latest_image = 'data:image/png;base64,' + ('b' * 100)

        messages = [
            {'role': 'user', 'content': [{'type': 'text', 'text': '旧图'}, {'type': 'image_url', 'image_url': {'url': old_image}}]},
            {'role': 'assistant', 'content': '已看过旧图'},
            {'role': 'user', 'content': [{'type': 'text', 'text': '新图'}, {'type': 'image_url', 'image_url': {'url': latest_image}}]},
        ]

        with TemporaryDirectory() as tmpdir:
            module.HISTORY_FILE = Path(tmpdir) / 'history.json'
            client = TestClient(module.app)
            response = client.post('/api/chat', json={'messages': messages})

        self.assertEqual(response.status_code, 200)
        forwarded = RecordingAsyncClient.last_json['messages']
        self.assertNotIn(old_image, json.dumps(forwarded))
        self.assertIn(latest_image, json.dumps(forwarded))
        self.assertIn('[历史图片已省略以控制请求大小]', forwarded[0]['content'])


if __name__ == '__main__':
    unittest.main()
