import importlib.util
from pathlib import Path
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


if __name__ == '__main__':
    unittest.main()
