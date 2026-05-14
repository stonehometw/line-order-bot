import os
import json
import logging
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage
import anthropic
import httpx
import gspread
from google.oauth2.service_account import Credentials
from datetime import datetime

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

line_bot_api = LineBotApi(os.environ['LINE_CHANNEL_ACCESS_TOKEN'])
handler = WebhookHandler(os.environ['LINE_CHANNEL_SECRET'])

# 修正 httpx 版本衝突問題
claude_client = anthropic.Anthropic(
    api_key=os.environ['ANTHROPIC_API_KEY'],
    http_client=httpx.Client()
)

PARSE_PROMPT = """你是一個蔬果訂單解析助手。

判斷以下 LINE 訊息是否為蔬果/食材訂單。
訂單特徵：第一行是店家名稱，後面每行是「品項＋數量＋單位」，最後一行通常是「共X樣 謝謝」。

如果是訂單，只回傳此 JSON（不含任何其他文字或 markdown）：
{"is_order": true, "store": "店家名稱", "items": [{"name": "品項名稱", "qty": "數量", "unit": "單位"}]}

數量和單位請分開，例如「50斤」→ qty: "50", unit: "斤"；「2件」→ qty: "2", unit: "件"；「半斤」→ qty: "0.5", unit: "斤"；「4兩」→ qty: "4", unit: "兩"；「10盒」→ qty: "10", unit: "盒"

忽略「共X樣 謝謝」這類結尾語。

如果不是訂單（例如一般聊天、圖片說明），只回傳：{"is_order": false}

訊息：
{text}"""


def get_sheets_client():
    creds_json = json.loads(os.environ['GOOGLE_CREDENTIALS'])
    creds = Credentials.from_service_account_info(
        creds_json,
        scopes=[
            'https://spreadsheets.google.com/feeds',
            'https://www.googleapis.com/auth/drive'
        ]
    )
    return gspread.authorize(creds)


def ensure_header(ws):
    first_row = ws.row_values(1)
    expected = ['時間', '店家', '品項', '數量', '單位', '傳送者']
    if first_row != expected:
        ws.insert_row(expected, 1)


def parse_order(text: str) -> dict:
    response = claude_client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=800,
        messages=[{
            "role": "user",
            "content": PARSE_PROMPT.format(text=text)
        }]
    )
    raw = response.content[0].text.strip()
    raw = raw.replace('```json', '').replace('```', '').strip()
    return json.loads(raw)


def get_display_name(user_id: str) -> str:
    try:
        profile = line_bot_api.get_profile(user_id)
        return profile.display_name
    except Exception:
        return user_id


def write_to_sheets(data: dict, sender_name: str) -> int:
    gc = get_sheets_client()
    sh = gc.open(os.environ['SHEET_NAME'])
    ws = sh.sheet1
    ensure_header(ws)

    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    rows = []
    for item in data['items']:
        rows.append([
            now,
            data['store'],
            item['name'],
            item['qty'],
            item['unit'],
            sender_name
        ])

    if rows:
        ws.append_rows(rows, value_input_option='USER_ENTERED')

    return len(rows)


@app.route('/callback', methods=['POST'])
def callback():
    signature = request.headers.get('X-Line-Signature', '')
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        logger.error("Invalid signature")
        abort(400)
    return 'OK'


@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    text = event.message.text.strip()
    user_id = event.source.user_id
    sender_name = get_display_name(user_id)

    logger.info(f"收到訊息 from {sender_name}: {text[:50]}")

    try:
        result = parse_order(text)
    except Exception as e:
        logger.error(f"Claude 解析失敗: {e}")
        return

    if not result.get('is_order'):
        return

    try:
        count = write_to_sheets(result, sender_name)
        logger.info(f"靜默寫入完成：{result['store']} {count} 筆")
    except Exception as e:
        logger.error(f"寫入 Sheets 失敗: {e}")
        return


@app.route('/health', methods=['GET'])
def health():
    return {'status': 'ok', 'time': datetime.now().isoformat()}


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port)
