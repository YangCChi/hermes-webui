# Hermes WebUI

A small browser-based WebUI for chatting with a local Hermes Agent API server.

This project was built for a self-hosted Hermes Agent running on a Linux server. It proxies browser requests to the local OpenAI-compatible Hermes API server, so the API key stays on the server instead of being exposed in the frontend JavaScript.

## Features

- Simple single-page chat interface
- Talks to Hermes through the OpenAI-compatible `/v1/chat/completions` endpoint
- Health indicator for the local Hermes API server
- Persistent chat history across browser refreshes
- Optional password login
- Systemd-friendly deployment
- No frontend build step required

## Security note

By default this example disables WebUI password login because it was designed to be used behind a trusted network or a cloud security-group IP allowlist.

If you expose it to the public internet, you should at least do one of the following:

1. Restrict the port to your own IP in your cloud firewall/security group.
2. Put it behind Tailscale, WireGuard, or another private network.
3. Enable password login with `WEBUI_AUTH_ENABLED=true`.
4. Put it behind HTTPS and a real reverse proxy.

Do not commit `.env`, API keys, passwords, or session secrets.

## Requirements

- Python 3.10+
- A running Hermes Agent API server, for example on `127.0.0.1:8642`

Install dependencies:

```bash
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
```

## Configuration

Copy the example environment file:

```bash
cp .env.example .env
```

Edit `.env`:

```bash
HERMES_API_BASE=http://127.0.0.1:8642
HERMES_MODEL=hermes-agent
HERMES_API_KEY=your-local-hermes-api-key
WEBUI_AUTH_ENABLED=false
WEBUI_SESSION_SECRET=replace-with-random-session-secret
```

The app also tries to read `/root/.hermes/.env` and use `API_SERVER_KEY` / `API_SERVER_MODEL_NAME` from there when local values are not provided.

## Run locally

```bash
./venv/bin/python -m uvicorn app:app --host 0.0.0.0 --port 8643
```

Open:

```text
http://SERVER_IP:8643/
```

## Optional password login

Password login is disabled by default. To enable it, generate a password hash:

```bash
./venv/bin/python - <<'PY'
from app import hash_password
print(hash_password('change-this-password'))
PY
```

Then set in `.env`:

```bash
WEBUI_AUTH_ENABLED=true
WEBUI_PASSWORD_HASH=your-generated-password-hash
WEBUI_SESSION_SECRET=replace-with-a-long-random-secret
```

Restart the service after changing `.env`.

## Chat history

The app saves recent chat messages to:

```text
/opt/hermes-webui/chat-history.json
```

The history file is intentionally ignored by git. Delete the file or call `POST /api/history/clear` if you want to clear the saved conversation.

## Example systemd service

Create `/etc/systemd/system/hermes-webui.service`:

```ini
[Unit]
Description=Hermes Agent WebUI
After=network.target hermes-gateway.service
Wants=hermes-gateway.service

[Service]
Type=simple
User=root
WorkingDirectory=/opt/hermes-webui
EnvironmentFile=/opt/hermes-webui/.env
ExecStart=/opt/hermes-webui/venv/bin/python -m uvicorn app:app --host 0.0.0.0 --port 8643
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
```

Enable and start:

```bash
systemctl daemon-reload
systemctl enable --now hermes-webui.service
systemctl status hermes-webui.service --no-pager
```

## Development checks

```bash
python3 -m py_compile app.py
```

## License

MIT
