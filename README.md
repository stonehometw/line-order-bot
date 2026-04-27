# LINE 訂單 Bot 部署指南

## 📁 檔案清單
- `app.py` — 主程式
- `requirements.txt` — 套件清單
- `render.yaml` — Render 部署設定

---

## 🚀 部署步驟

### 1. 上傳到 GitHub
1. 到 https://github.com 建立新 repository（可設為 Private）
2. 把這三個檔案上傳進去

### 2. 部署到 Render
1. 到 https://render.com 免費註冊
2. 點「New +」→「Web Service」→ 連結你的 GitHub repo
3. 設定以下環境變數（Environment Variables）：

| 變數名稱 | 說明 | 取得方式 |
|---------|------|---------|
| `LINE_CHANNEL_ACCESS_TOKEN` | LINE Bot Token | LINE Developers Console → Messaging API → Channel access token |
| `LINE_CHANNEL_SECRET` | LINE Bot Secret | LINE Developers Console → Basic settings → Channel secret |
| `ANTHROPIC_API_KEY` | Claude API Key | https://console.anthropic.com |
| `GOOGLE_CREDENTIALS` | Google 服務帳號 JSON | 見下方說明 |
| `SHEET_NAME` | Google 試算表名稱 | 你自己建立的試算表名稱，例如「訂單彙整」|

4. 點「Deploy」，等待完成
5. 記下你的網址，例如：`https://line-order-bot.onrender.com`

### 3. 取得 Google 憑證
1. 到 https://console.cloud.google.com 建立新專案
2. 啟用「Google Sheets API」和「Google Drive API」
3. 前往「憑證」→「建立憑證」→「服務帳號」
4. 建立完成後，點服務帳號 →「金鑰」→「新增金鑰」→「JSON」
5. 下載 JSON 檔，把**整個檔案內容**貼到 `GOOGLE_CREDENTIALS` 環境變數
6. 到你的 Google 試算表，點「共用」，把服務帳號的 email 加為**編輯者**
   （email 格式類似：`xxx@your-project.iam.gserviceaccount.com`）

### 4. 設定 LINE Webhook
1. 到 LINE Developers Console → 你的 Channel → Messaging API
2. Webhook URL 填入：`https://line-order-bot.onrender.com/callback`
3. 點「Verify」確認出現 ✅
4. 打開「Use webhook」開關
5. 打開「Allow bot to join group chats」

### 5. 把 Bot 加入群組
1. 在 Messaging API 頁面找到 Bot 的 QR Code
2. 掃描加為 LINE 好友
3. 在群組中邀請 Bot 加入

---

## ✅ 試算表欄位格式

Bot 會自動建立標題列並寫入以下欄位：

| 時間 | 店家 | 品項 | 數量 | 單位 | 傳送者 |
|------|------|------|------|------|-------|
| 2026-04-27 08:26 | 中華 | 高麗菜 | 50 | 斤 | 宋岵泳 |
| 2026-04-27 08:26 | 中華 | 洋蔥 | 4 | 斤 | 宋岵泳 |
| 2026-04-27 09:56 | 草蓆 | 高麗菜 | 2 | 件 | 斑鳩後昌瑞瑞 |

---

## 🔍 確認 Bot 正常運作

訂單傳入後，群組會看到：
```
✅ 已記錄 6 筆
📦 中華：高麗菜 50斤、洋蔥 4斤、巴西里 4兩、牛蕃茄 3顆、檸檬 0.5斤
```

---

## ❓ 常見問題

**Q: Webhook Verify 失敗？**
A: 確認 Render 已部署完成，且 URL 末尾有 `/callback`

**Q: 試算表沒有寫入？**
A: 確認服務帳號 email 已被加為試算表的編輯者

**Q: Bot 沒有回應？**
A: 確認 `Use webhook` 已開啟，且 `Auto-reply messages` 已關閉
