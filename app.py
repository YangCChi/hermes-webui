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
MAX_HISTORY_MESSAGES = 200
DEFAULT_API_BASE = 'http://127.0.0.1:8642'

APP_VERSION = 'v0.0.3'
MEDIA_TOKEN_PREFIX = 'local:'
MEDIA_REFERENCE_RE = re.compile(r'(?m)^MEDIA:(?P<path>/[^\r\n]+)\s*$')
ALLOWED_MEDIA_DIRS = (Path('/tmp'), Path('/var/tmp'), BASE_DIR / 'media')
ALLOWED_IMAGE_MIME_TYPES = {'image/png', 'image/jpeg', 'image/gif', 'image/webp', 'image/svg+xml'}
CHANGELOG = [
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

SETTINGS = load_settings()
app = FastAPI(title='Hermes WebUI')
app.add_middleware(SessionMiddleware, secret_key=SETTINGS['WEBUI_SESSION_SECRET'], same_site='lax')

CSS = """
:root{color-scheme:light;--bg:#f7f7f8;--sidebar:#202123;--sidebar-soft:#2a2b32;--surface:#ffffff;--surface-alt:#f7f7f8;--border:#e5e5e5;--text:#202123;--muted:#6e6e80;--assistant:#f7f7f8;--user:#ffffff;--accent:#10a37f;--accent-dark:#0d8f6f;--danger:#ef4444;--good:#10a37f}*{box-sizing:border-box}html,body{height:100%}body{margin:0;background:var(--bg);font-family:ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;color:var(--text)}main{height:100%;padding:0}.chatgpt-shell{height:100vh;display:grid;grid-template-columns:260px minmax(0,1fr);background:var(--bg)}.sidebar{background:var(--sidebar);color:#ececf1;display:flex;flex-direction:column;padding:12px;gap:12px}.new-chat{height:44px;border:1px solid rgba(255,255,255,.22);border-radius:8px;background:transparent;color:#ececf1;display:flex;align-items:center;gap:10px;padding:0 12px;font-weight:500}.side-title{font-size:13px;color:#c5c5d2;padding:8px 4px}.side-link{color:#ececf1;border-radius:8px;padding:10px 12px;text-decoration:none;font-size:14px}.side-link:hover{background:var(--sidebar-soft)}.side-footer{margin-top:auto;color:#9ca3af;font-size:12px;line-height:1.5;padding:8px 4px}.main-chat{min-width:0;display:flex;flex-direction:column;height:100vh}.topbar{height:56px;border-bottom:1px solid var(--border);display:flex;align-items:center;justify-content:space-between;padding:0 20px;background:rgba(255,255,255,.8);backdrop-filter:blur(12px)}.brand{font-size:17px;font-weight:650}.muted{color:var(--muted)}.small{font-size:13px}.pill{display:inline-flex;gap:8px;align-items:center;border:1px solid var(--border);border-radius:999px;padding:7px 10px;color:var(--muted);font-size:13px;background:#fff}.dot{width:8px;height:8px;border-radius:99px;background:var(--danger)}.dot.ok{background:var(--good)}#chat{flex:1;overflow:auto;padding:0;background:var(--bg)}.msg{display:flex;border-bottom:1px solid rgba(0,0,0,.04)}.msg-inner{width:min(820px,100%);margin:0 auto;display:grid;grid-template-columns:38px minmax(0,1fr);gap:18px;padding:24px 20px}.avatar{width:32px;height:32px;border-radius:4px;display:flex;align-items:center;justify-content:center;font-weight:700;font-size:13px;color:white;flex:none}.user .avatar{background:#5436da}.assistant .avatar{background:var(--accent)}.sys .avatar{background:#8e8ea0}.bubble{white-space:pre-wrap;line-height:1.68;font-size:15.5px;overflow-wrap:anywhere}.assistant{background:var(--assistant)}.user,.sys{background:var(--user)}.sys .bubble{color:var(--muted)}.composer-wrap{border-top:1px solid var(--border);background:linear-gradient(180deg,rgba(247,247,248,0),var(--bg) 18%);padding:18px 18px 24px}.composer{width:min(820px,100%);margin:0 auto;position:relative;border:1px solid #d9d9e3;border-radius:14px;background:#fff;box-shadow:0 8px 28px rgba(0,0,0,.08);display:flex;align-items:flex-end;padding:10px 52px 10px 14px}.attach-button{width:34px;height:34px;border:0;border-radius:8px;background:transparent;color:var(--muted);font-size:22px;line-height:1;cursor:pointer;margin-right:8px}.attach-button:hover{background:var(--surface-alt);color:var(--text)}.composer textarea{width:100%;min-height:28px;max-height:180px;height:28px;resize:none;border:0;outline:0;background:transparent;color:var(--text);font:inherit;line-height:1.5;padding:2px 0}.send-button{position:absolute;right:10px;bottom:9px;width:34px;height:34px;border:0;border-radius:8px;background:var(--accent);color:white;font-weight:800;cursor:pointer}.send-button:disabled{background:#d9d9e3;cursor:not-allowed}.attachment-preview{width:min(820px,100%);margin:0 auto 10px;display:flex;gap:8px;flex-wrap:wrap}.attachment-thumb{position:relative;border:1px solid var(--border);border-radius:10px;background:#fff;padding:4px;box-shadow:0 4px 16px rgba(0,0,0,.06)}.attachment-thumb img{width:74px;height:74px;object-fit:cover;border-radius:7px;display:block}.attachment-remove{position:absolute;right:-7px;top:-7px;width:22px;height:22px;border:0;border-radius:99px;background:#202123;color:white;cursor:pointer}.message-image{display:block;max-width:min(420px,100%);max-height:360px;border-radius:12px;margin:8px 0;border:1px solid var(--border)}.message-text{white-space:pre-wrap}.hint{width:min(820px,100%);margin:8px auto 0;text-align:center;color:var(--muted);font-size:12px}.login{max-width:430px;margin:12vh auto;background:white;border:1px solid var(--border);border-radius:16px;box-shadow:0 16px 50px rgba(0,0,0,.08);padding:24px}input{width:100%;border:1px solid var(--border);border-radius:10px;background:#fff;color:var(--text);padding:13px 14px;font:inherit}button{font:inherit}.login button{border:0;border-radius:10px;background:var(--accent);color:white;font-weight:700;cursor:pointer}.error{color:#dc2626}a{color:#0d8f6f;text-decoration:none}.changelog-page{min-height:100vh;background:var(--bg);padding:42px 18px}.changelog-hero,.release-list{width:min(860px,100%);margin:0 auto}.changelog-hero{padding:24px 0}.changelog-hero h1{font-size:38px;letter-spacing:-.04em;margin:18px 0 8px}.back-link{display:inline-flex;color:#0d8f6f;text-decoration:none;margin-bottom:10px}.release-card{background:#fff;border:1px solid var(--border);border-radius:16px;padding:22px 24px;margin:0 0 16px;box-shadow:0 10px 30px rgba(0,0,0,.04)}.release-head{display:flex;justify-content:space-between;align-items:baseline;gap:12px;border-bottom:1px solid var(--border);padding-bottom:12px;margin-bottom:14px}.release-head h2{margin:0;font-size:24px}.release-card h3{font-size:15px;margin:8px 0;color:var(--text)}.release-card li{margin:7px 0;line-height:1.6}code{background:#ececf1;border-radius:6px;padding:2px 6px}@media(max-width:760px){.chatgpt-shell{grid-template-columns:1fr}.sidebar{display:none}.topbar{padding:0 14px}.msg-inner{grid-template-columns:32px minmax(0,1fr);gap:12px;padding:20px 14px}.composer-wrap{padding:14px 12px 18px}}
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
        <a class='side-link' href='/changelog'>版本更新日志</a>
        <div class='side-footer'>Version: {APP_VERSION}<br>ChatGPT-style interface<br>API: {SETTINGS['HERMES_API_BASE']}<br>Model: {SETTINGS['HERMES_MODEL']}<br>Key: {mask_secret(SETTINGS['HERMES_API_KEY'])}</div>
      </aside>
      <section class='main-chat'>
        <header class='topbar'>
          <div><div class='brand'>Hermes WebUI</div><div class='muted small'>模仿 ChatGPT 的简洁对话界面</div></div>
          <span id='status' class='pill'><span class='dot'></span><span>checking</span></span>
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
    const chat=document.getElementById('chat'), form=document.getElementById('sendForm'), msg=document.getElementById('message'), btn=document.getElementById('sendBtn'), statusEl=document.getElementById('status'), imageInput=document.getElementById('imageInput'), attachmentPreview=document.getElementById('attachmentPreview');
    const history=[]; const selectedImages=[];
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
    function readImageFile(file){{ return new Promise((resolve,reject)=>{{ const reader=new FileReader(); reader.onload=()=>resolve({{name:file.name,url:String(reader.result)}}); reader.onerror=reject; reader.readAsDataURL(file); }}); }}
    imageInput.addEventListener('change', async e=>{{ const files=Array.from(e.target.files||[]).filter(f=>f.type.startsWith('image/')); for(const file of files) selectedImages.push(await readImageFile(file)); imageInput.value=''; updateAttachmentPreview(); msg.focus(); }});
    msg.addEventListener('input', resizeComposer);
    msg.addEventListener('keydown', e=>{{ if(e.key==='Enter' && !e.shiftKey){{ e.preventDefault(); form.requestSubmit(); }} }});
    loadHistory(); health(); setInterval(health,15000); resizeComposer();
    form.addEventListener('submit', async e=>{{
      e.preventDefault(); const text=msg.value.trim(); if((!text && selectedImages.length===0) || btn.disabled) return; const parts=[]; if(text) parts.push({{type:'text', text}}); for(const image of selectedImages) parts.push({{type:'image_url', image_url:{{url:image.url}}}}); const userContent=parts.length===1 && parts[0].type==='text' ? text : parts; msg.value=''; selectedImages.length=0; updateAttachmentPreview(); resizeComposer(); add('user', userContent); const b=add('assistant','思考中...'); btn.disabled=true;
      try{{
        history.push({{role:'user', content:userContent}});
        const r=await fetch('/api/chat', {{method:'POST', headers:{{'Content-Type':'application/json'}}, body:JSON.stringify({{messages:history}})}});
        const j=await r.json(); if(!r.ok) throw new Error(j.error||r.statusText);
        renderContent(b, j.content||''); history.push({{role:'assistant', content:j.content||''}});
      }}catch(err){{ b.textContent='错误：'+err.message; }} finally{{ btn.disabled=false; msg.focus(); }}
    }});
    </script>
    """
    return page_shell(body)



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
    body = {'model': SETTINGS['HERMES_MODEL'], 'messages': messages, 'stream': False}
    try:
        async with httpx.AsyncClient(timeout=600) as client:
            r = await client.post(f"{SETTINGS['HERMES_API_BASE'].rstrip('/')}/v1/chat/completions", json=body, headers=headers)
        if r.status_code >= 400:
            return JSONResponse({'error': r.text}, status_code=502)
        data = r.json()
        raw_content = data.get('choices', [{}])[0].get('message', {}).get('content', '')
        content = normalize_assistant_content(raw_content) or ''
        append_history(messages[-1], {'role': 'assistant', 'content': content})
        return JSONResponse({'content': content, 'raw': data})
    except Exception as exc:
        return JSONResponse({'error': str(exc)}, status_code=500)
