import os
import json
import base64
import logging
import re
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, ImageMessage
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

claude_client = anthropic.Anthropic(
    api_key=os.environ['ANTHROPIC_API_KEY'],
    http_client=httpx.Client()
)

TEXT_PROMPT = """你是一個蔬果訂單解析助手。

判斷以下 LINE 訊息是否為蔬果/食材訂單。
訂單特徵：第一行是店家名稱，後面每行是「品項＋數量＋單位」，最後一行通常是「共X樣 謝謝」。

如果是訂單，只回傳此 JSON（不含任何其他文字或 markdown）：
{"is_order": true, "store": "店家名稱", "items": [{"name": "品項名稱", "qty": "數量", "unit": "單位"}]}

數量和單位請分開，例如「50斤」→ qty:"50", unit:"斤"；「2件」→ qty:"2", unit:"件"；「半斤」→ qty:"0.5", unit:"斤"；「4兩」→ qty:"4", unit:"兩"；「10盒」→ qty:"10", unit:"盒"

忽略「共X樣 謝謝」這類結尾語。
如果不是訂單，只回傳：{"is_order": false}

訊息：
{text}"""

IMAGE_PROMPT = """你是一個蔬果訂單 OCR 助手。

請仔細辨識這張圖片中的訂單內容。
圖片可能是：手寫訂單、截圖訊息、表格、或手寫紙張。

如果圖片包含訂單，只回傳此 JSON（不含任何其他文字或 markdown）：
{"is_order": true, "store": "店家名稱", "items": [{"name": "品項名稱", "qty": "數量", "unit": "單位"}]}

數量和單位請分開，例如「50斤」→ qty:"50", unit:"斤"；「2件」→ qty:"2", unit:"件"；「半斤」→ qty:"0.5", unit:"斤"

如果圖片不含訂單（例如一般照片、風景），只回傳：{"is_order": false}"""


def extract_json(text: str) -> dict:
    """從 Claude 回傳的文字中安全地提取 JSON"""
    # 移除 markdown code block
    text = text.strip()
    text = re.sub(r'```json\s*', '', text)
    text = re.sub(r'```\s*', '', text)
    text = text.strip()
    # 找到第一個 { 到最後一個 }
    start = text.find('{')
    end = text.rfind('}')
    if start != -1 and end != -1:
        text = text[start:end+1]
    return json.loads(text)


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
    expected = ['時間', '店家', '品項', '數量', '單位', '傳送者', '來源']
    if first_row != expected:
        ws.insert_row(expected, 1)


def parse_text_order(text: str) -> dict:
    response = claude_client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=800,
        messages=[{"role": "user", "content": TEXT_PROMPT.format(text=text)}]
    )
    raw = response.content[0].text
    logger.info(f"Claude 文字回傳: {raw[:200]}")
    return extract_json(raw)


def parse_image_order(image_bytes: bytes, mime_type: str = "image/jpeg") -> dict:
    image_b64 = base64.standard_b64encode(image_bytes).decode("utf-8")
    response = claude_client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=800,
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": mime_type,
                        "data": image_b64,
                    },
                },
                {"type": "text", "text": IMAGE_PROMPT}
            ],
        }]
    )
    raw = response.content[0].text
    logger.info(f"Claude 圖片回傳: {raw[:200]}")
    return extract_json(raw)


def get_display_name(user_id: str) -> str:
    try:
        profile = line_bot_api.get_profile(user_id)
        return profile.display_name
    except Exception:
        return user_id


def write_to_sheets(data: dict, sender_name: str, source: str = "文字") -> int:
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
            sender_name,
            source
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
def handle_text(event):
    text = event.message.text.strip()
    user_id = event.source.user_id
    sender_name = get_display_name(user_id)
    logger.info(f"[文字] from {sender_name}: {text[:50]}")
    try:
        result = parse_text_order(text)
    except Exception as e:
        logger.error(f"文字解析失敗: {e}")
        return
    if not result.get('is_order'):
        return
    try:
        count = write_to_sheets(result, sender_name, source="文字")
        logger.info(f"文字寫入完成：{result['store']} {count} 筆")
    except Exception as e:
        logger.error(f"寫入 Sheets 失敗: {e}")


@handler.add(MessageEvent, message=ImageMessage)
def handle_image(event):
    user_id = event.source.user_id
    sender_name = get_display_name(user_id)
    message_id = event.message.id
    logger.info(f"[圖片] from {sender_name}, message_id={message_id}")
    try:
        message_content = line_bot_api.get_message_content(message_id)
        image_bytes = b''.join(chunk for chunk in message_content.iter_content())
    except Exception as e:
        logger.error(f"圖片下載失敗: {e}")
        return
    try:
        result = parse_image_order(image_bytes)
    except Exception as e:
        logger.error(f"圖片 OCR 解析失敗: {e}")
        return
    if not result.get('is_order'):
        logger.info("圖片不含訂單，忽略")
        return
    try:
        count = write_to_sheets(result, sender_name, source="圖片")
        logger.info(f"圖片寫入完成：{result['store']} {count} 筆")
    except Exception as e:
        logger.error(f"寫入 Sheets 失敗: {e}")


@app.route('/health', methods=['GET'])
def health():
    return {'status': 'ok', 'time': datetime.now().isoformat()}


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port)
