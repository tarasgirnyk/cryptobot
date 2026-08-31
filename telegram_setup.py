"""Локально показує chat_id та user_id останніх повідомлень боту.

Токен читається лише з .env і ніколи не друкується.
"""

import json
import urllib.request
from pathlib import Path


def read_env(path):
    values = {}
    if not path.exists():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


env = read_env(Path(__file__).with_name(".env"))
token = env.get("TELEGRAM_BOT_TOKEN", "")
if not token:
    raise SystemExit("Додайте TELEGRAM_BOT_TOKEN у .env, потім надішліть боту /start.")

request = urllib.request.Request(
    f"https://api.telegram.org/bot{token}/getUpdates",
    headers={"Accept": "application/json"},
)
with urllib.request.urlopen(request, timeout=20) as response:
    payload = json.load(response)

found = []
for update in payload.get("result", []):
    message = update.get("message") or update.get("channel_post")
    if not message:
        continue
    found.append(
        {
            "chat_id": message.get("chat", {}).get("id"),
            "user_id": message.get("from", {}).get("id"),
            "chat_type": message.get("chat", {}).get("type"),
            "text": str(message.get("text", ""))[:60],
        }
    )

if not found:
    raise SystemExit("Повідомлень немає. Відкрийте свого бота, надішліть /start і повторіть.")

print(json.dumps(found[-10:], ensure_ascii=False, indent=2))
