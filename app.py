#!/usr/bin/env python3
"""Small Web UI for the local Hermes API server."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import mimetypes
import os
import re
import secrets
import sqlite3
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote

MessageContent = str | list[dict[str, Any]]
NormalizedMessage = dict[str, Any]

import httpx
from fastapi import FastAPI, Form, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from starlette.middleware.sessions import SessionMiddleware

BASE_DIR = Path('/opt/hermes-webui')
ENV_FILE = BASE_DIR / '.env'
HERMES_ENV_FILE = Path('/root/.hermes/.env')
HISTORY_FILE = BASE_DIR / 'chat-history.json'
ACTIVITY_DB_FILE = Path('/root/.hermes/state.db')
ACTIVITY_SOURCE = 'webui'
MAX_HISTORY_MESSAGES = 200
MAX_ACTIVITY_MESSAGES = 120
DEFAULT_API_BASE = 'http://127.0.0.1:8642'

APP_VERSION = 'v0.0.8'
MEDIA_TOKEN_PREFIX='local:'
MEDIA_REFERENCE_RE = re.compile(r'(?m)^MEDIA:(?P<path>/[^\r\n]+)\s*$')
ALLOWED_MEDIA_DIRS = (Path('/tmp'), Path('/var/tmp'), BASE_DIR / 'media')
ALLOWED_IMAGE_MIME_TYPES = {'image/png', 'image/jpeg', 'image/gif', 'image/webp', 'image/svg+xml'}
MAX_API_MESSAGES = 40
MAX_API_BODY_BYTES = 900_000
HISTORICAL_IMAGE_PLACEHOLDER = '[历史图片已省略以控制请求大小]'
LOCAL_MEDIA_PLACEHOLDER = '[图片已在网页显示，未再次发送给模型]'
CHANGELOG = [
    {
        'version': 'v0.0.8',
        'updated_at': '2026-04-24',
        'changes': [
            '重做过程可视化页面：新增摘要指标、自动刷新开关、筛选器、会话卡片和时间线视图。',
            '活动记录按用户输入、助手输出、工具调用用不同颜色和图标展示，长内容可展开和复制。',
            '在页面中列出过程可视化改进方案，方便后续继续迭代。',
        ],
    },
    {
        'version': 'v0.0.7',
        'updated_at': '2026-04-24',
        'changes': [
            '新增过程查看页，可在 WebUI 中查看 Hermes 最近做过什么以及当前活动。',
            '新增 /api/activity 接口，从 Hermes 状态库读取最近输入、输出和工具调用记录。',
            '聊天页会记录 WebUI 发出的用户消息和助手回复，便于离开终端后回看进度。',
        ],
    },
    {
        'version': 'v0.0.6',
        'updated_at': '2026-04-24',
        'changes': [
            '顶部工具栏新增模型选择器，可从当前 Hermes API 可用模型中选择对话模型。',
            '新增 /api/models 接口，自动读取 OpenAI 兼容 /v1/models 列表。',
            '发送消息时会携带当前选择的模型，后端按选择转发给 Hermes API。',
        ],
    },
    {
        'version': 'v0.0.5',
        'updated_at': '2026-04-24',
        'changes': [
            '新增设置页，把 WebUI 状态、Hermes API 配置摘要和版本更新日志集中到一个入口。',
            '侧边栏新增“设置”入口，聊天页底部保留当前版本信息。',
            '新增结构化设置接口 /api/settings，便于前端后续扩展更多设置项。',
        ],
    },
    {
        'version': 'v0.0.4',
        'updated_at': '2026-04-24',
        'changes': [
            '修复历史中的 /api/media 本地图片被再次发给模型导致 invalid_image_url 的问题。',
            '请求模型时只保留最新用户消息里的 data:image 图片，历史图片改为文字占位，避免 Request body too large。',
            '前端上传图片会先压缩和缩放，降低大图导致请求体过大的概率。',
        ],
    },
    {
        'version': 'v0.0.3',
        'updated_at': '2026-04-24',
        'changes': [
            '支持把助手返回的 MEDIA:/path 图片引用转换为可显示图片。',
            '新增安全媒体读取接口，聊天区和历史记录可渲染助手生成的本地图片。',
            '改进图片消息回归测试，覆盖发送图片和接收图片历史保存。',
        ],
    },
    {
        'version': 'v0.0.2',
        'updated_at': '2026-04-24',
        'changes': [
            '支持在 WebUI 中选择、预览并发送图片。',
            '聊天区和历史记录可显示用户发送的图片。',
            '后端保留 OpenAI 兼容的 image_url 多模态消息格式转发给 Hermes API。',
        ],
    },
    {
        'version': 'v0.0.1',
        'updated_at': '2026-04-24',
        'changes': [
            '从 0.0.1 开始记录 Hermes WebUI 版本。',
            '保留 ChatGPT 风格界面、聊天历史、版本更新日志和本地 Hermes API 对话能力。',
        ],
    },
]


def read_env_file(path: Path) -> dict[str, str]:
    data: dict[str, str] = {}
    if not path.exists():
        return data
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, value = line.split('=', 1)
        data[key.strip()] = value.strip().strip('"').strip("'")
    return data


def load_settings() -> dict[str, str]:
    settings = read_env_file(ENV_FILE)
    hermes = read_env_file(HERMES_ENV_FILE)
    return {
        'WEBUI_AUTH_ENABLED': os.getenv('WEBUI_AUTH_ENABLED') or settings.get('WEBUI_AUTH_ENABLED', 'false'),
        'WEBUI_PASSWORD_HASH': os.getenv('WEBUI_PASSWORD_HASH') or settings.get('WEBUI_PASSWORD_HASH', ''),
        'WEBUI_SESSION_SECRET': os.getenv('WEBUI_SESSION_SECRET') or settings.get('WEBUI_SESSION_SECRET', secrets.token_urlsafe(32)),
        'HERMES_API_BASE': os.getenv('HERMES_API_BASE') or settings.get('HERMES_API_BASE', DEFAULT_API_BASE),
        'HERMES_API_KEY': os.getenv('HERMES_API_KEY') or settings.get('HERMES_API_KEY') or hermes.get('API_SERVER_KEY', ''),
        'HERMES_MODEL': os.getenv('HERMES_MODEL') or settings.get('HERMES_MODEL') or hermes.get('API_SERVER_MODEL_NAME', 'hermes-agent'),
    }


def hash_password(password: str, *, iterations: int = 260_000) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac('sha256', password.encode(), salt.encode(), iterations)
    encoded = base64.b64encode(digest).decode()
    return f'pbkdf2_sha256${iterations}${salt}${encoded}'


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, iterations_s, salt, expected = encoded.split('$', 3)
        if algorithm != 'pbkdf2_sha256':
            return False
        iterations = int(iterations_s)
        digest = hashlib.pbkdf2_hmac('sha256', password.encode(), salt.encode(), iterations)
        actual = base64.b64encode(digest).decode()
        return hmac.compare_digest(actual, expected)
    except Exception:
        return False


def mask_secret(value: str) -> str:
    if not value:
        return '[missing]'
    if len(value) < 12:
        return '[set]'
    return f'{value[:4]}...{value[-4:]}'


def normalize_model_id(value: Any) -> str:
    model = str(value or '').strip()
    if not model:
        return SETTINGS['HERMES_MODEL']
    return model[:120]


def parse_models_response(data: Any) -> list[str]:
    models: list[str] = []
    if isinstance(data, dict):
        items = data.get('data') or data.get('models') or []
    elif isinstance(data, list):
        items = data
    else:
        items = []
    for item in items:
        model_id = ''
        if isinstance(item, dict):
            model_id = str(item.get('id') or item.get('name') or '').strip()
        else:
            model_id = str(item).strip()
        if model_id and model_id not in models:
            models.append(model_id)
    default_model = SETTINGS['HERMES_MODEL']
    if default_model and default_model not in models:
        models.insert(0, default_model)
    return models or [default_model]


def normalize_content(content: Any) -> MessageContent | None:
    if isinstance(content, str):
        return content if content else None
    if isinstance(content, list):
        normalized_parts: list[dict[str, Any]] = []
        for part in content:
            if not isinstance(part, dict):
                continue
            part_type = str(part.get('type', '')).strip()
            if part_type == 'text':
                text = str(part.get('text', ''))
                if text:
                    normalized_parts.append({'type': 'text', 'text': text})
            elif part_type == 'image_url':
                image_url = part.get('image_url')
                url = ''
                if isinstance(image_url, dict):
                    url = str(image_url.get('url', ''))
                elif isinstance(image_url, str):
                    url = image_url
                if url.startswith(('data:image/', 'http://', 'https://', '/api/media/')):
                    normalized_parts.append({'type': 'image_url', 'image_url': {'url': url}})
        return normalized_parts or None
    return None


def media_token_for_path(path: Path) -> str:
    return MEDIA_TOKEN_PREFIX + quote(str(path.resolve()), safe='')


def path_from_media_token(token: str) -> Path | None:
    if not token.startswith(MEDIA_TOKEN_PREFIX):
        return None
    try:
        return Path(unquote(token[len(MEDIA_TOKEN_PREFIX):])).resolve()
    except Exception:
        return None


def is_allowed_media_path(path: Path) -> bool:
    try:
        resolved = path.resolve()
        return any(resolved == root.resolve() or root.resolve() in resolved.parents for root in ALLOWED_MEDIA_DIRS)
    except Exception:
        return False


def image_mime_type(path: Path) -> str | None:
    mime, _ = mimetypes.guess_type(str(path))
    if mime in ALLOWED_IMAGE_MIME_TYPES:
        return mime
    return None


def media_url_for_path(path: Path) -> str | None:
    if not path.exists() or not path.is_file() or not is_allowed_media_path(path):
        return None
    if image_mime_type(path) is None:
        return None
    return f'/api/media/{media_token_for_path(path)}'


def normalize_assistant_content(content: Any) -> MessageContent | None:
    if not isinstance(content, str):
        return normalize_content(content)
    parts: list[dict[str, Any]] = []
    cursor = 0
    for match in MEDIA_REFERENCE_RE.finditer(content):
        text = content[cursor:match.start()].strip()
        if text:
            parts.append({'type': 'text', 'text': text})
        media_url = media_url_for_path(Path(match.group('path')))
        if media_url:
            parts.append({'type': 'image_url', 'image_url': {'url': media_url}})
        else:
            literal = match.group(0).strip()
            if literal:
                parts.append({'type': 'text', 'text': literal})
        cursor = match.end()
    if not parts:
        return normalize_content(content)
    trailing = content[cursor:].strip()
    if trailing:
        parts.append({'type': 'text', 'text': trailing})
    return parts or None


def normalize_messages(messages: list[dict[str, Any]]) -> list[NormalizedMessage]:
    normalized: list[NormalizedMessage] = []
    for message in messages:
        role = str(message.get('role', '')).strip()
        content = normalize_content(message.get('content'))
        if role in {'user', 'assistant', 'system'} and content:
            normalized.append({'role': role, 'content': content})
    return normalized[-MAX_HISTORY_MESSAGES:]


def content_for_api(content: Any, *, preserve_images: bool) -> MessageContent | None:
    """Convert stored/renderable content into content safe for the upstream API."""
    normalized = normalize_content(content)
    if isinstance(normalized, str) or normalized is None:
        return normalized
    api_parts: list[dict[str, Any]] = []
    omitted_historical_image = False
    omitted_local_media = False
    for part in normalized:
        part_type = part.get('type')
        if part_type == 'text':
            text = str(part.get('text', ''))
            if text:
                api_parts.append({'type': 'text', 'text': text})
            continue
        if part_type != 'image_url':
            continue
        url = str(part.get('image_url', {}).get('url', ''))
        if url.startswith(('http://', 'https://')):
            api_parts.append({'type': 'image_url', 'image_url': {'url': url}})
        elif url.startswith('data:image/') and preserve_images:
            api_parts.append({'type': 'image_url', 'image_url': {'url': url}})
        elif url.startswith('/api/media/'):
            omitted_local_media = True
        else:
            omitted_historical_image = True
    if omitted_local_media:
        api_parts.append({'type': 'text', 'text': LOCAL_MEDIA_PLACEHOLDER})
    if omitted_historical_image:
        api_parts.append({'type': 'text', 'text': HISTORICAL_IMAGE_PLACEHOLDER})
    if not api_parts:
        return None
    if all(part.get('type') == 'text' for part in api_parts):
        return '\n'.join(str(part.get('text', '')) for part in api_parts if part.get('text'))
    return api_parts


def prepare_messages_for_api(messages: list[dict[str, Any]]) -> list[NormalizedMessage]:
    """Trim history and omit old/local images before forwarding to the model."""
    normalized = normalize_messages(messages)[-MAX_API_MESSAGES:]
    if not normalized:
        return []
    latest_user_index = next((idx for idx in range(len(normalized) - 1, -1, -1) if normalized[idx].get('role') == 'user'), len(normalized) - 1)
    prepared: list[NormalizedMessage] = []
    for idx, message in enumerate(normalized):
        content = content_for_api(message.get('content'), preserve_images=(idx == latest_user_index))
        if content:
            prepared.append({'role': message['role'], 'content': content})

    while prepared and len(json.dumps(prepared, ensure_ascii=False)) > MAX_API_BODY_BYTES:
        drop_index = next((idx for idx, message in enumerate(prepared) if idx != len(prepared) - 1 and message.get('role') != 'system'), 0)
        prepared.pop(drop_index)
    return prepared


def read_history() -> list[NormalizedMessage]:
    if not HISTORY_FILE.exists():
        return []
    try:
        data = json.loads(HISTORY_FILE.read_text())
        if isinstance(data, list):
            return normalize_messages(data)
    except Exception:
        return []
    return []


def write_history(messages: list[dict[str, Any]]) -> list[NormalizedMessage]:
    normalized = normalize_messages(messages)
    HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = HISTORY_FILE.with_suffix('.json.tmp')
    tmp.write_text(json.dumps(normalized, ensure_ascii=False, indent=2))
    tmp.replace(HISTORY_FILE)
    return normalized


def append_history(*messages: dict[str, Any]) -> list[NormalizedMessage]:
    history = read_history()
    history.extend(messages)
    return write_history(history)



def init_activity_db(path: Path = ACTIVITY_DB_FILE) -> None:
    """Create the minimal Hermes state schema needed for local WebUI activity logging."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY,
                source TEXT NOT NULL,
                user_id TEXT,
                model TEXT,
                model_config TEXT,
                system_prompt TEXT,
                parent_session_id TEXT,
                started_at REAL NOT NULL,
                ended_at REAL,
                end_reason TEXT,
                message_count INTEGER DEFAULT 0,
                tool_call_count INTEGER DEFAULT 0
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL REFERENCES sessions(id),
                role TEXT NOT NULL,
                content TEXT,
                tool_call_id TEXT,
                tool_calls TEXT,
                tool_name TEXT,
                timestamp REAL NOT NULL,
                token_count INTEGER,
                finish_reason TEXT
            )
            """
        )
        conn.commit()


def activity_session_id() -> str:
    return f"webui-{time.strftime('%Y%m%d')}"


def truncate_activity_content(content: Any, max_chars: int = 4000) -> str:
    if isinstance(content, str):
        text = content
    else:
        text = json.dumps(content, ensure_ascii=False)
    if len(text) > max_chars:
        return text[:max_chars] + '…'
    return text


def record_activity_message(role: str, content: Any, *, tool_name: str | None = None) -> None:
    """Append a WebUI-originated activity event to Hermes' state DB when available."""
    try:
        init_activity_db(ACTIVITY_DB_FILE)
        session_id = activity_session_id()
        now = time.time()
        text = truncate_activity_content(content)
        with sqlite3.connect(ACTIVITY_DB_FILE) as conn:
            conn.execute(
                """
                INSERT INTO sessions (id, source, model, started_at, message_count, tool_call_count)
                VALUES (?, ?, ?, ?, 0, 0)
                ON CONFLICT(id) DO NOTHING
                """,
                (session_id, ACTIVITY_SOURCE, SETTINGS['HERMES_MODEL'], now),
            )
            conn.execute(
                """
                INSERT INTO messages (session_id, role, content, tool_name, timestamp)
                VALUES (?, ?, ?, ?, ?)
                """,
                (session_id, role, text, tool_name, now),
            )
            conn.execute(
                """
                UPDATE sessions
                SET message_count = (SELECT COUNT(*) FROM messages WHERE session_id = ?),
                    tool_call_count = (SELECT COUNT(*) FROM messages WHERE session_id = ? AND tool_name IS NOT NULL)
                WHERE id = ?
                """,
                (session_id, session_id, session_id),
            )
            conn.commit()
    except Exception:
        # Activity logging should never break chat.
        return


def format_activity_time(ts: Any) -> str:
    try:
        return time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(float(ts)))
    except Exception:
        return ''


def read_activity(limit: int = MAX_ACTIVITY_MESSAGES) -> dict[str, Any]:
    limit = max(1, min(int(limit or MAX_ACTIVITY_MESSAGES), 300))
    if not ACTIVITY_DB_FILE.exists():
        return {'current_session_id': activity_session_id(), 'sessions': [], 'messages': []}
    try:
        with sqlite3.connect(f'file:{ACTIVITY_DB_FILE}?mode=ro', uri=True) as conn:
            conn.row_factory = sqlite3.Row
            sessions = [dict(row) for row in conn.execute(
                """
                SELECT id, source, model, started_at, ended_at, end_reason,
                       COALESCE(message_count, 0) AS message_count,
                       COALESCE(tool_call_count, 0) AS tool_call_count
                FROM sessions
                ORDER BY COALESCE(ended_at, started_at) DESC
                LIMIT 12
                """
            )]
            rows = [dict(row) for row in conn.execute(
                """
                SELECT m.id, m.session_id, m.role, m.content, m.tool_name, m.timestamp, s.source
                FROM messages m
                LEFT JOIN sessions s ON s.id = m.session_id
                ORDER BY m.timestamp DESC, m.id DESC
                LIMIT ?
                """,
                (limit,),
            )]
    except Exception as exc:
        return {'current_session_id': activity_session_id(), 'sessions': [], 'messages': [], 'error': str(exc)}
    for session in sessions:
        session['started_at_text'] = format_activity_time(session.get('started_at'))
        session['ended_at_text'] = format_activity_time(session.get('ended_at')) if session.get('ended_at') else ''
        session['active'] = not bool(session.get('ended_at'))
    messages = []
    for row in reversed(rows):
        content = row.get('content') or ''
        messages.append({
            'id': row.get('id'),
            'session_id': row.get('session_id'),
            'source': row.get('source') or '',
            'role': row.get('role') or '',
            'tool_name': row.get('tool_name') or '',
            'content': content,
            'timestamp': row.get('timestamp'),
            'time': format_activity_time(row.get('timestamp')),
        })
    return {'current_session_id': activity_session_id(), 'sessions': sessions, 'messages': messages}

SETTINGS = load_settings()
app = FastAPI(title='Hermes WebUI')
app.add_middleware(SessionMiddleware, secret_key=SETTINGS['WEBUI_SESSION_SECRET'], same_site='lax')

CSS = """
:root{color-scheme:light;--bg:#f7f7f8;--sidebar:#202123;--sidebar-soft:#2a2b32;--surface:#ffffff;--surface-alt:#f7f7f8;--border:#e5e5e5;--text:#202123;--muted:#6e6e80;--assistant:#f7f7f8;--user:#ffffff;--accent:#10a37f;--accent-dark:#0d8f6f;--danger:#ef4444;--good:#10a37f}*{box-sizing:border-box}html,body{height:100%}body{margin:0;background:var(--bg);font-family:ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;color:var(--text)}main{height:100%;padding:0}.chatgpt-shell{height:100vh;display:grid;grid-template-columns:260px minmax(0,1fr);background:var(--bg)}.sidebar{background:var(--sidebar);color:#ececf1;display:flex;flex-direction:column;padding:12px;gap:12px}.new-chat{height:44px;border:1px solid rgba(255,255,255,.22);border-radius:8px;background:transparent;color:#ececf1;display:flex;align-items:center;gap:10px;padding:0 12px;font-weight:500}.side-title{font-size:13px;color:#c5c5d2;padding:8px 4px}.side-link{color:#ececf1;border-radius:8px;padding:10px 12px;text-decoration:none;font-size:14px}.side-link:hover{background:var(--sidebar-soft)}.side-footer{margin-top:auto;color:#9ca3af;font-size:12px;line-height:1.5;padding:8px 4px}.main-chat{min-width:0;display:flex;flex-direction:column;height:100vh}.topbar{height:56px;border-bottom:1px solid var(--border);display:flex;align-items:center;justify-content:space-between;padding:0 20px;background:rgba(255,255,255,.8);backdrop-filter:blur(12px)}.brand{font-size:17px;font-weight:650}.muted{color:var(--muted)}.small{font-size:13px}.pill{display:inline-flex;gap:8px;align-items:center;border:1px solid var(--border);border-radius:999px;padding:7px 10px;color:var(--muted);font-size:13px;background:#fff}.dot{width:8px;height:8px;border-radius:99px;background:var(--danger)}.dot.ok{background:var(--good)}#chat{flex:1;overflow:auto;padding:0;background:var(--bg)}.msg{display:flex;border-bottom:1px solid rgba(0,0,0,.04)}.msg-inner{width:min(820px,100%);margin:0 auto;display:grid;grid-template-columns:38px minmax(0,1fr);gap:18px;padding:24px 20px}.avatar{width:32px;height:32px;border-radius:4px;display:flex;align-items:center;justify-content:center;font-weight:700;font-size:13px;color:white;flex:none}.user .avatar{background:#5436da}.assistant .avatar{background:var(--accent)}.sys .avatar{background:#8e8ea0}.bubble{white-space:pre-wrap;line-height:1.68;font-size:15.5px;overflow-wrap:anywhere}.assistant{background:var(--assistant)}.user,.sys{background:var(--user)}.sys .bubble{color:var(--muted)}.composer-wrap{border-top:1px solid var(--border);background:linear-gradient(180deg,rgba(247,247,248,0),var(--bg) 18%);padding:18px 18px 24px}.composer{width:min(820px,100%);margin:0 auto;position:relative;border:1px solid #d9d9e3;border-radius:14px;background:#fff;box-shadow:0 8px 28px rgba(0,0,0,.08);display:flex;align-items:flex-end;padding:10px 52px 10px 14px}.attach-button{width:34px;height:34px;border:0;border-radius:8px;background:transparent;color:var(--muted);font-size:22px;line-height:1;cursor:pointer;margin-right:8px}.attach-button:hover{background:var(--surface-alt);color:var(--text)}.composer textarea{width:100%;min-height:28px;max-height:180px;height:28px;resize:none;border:0;outline:0;background:transparent;color:var(--text);font:inherit;line-height:1.5;padding:2px 0}.send-button{position:absolute;right:10px;bottom:9px;width:34px;height:34px;border:0;border-radius:8px;background:var(--accent);color:white;font-weight:800;cursor:pointer}.send-button:disabled{background:#d9d9e3;cursor:not-allowed}.attachment-preview{width:min(820px,100%);margin:0 auto 10px;display:flex;gap:8px;flex-wrap:wrap}.attachment-thumb{position:relative;border:1px solid var(--border);border-radius:10px;background:#fff;padding:4px;box-shadow:0 4px 16px rgba(0,0,0,.06)}.attachment-thumb img{width:74px;height:74px;object-fit:cover;border-radius:7px;display:block}.attachment-remove{position:absolute;right:-7px;top:-7px;width:22px;height:22px;border:0;border-radius:99px;background:#202123;color:white;cursor:pointer}.message-image{display:block;max-width:min(420px,100%);max-height:360px;border-radius:12px;margin:8px 0;border:1px solid var(--border)}.message-text{white-space:pre-wrap}.hint{width:min(820px,100%);margin:8px auto 0;text-align:center;color:var(--muted);font-size:12px}.login{max-width:430px;margin:12vh auto;background:white;border:1px solid var(--border);border-radius:16px;box-shadow:0 16px 50px rgba(0,0,0,.08);padding:24px}input{width:100%;border:1px solid var(--border);border-radius:10px;background:#fff;color:var(--text);padding:13px 14px;font:inherit}button{font:inherit}.login button{border:0;border-radius:10px;background:var(--accent);color:white;font-weight:700;cursor:pointer}.error{color:#dc2626}a{color:#0d8f6f;text-decoration:none}.changelog-page{min-height:100vh;background:var(--bg);padding:42px 18px}.changelog-hero,.release-list{width:min(860px,100%);margin:0 auto}.changelog-hero{padding:24px 0}.changelog-hero h1{font-size:38px;letter-spacing:-.04em;margin:18px 0 8px}.back-link{display:inline-flex;color:#0d8f6f;text-decoration:none;margin-bottom:10px}.release-card{background:#fff;border:1px solid var(--border);border-radius:16px;padding:22px 24px;margin:0 0 16px;box-shadow:0 10px 30px rgba(0,0,0,.04)}.release-head{display:flex;justify-content:space-between;align-items:baseline;gap:12px;border-bottom:1px solid var(--border);padding-bottom:12px;margin-bottom:14px}.release-head h2{margin:0;font-size:24px}.release-card h3{font-size:15px;margin:8px 0;color:var(--text)}.release-card li{margin:7px 0;line-height:1.6}code{background:#ececf1;border-radius:6px;padding:2px 6px}.model-toolbar{display:flex;align-items:center;gap:8px}.model-label{color:var(--muted);font-size:13px}.model-select{min-width:190px;max-width:280px;border:1px solid var(--border);border-radius:10px;background:#fff;color:var(--text);padding:8px 32px 8px 10px;font:inherit;font-size:13px}.model-select:disabled{color:var(--muted);background:#f3f4f6}.topbar-actions{display:flex;align-items:center;gap:12px}@media(max-width:760px){.chatgpt-shell{grid-template-columns:1fr}.sidebar{display:none}.topbar{padding:0 14px}.msg-inner{grid-template-columns:32px minmax(0,1fr);gap:12px;padding:20px 14px}.composer-wrap{padding:14px 12px 18px}}.settings-page{min-height:100vh;background:var(--bg);padding:28px}.settings-hero{max-width:980px;margin:0 auto 18px}.settings-grid{max-width:980px;margin:0 auto;display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:14px}.settings-card{background:var(--surface);border:1px solid var(--border);border-radius:16px;padding:18px;box-shadow:0 8px 30px rgba(0,0,0,.04)}.settings-card h2{margin:0 0 10px;font-size:18px}.settings-row{display:flex;justify-content:space-between;gap:16px;border-top:1px solid var(--border);padding:10px 0}.settings-row:first-of-type{border-top:0}.settings-key{color:var(--muted)}.settings-value{text-align:right;overflow-wrap:anywhere}.settings-changelog{max-width:980px;margin:18px auto 0}.settings-changelog .release-list{display:grid;gap:14px}

.activity-page{padding:0;background:linear-gradient(135deg,#f7f7f8 0%,#eef7f4 52%,#f8fafc 100%)}.activity-hero{max-width:1180px;margin:0 auto;padding:34px 24px 18px}.activity-hero h1{font-size:40px;letter-spacing:-.045em;margin:14px 0 10px}.activity-hero-actions{display:flex;align-items:center;gap:12px;flex-wrap:wrap}.activity-dashboard{max-width:1180px;margin:0 auto;padding:0 24px 34px;display:grid;grid-template-columns:320px minmax(0,1fr);gap:18px}.activity-commandbar{grid-column:1/-1;background:rgba(255,255,255,.82);border:1px solid var(--border);border-radius:18px;box-shadow:0 18px 45px rgba(16,163,127,.09);padding:14px;display:flex;align-items:center;justify-content:space-between;gap:14px;backdrop-filter:blur(16px)}.activity-filters{display:flex;gap:8px;flex-wrap:wrap}.activity-filter{border:1px solid var(--border);background:#fff;border-radius:999px;padding:8px 12px;color:var(--muted);cursor:pointer}.activity-filter.active{background:#202123;color:#fff;border-color:#202123}.activity-refresh{display:flex;align-items:center;gap:8px;color:var(--muted);font-size:13px}.activity-refresh input{width:auto}.activity-panel{background:rgba(255,255,255,.9);border:1px solid var(--border);border-radius:20px;box-shadow:0 18px 55px rgba(0,0,0,.06);padding:18px}.activity-metrics{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px;margin-bottom:16px}.activity-metric{border:1px solid #e6f3ef;background:linear-gradient(180deg,#fff,#f7fffc);border-radius:16px;padding:14px}.activity-metric-value{display:block;font-size:28px;font-weight:800;color:#0d8f6f;letter-spacing:-.04em}.activity-metric-label{display:block;color:var(--muted);font-size:12px;margin-top:3px}.activity-session-list{display:grid;gap:10px}.activity-session{border:1px solid var(--border);border-radius:14px;padding:12px;background:#fff;display:grid;gap:4px}.activity-session.active{border-color:#10a37f;box-shadow:0 0 0 3px rgba(16,163,127,.12)}.activity-session strong{font-size:14px}.activity-main{min-width:0}.activity-summary-card{margin-bottom:14px}.activity-plan{display:grid;gap:8px;margin-top:10px}.activity-plan-step{display:flex;gap:10px;align-items:flex-start;color:var(--muted);line-height:1.5}.activity-plan-step b{display:inline-flex;width:22px;height:22px;border-radius:99px;background:#dcfce7;color:#08785f;align-items:center;justify-content:center;font-size:12px;flex:none}.activity-timeline{position:relative;display:grid;gap:12px}.activity-timeline:before{content:'';position:absolute;left:22px;top:8px;bottom:8px;width:2px;background:linear-gradient(#10a37f,#d1d5db)}.activity-item{position:relative;display:grid;grid-template-columns:46px minmax(0,1fr);gap:12px;border:1px solid var(--border);border-radius:18px;background:#fff;padding:14px;box-shadow:0 8px 28px rgba(0,0,0,.04)}.activity-icon{position:relative;z-index:1;width:44px;height:44px;border-radius:14px;display:flex;align-items:center;justify-content:center;font-weight:800;color:#fff;background:#8e8ea0}.activity-item.user .activity-icon{background:#5436da}.activity-item.assistant .activity-icon{background:#10a37f}.activity-item.tool .activity-icon{background:#f59e0b}.activity-item.tool{border-color:#fde68a}.activity-item.user{border-color:#ddd6fe}.activity-item.assistant{border-color:#bbf7d0}.activity-meta{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:8px}.activity-badge{border:1px solid var(--border);border-radius:999px;padding:4px 8px;background:#f9fafb;color:var(--muted);font-size:12px}.activity-content{margin:0;white-space:pre-wrap;overflow-wrap:anywhere;line-height:1.55;max-height:180px;overflow:hidden;background:#f8fafc;border:1px solid #edf2f7;border-radius:12px;padding:12px}.activity-item.expanded .activity-content{max-height:none}.activity-actions{display:flex;gap:8px;margin-top:10px}.activity-mini-button{border:1px solid var(--border);border-radius:10px;background:#fff;color:var(--muted);padding:6px 9px;cursor:pointer}.activity-mini-button:hover{color:var(--text);border-color:#cbd5e1}.activity-empty{border:1px dashed var(--border);border-radius:18px;padding:28px;text-align:center;color:var(--muted);background:rgba(255,255,255,.68)}@media(max-width:900px){.activity-dashboard{grid-template-columns:1fr}.activity-commandbar{align-items:flex-start;flex-direction:column}.activity-hero h1{font-size:32px}}@media(max-width:620px){.activity-metrics{grid-template-columns:1fr}.activity-item{grid-template-columns:38px minmax(0,1fr);padding:12px}.activity-icon{width:36px;height:36px;border-radius:12px}.activity-timeline:before{left:18px}.activity-page{padding-bottom:24px}}
"""


def page_shell(body: str) -> str:
    return f"""<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>Hermes WebUI</title><style>{CSS}</style></head><body><main>{body}</main></body></html>"""


def auth_enabled() -> bool:
    return str(SETTINGS.get('WEBUI_AUTH_ENABLED', 'false')).lower() in {'1', 'true', 'yes', 'on'}


def is_logged_in(request: Request) -> bool:
    return (not auth_enabled()) or bool(request.session.get('logged_in'))


@app.get('/', response_class=HTMLResponse)
async def index(request: Request) -> str:
    if not is_logged_in(request):
        return RedirectResponse('/login', status_code=302)
    body = f"""
    <div class='chatgpt-shell'>
      <aside class='sidebar'>
        <button class='new-chat' type='button' onclick='clearHistory()'><span>＋</span><span>New chat</span></button>
        <div class='side-title'>Hermes Agent</div>
        <a class='side-link' href='/activity'>过程查看</a>
        <a class='side-link' href='/settings'>设置</a>
        <a class='side-link' href='/changelog'>版本更新日志</a>
        <div class='side-footer'>Version: {APP_VERSION}<br>ChatGPT-style interface<br>API: {SETTINGS['HERMES_API_BASE']}<br>Model: {SETTINGS['HERMES_MODEL']}<br>Key: {mask_secret(SETTINGS['HERMES_API_KEY'])}</div>
      </aside>
      <section class='main-chat'>
        <header class='topbar'>
          <div><div class='brand'>Hermes WebUI</div><div class='muted small'>模仿 ChatGPT 的简洁对话界面</div></div>
          <div class='topbar-actions'>
            <label class='model-toolbar' title='选择本次对话使用的大模型'>
              <span class='model-label'>模型</span>
              <select id='modelSelect' class='model-select'><option value='{SETTINGS['HERMES_MODEL']}'>加载模型中...</option></select>
            </label>
            <span id='status' class='pill'><span class='dot'></span><span>checking</span></span>
          </div>
        </header>
        <div id='chat'><div class='msg sys'><div class='msg-inner'><div class='avatar'>H</div><div class='bubble'>已连接 WebUI。你可以在这里向 Hermes 发消息；Hermes 仍然能使用服务器工具。</div></div></div></div>
        <div class='composer-wrap'>
          <div id='attachmentPreview' class='attachment-preview'></div>
          <form id='sendForm' class='composer'>
            <input id='imageInput' type='file' accept='image/*' multiple hidden>
            <button id='attachBtn' class='attach-button' type='button' title='添加图片' onclick='imageInput.click()'>＋</button>
            <textarea id='message' placeholder='Message Hermes...' autofocus rows='1'></textarea>
            <button id='sendBtn' class='send-button' type='submit' title='发送'>↑</button>
          </form>
          <div class='hint'>按 Enter 发送，Shift + Enter 换行；点击＋可添加图片</div>
        </div>
      </section>
    </div>
    <script>
    const chat=document.getElementById('chat'), form=document.getElementById('sendForm'), msg=document.getElementById('message'), btn=document.getElementById('sendBtn'), statusEl=document.getElementById('status'), imageInput=document.getElementById('imageInput'), attachmentPreview=document.getElementById('attachmentPreview'), modelSelect=document.getElementById('modelSelect');
    const history=[]; const selectedImages=[];
    let selectedModel={SETTINGS['HERMES_MODEL']!r};
    const MAX_IMAGE_UPLOAD_BYTES=900000, MAX_IMAGE_DIMENSION=1280;
    async function loadModels(){{ try{{ const r=await fetch('/api/models'); const j=await r.json(); const models=j.models&&j.models.length?j.models:[j.current_model||selectedModel]; selectedModel=j.current_model||models[0]||selectedModel; modelSelect.innerHTML=''; for(const model of models){{ const option=document.createElement('option'); option.value=model; option.textContent=model; if(model===selectedModel) option.selected=true; modelSelect.appendChild(option); }} modelSelect.disabled=false; }}catch(e){{ modelSelect.innerHTML=''; const option=document.createElement('option'); option.value=selectedModel; option.textContent=selectedModel+' (默认)'; option.selected=true; modelSelect.appendChild(option); modelSelect.disabled=false; console.warn('model load failed', e); }} }}
    modelSelect.addEventListener('change',()=>{{ selectedModel=modelSelect.value||selectedModel; }});
    function avatarFor(role){{ return role==='user'?'你':(role==='assistant'?'H':'i'); }}
    function textFromContent(content){{ if(typeof content==='string') return content; if(Array.isArray(content)) return content.filter(p=>p.type==='text').map(p=>p.text||'').join('\\n'); return ''; }}
    function renderContent(container, content){{ container.innerHTML=''; if(typeof content==='string'){{ const mediaMatch=content.match(/^([\\s\\S]*?)\\n*MEDIA:(\\/[^\\r\\n]+)\\s*$/m); if(mediaMatch){{ const text=mediaMatch[1].trim(); if(text){{ const div=document.createElement('div'); div.className='message-text'; div.textContent=text; container.appendChild(div); }} const div=document.createElement('div'); div.className='message-text'; div.textContent='图片需要刷新历史后显示：'+mediaMatch[2]; container.appendChild(div); return; }} container.textContent=content; return; }} if(Array.isArray(content)){{ for(const part of content){{ if(part.type==='text' && part.text){{ const div=document.createElement('div'); div.className='message-text'; div.textContent=part.text; container.appendChild(div); }} if(part.type==='image_url' && part.image_url && part.image_url.url){{ const img=document.createElement('img'); img.className='message-image'; img.src=part.image_url.url; img.alt='图片'; container.appendChild(img); }} }} return; }} container.textContent=String(content||''); }}
    function add(role, content){{ const wrap=document.createElement('div'); wrap.className='msg '+role; const inner=document.createElement('div'); inner.className='msg-inner'; const avatar=document.createElement('div'); avatar.className='avatar'; avatar.textContent=avatarFor(role); const b=document.createElement('div'); b.className='bubble'; renderContent(b, content); inner.appendChild(avatar); inner.appendChild(b); wrap.appendChild(inner); chat.appendChild(wrap); chat.scrollTop=chat.scrollHeight; return b; }}
    function renderHistory(items){{ chat.innerHTML='<div class="msg sys"><div class="msg-inner"><div class="avatar">H</div><div class="bubble">历史消息已加载。你可以继续上次的对话。</div></div></div>'; history.length=0; for(const item of items){{ history.push(item); add(item.role, item.content); }} }}
    async function loadHistory(){{ try{{ const r=await fetch('/api/history'); const j=await r.json(); if(r.ok) renderHistory(j.messages||[]); }}catch(e){{ console.warn('history load failed', e); }} }}
    async function clearHistory(){{ if(!confirm('清空当前历史消息？')) return; await fetch('/api/history/clear', {{method:'POST'}}); history.length=0; renderHistory([]); msg.focus(); }}
    async function health(){{ try{{ let r=await fetch('/api/health'); let j=await r.json(); statusEl.innerHTML='<span class="dot ok"></span><span>'+j.status+'</span>'; }}catch(e){{ statusEl.innerHTML='<span class="dot"></span><span>offline</span>'; }} }}
    function resizeComposer(){{ msg.style.height='auto'; msg.style.height=Math.min(msg.scrollHeight,180)+'px'; }}
    function updateAttachmentPreview(){{ attachmentPreview.innerHTML=''; selectedImages.forEach((item,index)=>{{ const thumb=document.createElement('div'); thumb.className='attachment-thumb'; const img=document.createElement('img'); img.src=item.url; img.alt=item.name||'image'; const remove=document.createElement('button'); remove.type='button'; remove.className='attachment-remove'; remove.textContent='×'; remove.onclick=()=>{{ selectedImages.splice(index,1); updateAttachmentPreview(); }}; thumb.appendChild(img); thumb.appendChild(remove); attachmentPreview.appendChild(thumb); }}); }}
    function compressImageFile(file){{ return new Promise((resolve,reject)=>{{ const reader=new FileReader(); reader.onload=()=>{{ const original=String(reader.result); if(original.length<=MAX_IMAGE_UPLOAD_BYTES) return resolve({{name:file.name,url:original}}); const image=new Image(); image.onload=()=>{{ let w=image.width,h=image.height,scale=Math.min(1,MAX_IMAGE_DIMENSION/Math.max(w,h)); w=Math.max(1,Math.round(w*scale)); h=Math.max(1,Math.round(h*scale)); const canvas=document.createElement('canvas'); canvas.width=w; canvas.height=h; const ctx=canvas.getContext('2d'); ctx.drawImage(image,0,0,w,h); let quality=.82, url=canvas.toDataURL('image/jpeg',quality); while(url.length>MAX_IMAGE_UPLOAD_BYTES && quality>.45){{ quality-=.12; url=canvas.toDataURL('image/jpeg',quality); }} resolve({{name:file.name,url}}); }}; image.onerror=()=>resolve({{name:file.name,url:original}}); image.src=original; }}; reader.onerror=reject; reader.readAsDataURL(file); }}); }}
    function readImageFile(file){{ return compressImageFile(file); }}
    imageInput.addEventListener('change', async e=>{{ const files=Array.from(e.target.files||[]).filter(f=>f.type.startsWith('image/')); for(const file of files) selectedImages.push(await readImageFile(file)); imageInput.value=''; updateAttachmentPreview(); msg.focus(); }});
    msg.addEventListener('input', resizeComposer);
    msg.addEventListener('keydown', e=>{{ if(e.key==='Enter' && !e.shiftKey){{ e.preventDefault(); form.requestSubmit(); }} }});
    loadModels(); loadHistory(); health(); setInterval(health,15000); resizeComposer();
    form.addEventListener('submit', async e=>{{
      e.preventDefault(); const text=msg.value.trim(); if((!text && selectedImages.length===0) || btn.disabled) return; const parts=[]; if(text) parts.push({{type:'text', text}}); for(const image of selectedImages) parts.push({{type:'image_url', image_url:{{url:image.url}}}}); const userContent=parts.length===1 && parts[0].type==='text' ? text : parts; msg.value=''; selectedImages.length=0; updateAttachmentPreview(); resizeComposer(); add('user', userContent); const b=add('assistant','思考中...'); btn.disabled=true;
      try{{
        history.push({{role:'user', content:userContent}});
        const r=await fetch('/api/chat', {{method:'POST', headers:{{'Content-Type':'application/json'}}, body:JSON.stringify({{messages:history, model: selectedModel}})}});
        const j=await r.json(); if(!r.ok) throw new Error(j.error||r.statusText);
        renderContent(b, j.content||''); history.push({{role:'assistant', content:j.content||''}});
      }}catch(err){{ b.textContent='错误：'+err.message; }} finally{{ btn.disabled=false; msg.focus(); }}
    }});
    </script>
    """
    return page_shell(body)



@app.get('/activity', response_class=HTMLResponse)
async def activity_page(request: Request) -> str:
    if not is_logged_in(request):
        return RedirectResponse('/login', status_code=302)
    body = """
    <div class='activity-page'>
      <header class='activity-hero'>
        <div class='activity-hero-actions'>
          <a class='back-link' href='/'>← 返回聊天</a>
          <span class='pill'><span class='dot ok'></span><span>每 5 秒刷新</span></span>
        </div>
        <h1>Hermes 过程查看</h1>
        <p class='muted'>把原来的纯列表升级成过程时间线：先看摘要，再按用户输入、助手输出和工具调用筛选，必要时展开、复制每一步详情。</p>
      </header>
      <section class='activity-dashboard'>
        <div class='activity-commandbar'>
          <div id='activityFilters' class='activity-filters' aria-label='活动类型筛选'>
            <button class='activity-filter active' type='button' data-filter='all'>全部</button>
            <button class='activity-filter' type='button' data-filter='user'>用户输入</button>
            <button class='activity-filter' type='button' data-filter='assistant'>助手输出</button>
            <button class='activity-filter' type='button' data-filter='tool'>工具调用</button>
          </div>
          <label class='activity-refresh'><input id='activityRefreshToggle' type='checkbox' checked> 自动刷新</label>
        </div>
        <aside class='activity-panel'>
          <div class='activity-metrics'>
            <div class='activity-metric'><span id='activityMetricTotal' class='activity-metric-value'>0</span><span class='activity-metric-label'>当前显示步骤</span></div>
            <div class='activity-metric'><span id='activityMetricTools' class='activity-metric-value'>0</span><span class='activity-metric-label'>工具调用</span></div>
            <div class='activity-metric'><span id='activityMetricSessions' class='activity-metric-value'>0</span><span class='activity-metric-label'>最近会话</span></div>
            <div class='activity-metric'><span id='activityMetricLatest' class='activity-metric-value'>--</span><span class='activity-metric-label'>最新活动</span></div>
          </div>
          <h2>最近会话</h2>
          <div id='sessionList' class='activity-session-list muted'>加载中...</div>
        </aside>
        <main class='activity-main'>
          <section class='activity-panel activity-summary-card'>
            <h2>当前状态</h2>
            <p id='activitySummary' class='muted'>正在读取 Hermes 状态库...</p>
            <div class='activity-plan' aria-label='过程可视化改进方案'>
              <div class='activity-plan-step'><b>1</b><span>顶部展示总步骤、工具数、会话数和最新时间，先回答“现在做到哪了”。</span></div>
              <div class='activity-plan-step'><b>2</b><span>中间用可筛选时间线表达输入、输出、工具调用，避免原来大段文本堆叠。</span></div>
              <div class='activity-plan-step'><b>3</b><span>每条记录支持展开和复制，长日志默认收起，查看细节时不打断整体节奏。</span></div>
            </div>
          </section>
          <section class='activity-panel'>
            <h2>输入 / 输出 / 工具调用时间线</h2>
            <div id='activityTimeline' class='activity-timeline'>加载中...</div>
            <div id='activityList' class='activity-list' hidden></div>
          </section>
        </main>
      </section>
    </div>
    <script>
    const sessionList=document.getElementById('sessionList'), activityTimeline=document.getElementById('activityTimeline'), activityList=document.getElementById('activityList'), activitySummary=document.getElementById('activitySummary'), activityFilters=document.getElementById('activityFilters'), activityRefreshToggle=document.getElementById('activityRefreshToggle'), activityMetricTotal=document.getElementById('activityMetricTotal'), activityMetricTools=document.getElementById('activityMetricTools'), activityMetricSessions=document.getElementById('activityMetricSessions'), activityMetricLatest=document.getElementById('activityMetricLatest');
    let activityState={sessions:[],messages:[],filter:'all'};
    function escapeHtml(text){ return String(text||'').replace(/[&<>\"]/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;'}[c])); }
    function roleType(item){ return item.tool_name ? 'tool' : (item.role||'record'); }
    function roleLabel(item){ if(item.tool_name) return '工具：'+item.tool_name; if(item.role==='user') return '用户输入'; if(item.role==='assistant') return '助手输出'; return item.role||'记录'; }
    function roleIcon(item){ const type=roleType(item); if(type==='user') return '你'; if(type==='assistant') return 'H'; if(type==='tool') return '⌘'; return '•'; }
    function summarizeActivity(messages){ const total=messages.length, tools=messages.filter(m=>roleType(m)==='tool').length, latest=messages.length ? (messages[messages.length-1].time||'--') : '--'; return {total,tools,latest}; }
    function filteredMessages(){ return activityState.filter==='all' ? activityState.messages : activityState.messages.filter(item=>roleType(item)===activityState.filter); }
    function renderSessions(sessions){ sessionList.innerHTML=sessions.length ? sessions.map(s=>`<div class='activity-session ${s.active?'active':''}'><strong>${escapeHtml(s.source||'session')}</strong><span>${escapeHtml(s.started_at_text||'')}</span><span>${s.active?'进行中':'已结束'} · ${s.message_count||0} 条 · 工具 ${s.tool_call_count||0}</span></div>`).join('') : '<p class="muted">暂无会话。</p>'; }
    function renderActivityTimeline(){ const messages=filteredMessages(); const summary=summarizeActivity(messages); activityMetricTotal.textContent=summary.total; activityMetricTools.textContent=summary.tools; activityMetricSessions.textContent=activityState.sessions.length; activityMetricLatest.textContent=summary.latest ? summary.latest.slice(11,16)||summary.latest : '--'; activitySummary.textContent=activityState.messages.length ? `共读取 ${activityState.messages.length} 条记录，当前筛选显示 ${messages.length} 条，最新：${summary.latest}。` : '还没有活动记录。'; activityTimeline.innerHTML=messages.length ? messages.map((item,index)=>`<article class='activity-item ${escapeHtml(roleType(item))}'><div class='activity-icon'>${escapeHtml(roleIcon(item))}</div><div><div class='activity-meta'><span class='activity-badge'>${escapeHtml(roleLabel(item))}</span><span class='activity-badge'>${escapeHtml(item.time)}</span><span class='activity-badge'>${escapeHtml(item.source)}</span></div><pre class='activity-content'>${escapeHtml(item.content)}</pre><div class='activity-actions'><button class='activity-mini-button' type='button' onclick='toggleActivityItem(this)'>展开/收起</button><button class='activity-mini-button' type='button' onclick='copyActivityContent(${index})'>复制内容</button></div></div></article>`).join('') : '<div class="activity-empty">暂无匹配活动。可切换筛选或等待自动刷新。</div>'; activityList.textContent=messages.map(m=>`${roleLabel(m)} ${m.time}\n${m.content}`).join('\n\n'); }
    function toggleActivityItem(button){ const item=button.closest('.activity-item'); if(item) item.classList.toggle('expanded'); }
    async function copyActivityContent(index){ const item=filteredMessages()[index]; if(!item) return; try{ await navigator.clipboard.writeText(item.content||''); }catch(e){ console.warn('copy failed', e); } }
    activityFilters.addEventListener('click', e=>{ const button=e.target.closest('button[data-filter]'); if(!button) return; activityState.filter=button.dataset.filter; activityFilters.querySelectorAll('.activity-filter').forEach(btn=>btn.classList.toggle('active', btn===button)); renderActivityTimeline(); });
    async function loadActivity(){
      if(!activityRefreshToggle.checked && activityState.messages.length) return;
      try{
        const r=await fetch('/api/activity'); const j=await r.json(); if(!r.ok) throw new Error(j.error||r.statusText);
        activityState.sessions=j.sessions||[]; activityState.messages=j.messages||[];
        renderSessions(activityState.sessions); renderActivityTimeline();
      }catch(e){ activitySummary.textContent='读取失败：'+e.message; }
    }
    loadActivity(); setInterval(loadActivity,5000);
    </script>
    """
    return page_shell(body)


@app.get('/settings', response_class=HTMLResponse)
async def settings_page(request: Request) -> str:
    if not is_logged_in(request):
        return RedirectResponse('/login', status_code=302)
    settings_rows = [
        ('WebUI 版本', APP_VERSION),
        ('登录保护', '已开启' if auth_enabled() else '未开启'),
        ('Hermes API', SETTINGS['HERMES_API_BASE']),
        ('模型', SETTINGS['HERMES_MODEL']),
        ('API Key', mask_secret(SETTINGS['HERMES_API_KEY'])),
        ('历史消息上限', str(MAX_HISTORY_MESSAGES)),
    ]
    rows_html = ''.join(
        f"<div class='settings-row'><span class='settings-key'>{key}</span><span class='settings-value'>{value}</span></div>"
        for key, value in settings_rows
    )
    changelog_items = []
    for release in CHANGELOG:
        changes = ''.join(f"<li>{change}</li>" for change in release['changes'])
        changelog_items.append(
            f"""
            <article class='release-card'>
              <div class='release-head'>
                <h2>{release['version']}</h2>
                <span class='muted small'>更新时间：{release['updated_at']}</span>
              </div>
              <ul>{changes}</ul>
            </article>
            """
        )
    body = f"""
    <div class='settings-page'>
      <header class='settings-hero'>
        <a class='back-link' href='/'>← 返回聊天</a>
        <h1>设置</h1>
        <p class='muted'>查看 Hermes WebUI 当前状态、连接配置摘要和版本更新日志。</p>
      </header>
      <section class='settings-grid'>
        <article class='settings-card'>
          <h2>基础信息</h2>
          {rows_html}
        </article>
        <article class='settings-card'>
          <h2>说明</h2>
          <p class='muted'>这里先放只读设置，避免误改 API Key、密码或服务配置。后续可以继续扩展成可编辑配置页。</p>
          <p class='muted small'>结构化接口：<code>/api/settings</code></p>
        </article>
      </section>
      <section class='settings-changelog'>
        <h2>版本更新日志</h2>
        <div class='release-list'>{''.join(changelog_items)}</div>
      </section>
    </div>
    """
    return page_shell(body)


@app.get('/api/settings')
async def api_settings(request: Request) -> JSONResponse:
    if not is_logged_in(request):
        return JSONResponse({'error': 'unauthorized'}, status_code=401)
    return JSONResponse({
        'version': APP_VERSION,
        'auth_enabled': auth_enabled(),
        'api_base': SETTINGS['HERMES_API_BASE'],
        'model': SETTINGS['HERMES_MODEL'],
        'models_endpoint': '/api/models',
        'activity_endpoint': '/api/activity',
        'api_key': mask_secret(SETTINGS['HERMES_API_KEY']),
        'max_history_messages': MAX_HISTORY_MESSAGES,
        'changelog': CHANGELOG,
    })


@app.get('/changelog', response_class=HTMLResponse)
async def changelog_page(request: Request) -> str:
    if not is_logged_in(request):
        return RedirectResponse('/login', status_code=302)
    items = []
    for release in CHANGELOG:
        changes = ''.join(f"<li>{change}</li>" for change in release['changes'])
        items.append(
            f"""
            <article class='release-card'>
              <div class='release-head'>
                <h2>{release['version']}</h2>
                <span class='muted small'>更新时间：{release['updated_at']}</span>
              </div>
              <h3>更新内容</h3>
              <ul>{changes}</ul>
            </article>
            """
        )
    body = f"""
    <div class='changelog-page'>
      <header class='changelog-hero'>
        <a class='back-link' href='/'>← 返回聊天</a>
        <h1>版本更新日志</h1>
        <p class='muted'>每次发布都会记录版本、更新时间和更新内容。当前版本：{APP_VERSION}</p>
        <p class='muted small'>结构化接口：<code>/api/changelog</code></p>
      </header>
      <section class='release-list'>{''.join(items)}</section>
    </div>
    """
    return page_shell(body)


@app.get('/api/changelog')
async def api_changelog(request: Request) -> JSONResponse:
    if not is_logged_in(request):
        return JSONResponse({'error': 'unauthorized'}, status_code=401)
    return JSONResponse({'current_version': APP_VERSION, 'versions': CHANGELOG})

@app.get('/login', response_class=HTMLResponse)
async def login_page(request: Request) -> str:
    error = request.query_params.get('error')
    body = """
    <section class='card login'>
      <div class='brand'>Hermes WebUI</div>
      <p class='muted'>请输入 WebUI 密码。</p>
      {error_html}
      <form method='post' action='/login'>
        <input name='password' type='password' placeholder='Password' autocomplete='current-password' autofocus required>
        <div style='height:12px'></div>
        <button style='width:100%;height:48px' type='submit'>登录</button>
      </form>
    </section>
    """.format(error_html="<p class='error'>密码不正确</p>" if error else '')
    return page_shell(body)


@app.post('/login')
async def login(request: Request, password: str = Form(...)) -> RedirectResponse:
    if verify_password(password, SETTINGS['WEBUI_PASSWORD_HASH']):
        request.session['logged_in'] = True
        return RedirectResponse('/', status_code=302)
    return RedirectResponse('/login?error=1', status_code=302)


@app.get('/logout')
async def logout(request: Request) -> RedirectResponse:
    request.session.clear()
    return RedirectResponse('/login' if auth_enabled() else '/', status_code=302)


@app.get('/api/models')
async def api_models(request: Request) -> JSONResponse:
    if not is_logged_in(request):
        return JSONResponse({'error': 'unauthorized'}, status_code=401)
    headers = {'Authorization': f"Bearer {SETTINGS['HERMES_API_KEY']}"} if SETTINGS['HERMES_API_KEY'] else {}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(f"{SETTINGS['HERMES_API_BASE'].rstrip()}/v1/models", headers=headers)
        if r.status_code >= 400:
            return JSONResponse({'current_model': SETTINGS['HERMES_MODEL'], 'models': [SETTINGS['HERMES_MODEL']], 'error': r.text}, status_code=200)
        models = parse_models_response(r.json())
        return JSONResponse({'current_model': SETTINGS['HERMES_MODEL'], 'models': models})
    except Exception as exc:
        return JSONResponse({'current_model': SETTINGS['HERMES_MODEL'], 'models': [SETTINGS['HERMES_MODEL']], 'error': str(exc)}, status_code=200)


@app.get('/api/activity')
async def api_activity(request: Request, limit: int = MAX_ACTIVITY_MESSAGES) -> JSONResponse:
    if not is_logged_in(request):
        return JSONResponse({'error': 'unauthorized'}, status_code=401)
    return JSONResponse(read_activity(limit))


@app.get('/api/health')
async def api_health(request: Request) -> JSONResponse:
    if not is_logged_in(request):
        return JSONResponse({'error': 'unauthorized'}, status_code=401)
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.get(f"{SETTINGS['HERMES_API_BASE'].rstrip('/')}/health")
        return JSONResponse({'status': 'ok' if r.status_code == 200 else f'api {r.status_code}'})
    except Exception as exc:
        return JSONResponse({'status': 'offline', 'error': str(exc)}, status_code=503)


@app.get('/api/history')
async def api_history(request: Request) -> JSONResponse:
    if not is_logged_in(request):
        return JSONResponse({'error': 'unauthorized'}, status_code=401)
    return JSONResponse({'messages': read_history()})


@app.post('/api/history/clear')
async def api_history_clear(request: Request) -> JSONResponse:
    if not is_logged_in(request):
        return JSONResponse({'error': 'unauthorized'}, status_code=401)
    write_history([])
    return JSONResponse({'messages': []})


@app.get('/api/media/{token:path}')
async def api_media(request: Request, token: str):
    if not is_logged_in(request):
        return JSONResponse({'error': 'unauthorized'}, status_code=401)
    path = path_from_media_token(token)
    if path is None or not path.exists() or not path.is_file() or not is_allowed_media_path(path):
        return JSONResponse({'error': 'media not found'}, status_code=404)
    mime = image_mime_type(path)
    if mime is None:
        return JSONResponse({'error': 'unsupported media type'}, status_code=415)
    return FileResponse(path, media_type=mime)


@app.post('/api/chat')
async def api_chat(request: Request) -> JSONResponse:
    if not is_logged_in(request):
        return JSONResponse({'error': 'unauthorized'}, status_code=401)
    payload = await request.json()
    messages: list[dict[str, Any]] = payload.get('messages') or []
    if not messages:
        return JSONResponse({'error': 'messages required'}, status_code=400)
    headers = {'Authorization': f"Bearer {SETTINGS['HERMES_API_KEY']}"} if SETTINGS['HERMES_API_KEY'] else {}
    selected_model = normalize_model_id(payload.get('model'))
    body = {'model': selected_model, 'messages': prepare_messages_for_api(messages), 'stream': False}
    if not body['messages']:
        return JSONResponse({'error': 'no valid messages after normalization'}, status_code=400)
    try:
        async with httpx.AsyncClient(timeout=600) as client:
            r = await client.post(f"{SETTINGS['HERMES_API_BASE'].rstrip('/')}/v1/chat/completions", json=body, headers=headers)
        if r.status_code >= 400:
            return JSONResponse({'error': r.text}, status_code=502)
        data = r.json()
        raw_content = data.get('choices', [{}])[0].get('message', {}).get('content', '')
        content = normalize_assistant_content(raw_content) or ''
        append_history(messages[-1], {'role': 'assistant', 'content': content})
        record_activity_message('user', messages[-1].get('content', ''))
        record_activity_message('assistant', content)
        return JSONResponse({'content': content, 'raw': data})
    except Exception as exc:
        return JSONResponse({'error': str(exc)}, status_code=500)
