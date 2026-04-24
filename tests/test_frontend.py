import importlib.util
from pathlib import Path
import asyncio
import re
import subprocess
import tempfile
import unittest

APP_PATH = Path('/opt/hermes-webui-git/app.py')


def load_app_module():
    spec = importlib.util.spec_from_file_location('hermes_webui_app_ui_test', APP_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class FrontendBehaviorTests(unittest.TestCase):
    def test_index_includes_enter_to_send_keyboard_handler_and_chatgpt_style_shell(self):
        module = load_app_module()
        module.SETTINGS['WEBUI_AUTH_ENABLED'] = 'false'

        body = module.page_shell('') + module.CSS
        index_source = APP_PATH.read_text()

        self.assertIn('msg.addEventListener(\'keydown\'', index_source)
        self.assertIn('!e.shiftKey', index_source)
        self.assertIn("form.requestSubmit()", index_source)
        self.assertIn('chatgpt-shell', index_source)
        self.assertIn('sidebar', index_source)
        self.assertIn('composer', index_source)
        self.assertIn('New chat', index_source)
        self.assertIn('ChatGPT', body + index_source)

    def test_rendered_inline_script_is_valid_javascript(self):
        module = load_app_module()
        module.SETTINGS['WEBUI_AUTH_ENABLED'] = 'false'
        html = asyncio.run(module.index(type('Req', (), {'session': {}})()))
        script_match = re.search(r'<script>(.*?)</script>', html, re.S)
        self.assertIsNotNone(script_match)
        script = script_match.group(1)
        with tempfile.NamedTemporaryFile('w', suffix='.js', delete=False) as tmp:
            tmp.write(script)
            script_path = tmp.name
        result = subprocess.run(['node', '--check', script_path], text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("join('\n')", script)
        self.assertIn("join('\\n')", script)


if __name__ == '__main__':
    unittest.main()
