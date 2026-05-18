import os
import json
import base64
import logging
import re
import threading
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, ImageMessage
import anthropic
import httpx
from google import genai
from google.genai import types
import gspread
from google.oauth2.service_account import Credentials
from google.cloud import documentai
from google.api_core.client_options import ClientOptions
from datetime import datetime

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

line_bot_api = LineBotApi(os.environ['LINE_CHANNEL_ACCESS_TOKEN'])
handler = WebhookHandler(os.environ['LINE_CHANNEL_SECRET'])

# Claude 客戶端
claude_client = anthropic.Anthropic(
    api_key=os.environ['ANTHROPIC_API_KEY'],
    http_client=httpx.Client()
)

# Gemini 客戶端
gemini_client = genai.Client(api_key=os.environ['GEMINI_API_KEY'])
GEMINI_MODEL = 'gemini-2.5-pro'

TEXT_PROMPT = """你是一個蔬果訂單解析助手。

判斷以下 LINE 訊息是否為蔬果/食材訂單。
訂單特徵：第一行是店家名稱，後面每行是「品項＋數量＋單位」，最後一行通常是「共X樣 謝謝」。

如果是訂單，只回傳此 JSON（不含任何其他文字或 markdown）：
{"is_order": true, "store": "店家名稱", "items": [{"name": "品項名稱", "qty": "數量", "unit": "單位"}]}

數量和單位請分開，例如「50斤」→ qty:"50", unit:"斤"；「2件」→ qty:"2", unit:"件"；「半斤」→ qty:"0.5", unit:"斤"
忽略「共X樣 謝謝」這類結尾語。
如果不是訂單，只回傳：{"is_order": false}

訊息：
"""

IMAGE_PROMPT = """你是一個蔬果訂單 OCR 助手。請仔細辨識這張圖片中的訂單內容。
圖片可能是：手寫訂單、截圖訊息、表格、或手寫紙張。

如果圖片包含訂單，只回傳此 JSON（不含任何其他文字或 markdown）：
{"is_order": true, "store": "店家名稱", "items": [{"name": "品項名稱", "qty": "數量", "unit": "單位"}]}

數量和單位請分開，例如「50斤」→ qty:"50", unit:"斤"；「2件」→ qty:"2", unit:"件"
如果圖片不含訂單，只回傳：{"is_order": false}"""

TABLE_IMAGE_PROMPT = """你是一個 OCR 助手。請仔細辨識這張圖片中的表格內容。
請將圖片中的表格內容完整擷取下來，並以 JSON 二維陣列 (List of Lists) 的格式回傳。
每一列 (Row) 是一個陣列，包含該列的所有欄位文字。
請「只」回傳這個 JSON 陣列，不要包含任何其他文字、Markdown 符號或 ```json 標籤。
例如：
[
  ["品名", "數量", "備註"],
  ["高麗菜", "50斤", ""],
  ["洋蔥", "2件", "大"]
]
如果圖片沒有內容，請回傳 []。"""


def extract_json(text: str) -> dict:
    text = text.strip()
    text = re.sub(r'```json\s*', '', text)
    text = re.sub(r'```\s*', '', text)
    text = text.strip()
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


def ensure_header(ws, model='Claude'):
    first_row = ws.row_values(1)
    expected = ['時間', '店家', '品項', '數量', '單位', '傳送者', '來源', '模型']
    if first_row != expected:
        ws.insert_row(expected, 1)


def get_or_create_sheet(sh, name):
    try:
        return sh.worksheet(name)
    except Exception:
        return sh.add_worksheet(title=name, rows=1000, cols=10)


def write_to_sheets(data: dict, sender_name: str, source: str, model: str) -> int:
    gc = get_sheets_client()
    sh = gc.open(os.environ['SHEET_NAME'])
    ws = get_or_create_sheet(sh, model)
    ensure_header(ws, model)

    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    rows = [[now, data['store'], item['name'], item['qty'], item['unit'], sender_name, source, model]
            for item in data['items']]
    if rows:
        ws.append_rows(rows, value_input_option='USER_ENTERED')
    return len(rows)

# ── Claude 解析 ──────────────────────────────────────────────
def parse_with_claude_text(text: str) -> dict:
    response = claude_client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=2000,
        messages=[{"role": "user", "content": TEXT_PROMPT + text}]
    )
    raw = response.content[0].text
    logger.info(f"Claude 文字回傳: {raw[:100]}")
    return extract_json(raw)


def parse_with_claude_image(image_bytes: bytes) -> dict:
    image_b64 = base64.standard_b64encode(image_bytes).decode("utf-8")
    response = claude_client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=2000,
        messages=[{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": image_b64}},
            {"type": "text", "text": IMAGE_PROMPT}
        ]}]
    )
    raw = response.content[0].text
    logger.info(f"Claude 圖片回傳: {raw[:100]}")
    return extract_json(raw)

# ── Gemini 解析 ──────────────────────────────────────────────
def parse_with_gemini_text(text: str) -> dict:
    response = gemini_client.models.generate_content(
        model=GEMINI_MODEL,
        contents=TEXT_PROMPT + text
    )
    raw = response.text
    logger.info(f"Gemini 文字回傳: {raw[:100]}")
    return extract_json(raw)


def parse_with_gemini_image(image_bytes: bytes) -> dict:
    response = gemini_client.models.generate_content(
        model=GEMINI_MODEL,
        contents=[
            types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg"),
            IMAGE_PROMPT
        ]
    )
    raw = response.text
    logger.info(f"Gemini 圖片回傳: {raw[:100]}")
    return extract_json(raw)


def parse_with_gemini_table(image_bytes: bytes) -> list[list[str]]:
    response = gemini_client.models.generate_content(
        model=GEMINI_MODEL,
        contents=[
            types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg"),
            TABLE_IMAGE_PROMPT
        ]
    )
    raw = response.text
    logger.info(f"Gemini 表格回傳: {raw[:100]}")
    text = raw.strip()
    text = re.sub(r'```json\s*', '', text)
    text = re.sub(r'```\s*', '', text)
    try:
        table_rows = json.loads(text.strip())
        if isinstance(table_rows, list):
            return table_rows
    except Exception as e:
        logger.error(f"Gemini 表格 JSON 解析失敗: {e}")
    return []

def process_gemini_table_flow(image_bytes, write_fn_args):
    try:
        table_rows = parse_with_gemini_table(image_bytes)
        if not table_rows:
            logger.info("Gemini: 未擷取到表格，忽略")
            return
        sender_name, source = write_fn_args
        count = write_table_to_sheets(table_rows, sender_name, source, "Gemini")
        logger.info(f"Gemini 表格寫入完成：{count} 列")
    except Exception as e:
        logger.error(f"Gemini 表格流程失敗: {e}")

def get_display_name(user_id: str) -> str:
    try:
        return line_bot_api.get_profile(user_id).display_name
    except Exception:
        return user_id

# ── Document AI 解析 ──────────────────────────────────────────
def parse_with_docai_image(image_bytes: bytes) -> list[list[str]]:
    project_id = os.environ.get('DOCAI_PROJECT_ID')
    location = os.environ.get('DOCAI_LOCATION', 'us')
    processor_id = os.environ.get('DOCAI_PROCESSOR_ID')
    if not project_id or not processor_id:
        logger.error("Document AI 環境變數未設定 (DOCAI_PROJECT_ID, DOCAI_PROCESSOR_ID)")
        return []

    opts = ClientOptions(api_endpoint=f"{location}-documentai.googleapis.com")
    client = documentai.DocumentProcessorServiceClient(client_options=opts)
    name = client.processor_path(project_id, location, processor_id)
    
    raw_document = documentai.RawDocument(content=image_bytes, mime_type="image/jpeg")
    request = documentai.ProcessRequest(name=name, raw_document=raw_document)
    
    try:
        result = client.process_document(request=request)
    except Exception as e:
        logger.error(f"Document AI API 失敗: {e}")
        return []

    document = result.document
    table_rows = []
    
    def layout_to_text(layout, text):
        # 提取文字並將內部的換行替換為空白，以防破壞表格結構
        val = "".join([text[segment.start_index:segment.end_index] for segment in layout.text_anchor.text_segments]).strip()
        return val.replace('\n', ' ')
    
    for page in document.pages:
        for table in page.tables:
            for row in table.header_rows:
                row_data = []
                for cell in row.cells:
                    cell_text = layout_to_text(cell.layout, document.text)
                    row_data.append(cell_text)
                table_rows.append(row_data)
            for row in table.body_rows:
                row_data = []
                for cell in row.cells:
                    cell_text = layout_to_text(cell.layout, document.text)
                    row_data.append(cell_text)
                table_rows.append(row_data)
                
    return table_rows


def write_table_to_sheets(table_rows: list[list[str]], sender_name: str, source: str, sheet_name: str = "Document AI") -> int:
    if not table_rows:
        return 0
        
    gc = get_sheets_client()
    sh = gc.open(os.environ['SHEET_NAME'])
    ws = get_or_create_sheet(sh, sheet_name)
    
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    
    # 準備寫入的資料
    # 第一列：時間、傳送者、來源
    write_data = [[f"[{now}] 來自 {sender_name} ({source}) 的表格"]]
    
    # 將擷取的表格附加到後面
    write_data.extend(table_rows)
    # 加一個空行作分隔
    write_data.append([])
    
    ws.append_rows(write_data, value_input_option='USER_ENTERED')
    return len(table_rows)

def process_with_docai(image_bytes, write_fn_args):
    try:
        table_rows = parse_with_docai_image(image_bytes)
        if not table_rows:
            logger.info("Document AI: 未擷取到表格，忽略")
            return
        sender_name, source = write_fn_args
        count = write_table_to_sheets(table_rows, sender_name, source)
        logger.info(f"Document AI 寫入完成：{count} 列")
    except Exception as e:
        logger.error(f"Document AI 失敗: {e}")

# ── 並行處理兩個 AI ──────────────────────────────────────────
def process_with_model(parse_fn, write_fn_args, model_name):
    try:
        result = parse_fn()
        if not result.get('is_order'):
            logger.info(f"{model_name}: 非訂單，忽略")
            return
        count = write_to_sheets(result, *write_fn_args, model_name)
        logger.info(f"{model_name} 寫入完成：{result['store']} {count} 筆")
    except Exception as e:
        logger.error(f"{model_name} 失敗: {e}")


@app.route('/callback', methods=['POST'])
def callback():
    signature = request.headers.get('X-Line-Signature', '')
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return 'OK'


@handler.add(MessageEvent, message=TextMessage)
def handle_text(event):
    text = event.message.text.strip()
    sender = get_display_name(event.source.user_id)
    logger.info(f"[文字] from {sender}: {text[:50]}")

    # 同時啟動兩個執行緒 (暫停 Claude)
    # t1 = threading.Thread(target=process_with_model,
    #     args=(lambda: parse_with_claude_text(text), (sender, "文字"), "Claude"))
    t2 = threading.Thread(target=process_with_model,
        args=(lambda: parse_with_gemini_text(text), (sender, "文字"), "Gemini"))
    # t1.start()
    t2.start()


@handler.add(MessageEvent, message=ImageMessage)
def handle_image(event):
    sender = get_display_name(event.source.user_id)
    logger.info(f"[圖片] from {sender}")
    try:
        content = line_bot_api.get_message_content(event.message.id)
        image_bytes = b''.join(chunk for chunk in content.iter_content())
    except Exception as e:
        logger.error(f"圖片下載失敗: {e}")
        return

    # 同時啟動兩個執行緒 (暫停 Claude)
    # t1 = threading.Thread(target=process_with_model,
    #     args=(lambda: parse_with_claude_image(image_bytes), (sender, "圖片"), "Claude"))
    t2 = threading.Thread(target=process_gemini_table_flow, args=(image_bytes, (sender, "圖片")))
    t3 = threading.Thread(target=process_with_docai, args=(image_bytes, (sender, "圖片")))
    # t1.start()
    t2.start()
    t3.start()


@app.route('/health', methods=['GET'])
def health():
    return {'status': 'ok', 'time': datetime.now().isoformat()}


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 8080)))
