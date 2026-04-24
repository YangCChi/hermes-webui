#!/usr/bin/env python3
"""Small Web UI for the local Hermes API server."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.middleware.sessions import SessionMiddleware

BASE_DIR = Path('/opt/hermes-webui')
ENV_FILE = BASE_DIR / '.env'
HERMES_ENV_FILE = Path('/root/.hermes/.env')
DEFAULT_API_BASE = 'http://127.0.0.1:8642'


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

SETTINGS = load_settings()
app = FastAPI(title='Hermes WebUI')
app.add_middleware(SessionMiddleware, secret_key=SETTINGS['WEBUI_SESSION_SECRET'], same_site='lax')

CSS = """
:root{color-scheme:dark;--bg:#0b1020;--panel:#111a2e;--soft:#1a2742;--text:#e7edf8;--muted:#95a3b8;--accent:#8b5cf6;--good:#22c55e;--bad:#ef4444}*{box-sizing:border-box}body{margin:0;background:radial-gradient(circle at top left,#1b2550,#0b1020 45%);font-family:ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;color:var(--text)}main{max-width:980px;margin:0 auto;padding:28px}.card{background:rgba(17,26,46,.88);border:1px solid rgba(255,255,255,.08);border-radius:22px;box-shadow:0 24px 80px rgba(0,0,0,.35);padding:24px}.top{display:flex;align-items:center;justify-content:space-between;gap:16px}.brand{font-size:26px;font-weight:800;letter-spacing:-.04em}.muted{color:var(--muted)}.pill{display:inline-flex;gap:8px;align-items:center;background:var(--soft);border-radius:999px;padding:8px 12px;color:var(--muted);font-size:13px}.dot{width:9px;height:9px;border-radius:99px;background:var(--bad)}.dot.ok{background:var(--good)}#chat{height:58vh;overflow:auto;padding:18px;background:rgba(0,0,0,.16);border-radius:16px;border:1px solid rgba(255,255,255,.06);margin:18px 0}.msg{margin:0 0 14px;display:flex}.bubble{max-width:82%;white-space:pre-wrap;line-height:1.52;padding:13px 15px;border-radius:16px}.user{justify-content:flex-end}.user .bubble{background:linear-gradient(135deg,#7c3aed,#2563eb)}.assistant .bubble{background:#18233b}.sys .bubble{background:transparent;color:var(--muted);border:1px dashed rgba(255,255,255,.12)}form.row{display:flex;gap:10px}textarea,input{width:100%;border:1px solid rgba(255,255,255,.1);border-radius:14px;background:#0d1527;color:var(--text);padding:13px 14px;font:inherit}textarea{height:86px;resize:vertical}button{border:0;border-radius:14px;background:linear-gradient(135deg,#8b5cf6,#2563eb);color:white;font-weight:700;padding:0 20px;cursor:pointer}button.secondary{background:#24314f}.login{max-width:430px;margin:12vh auto}.small{font-size:13px}.error{color:#fca5a5}.toolbar{display:flex;gap:8px;align-items:center}a{color:#c4b5fd;text-decoration:none}
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
    <section class='card'>
      <div class='top'>
        <div><div class='brand'>Hermes WebUI</div><div class='muted small'>和服务器上的 Hermes Agent 直接对话</div></div>
        <div class='toolbar'><span id='status' class='pill'><span class='dot'></span><span>checking</span></span></div>
      </div>
      <div class='muted small' style='margin-top:12px'>API: {SETTINGS['HERMES_API_BASE']} · Model: {SETTINGS['HERMES_MODEL']} · Key: {mask_secret(SETTINGS['HERMES_API_KEY'])}</div>
      <div id='chat'><div class='msg sys'><div class='bubble'>已连接 WebUI。你可以在这里向 Hermes 发消息；Hermes 仍然能使用服务器工具。</div></div></div>
      <form id='sendForm' class='row'>
        <textarea id='message' placeholder='输入你的问题，例如：检查服务器磁盘空间' required autofocus></textarea>
        <button id='sendBtn' type='submit'>发送</button>
      </form>
    </section>
    <script>
    const chat=document.getElementById('chat'), form=document.getElementById('sendForm'), msg=document.getElementById('message'), btn=document.getElementById('sendBtn'), statusEl=document.getElementById('status');
    const history=[];
    function add(role, text){{ const wrap=document.createElement('div'); wrap.className='msg '+role; const b=document.createElement('div'); b.className='bubble'; b.textContent=text; wrap.appendChild(b); chat.appendChild(wrap); chat.scrollTop=chat.scrollHeight; return b; }}
    async function health(){{ try{{ let r=await fetch('/api/health'); let j=await r.json(); statusEl.innerHTML='<span class="dot ok"></span><span>'+j.status+'</span>'; }}catch(e){{ statusEl.innerHTML='<span class="dot"></span><span>offline</span>'; }} }}
    health(); setInterval(health,15000);
    form.addEventListener('submit', async e=>{{
      e.preventDefault(); const text=msg.value.trim(); if(!text) return; msg.value=''; add('user', text); const b=add('assistant','思考中...'); btn.disabled=true;
      try{{
        history.push({{role:'user', content:text}});
        const r=await fetch('/api/chat', {{method:'POST', headers:{{'Content-Type':'application/json'}}, body:JSON.stringify({{messages:history}})}});
        const j=await r.json(); if(!r.ok) throw new Error(j.error||r.statusText);
        b.textContent=j.content||''; history.push({{role:'assistant', content:j.content||''}});
      }}catch(err){{ b.textContent='错误：'+err.message; }} finally{{ btn.disabled=false; msg.focus(); }}
    }});
    </script>
    """
    return page_shell(body)


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
        content = data.get('choices', [{}])[0].get('message', {}).get('content', '')
        return JSONResponse({'content': content, 'raw': data})
    except Exception as exc:
        return JSONResponse({'error': str(exc)}, status_code=500)
