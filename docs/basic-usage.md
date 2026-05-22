# 基本操作

這份文件整理 CodeTrail 的基本操作：環境驗收、正常對話、夾帶附件、注入 RAG。完整安裝步驟仍放在 [安裝、設定與啟動](setup.md)；工具細節放在 [MCP 工具清單](mcp-tools.md)。

[回到 README](../README.md)。

---

## 0. 環境驗收

照 [安裝、設定與啟動](setup.md) 完成後，先在 CodeTrail repo 裡跑：

```bash
AICODE_MODEL=<CODE_MODEL> python scripts/doctor.py
```

`<CODE_MODEL>` 是佔位符,必須替換成 MODEL_REGISTRY 裡登記的 bare name 或 GGUF 絕對路徑;如果你已經在 OpenCode JSON 設好同一顆模型,doctor 也能從設定檔解析。`FAIL` 要先處理;`WARN` 可以依訊息判斷是否需要調整。接著切到要分析的專案根目錄:

```bash
cd <PROJECT_TO_ANALYZE>
aicode
```

進入 TUI 後就可以開始下一節操作。需要讀專案外附件時看「夾帶附件」；若工具看起來沒接上，再用 `/status` 檢查是否有 `codetrail Connected`；如果 `/status` 沒連上，先看 [常見問題](troubleshooting.md)。

---

## 1. 正常對話

CodeTrail 的使用方式不是把整個 repo 貼進對話，而是讓模型透過 MCP 工具按需讀檔、搜尋、查 RAG。

第一次進一個陌生 repo，可以先問：

```text
先不要改檔。
請用工具 list_dir 看兩層目錄，找出主要 entry point、測試目錄和設定檔。
再用工具 grep_code 或 code_rag_search 找初始化流程。
最後用 file:line 列出「證據」和「推測」。
```

正常情況下，你會看到模型呼叫 `list_dir(...)`、`grep_code(...)`、`code_rag_search(...)`、`read_file(...)` 這類工具，再用檔名與行號回答。如果它沒有讀檔就直接回答，可以要求：

```text
請先用工具查證，不要只根據一般經驗回答。
```

常用說法：

| 需求 | 可以這樣問 |
|---|---|
| 看 repo 架構 | `請用 list_dir 看專案結構，找 entry point、測試和設定檔。` |
| 找錯誤訊息 | `請用 grep_code 搜尋 "panic: xxx"，再讀最可能的檔案。` |
| 看已知檔案 | `請用 file_info 看 src/main.c 大小，再用 read_file 讀前 120 行。` |
| 查已匯入 spec | `請用 query_knowledge 查 reset timing，回答要附 REF。` |
| 高風險規格數字 | `請用 query_knowledge_strict 查最大值，證據不足就拒答。` |

完整工具清單見 [MCP 工具清單](mcp-tools.md)。

---

## 2. 夾帶附件

附件有兩種情況：檔案已經在專案目錄內，或檔案還在專案外。

### 檔案在專案目錄內

把檔案放在 `AICODE_ROOT` 底下，例如 `logs/build_fail.txt`、`screenshots/error.png`、`firmware/boot.bin`。然後在對話裡明確要求使用工具：

```text
請用工具 read_file 讀 logs/build_fail.txt，找出最重要的錯誤訊息。
```

```text
請用工具 analyze_file 分析 screenshots/error.png，辨識畫面上的錯誤文字。
```

```text
請用工具 analyze_file 分析 firmware/boot.bin，整理檔頭、magic 和可讀字串。
```

`read_file(...)` 適合文字；`analyze_file(...)` 適合圖片、ELF、firmware binary。這些操作只把附件帶進目前對話，不會建立可長期查詢的知識庫。

### 檔案在專案目錄外

預設不能直接讀 `$HOME`、`Downloads` 或其他專案外路徑。要匯入外部附件，啟動時打開匯入功能：

```bash
AI_CODE_ALLOW_EXTERNAL_IMPORT=1 aicode
```

`AI_CODE_ALLOW_EXTERNAL_IMPORT=1` 是總開關。預設可匯入來源是 `~/Downloads` 和 `/tmp`。如果附件在其他目錄，用 `AI_CODE_IMPORT_ROOTS` 指定白名單；一旦設定就會取代預設清單：

```bash
AI_CODE_ALLOW_EXTERNAL_IMPORT=1 \
AI_CODE_IMPORT_ROOTS="$HOME/Downloads:/tmp:$HOME/specs" \
aicode
```

進入 TUI 後請模型先匯入，再分析回傳的新路徑：

```text
請用工具 import_external_file 匯入 ~/Downloads/error.log，
再用 read_file 讀回傳的新路徑，整理最重要的錯誤。
```

匯入後的檔案會複製到專案底下 `.aicode_uploads/`，原始檔不會被修改。更多副檔名、白名單與圖片/binary 細節見 [RAG、附件與知識庫操作](rag.md)。

---

## 3. 注入 RAG

如果要讓模型之後能反覆查 spec、datasheet、manual 或設計文件，不要只用 `read_file(...)` 看一次。改成匯入知識庫：

```text
請用工具 ingest_document 匯入 docs/npu_spec.pdf，
完成後用工具 reload_knowledge_base，
最後回報目前載入幾個 chunks。
```

成功時 chunks 會大於 0。接著查詢：

```text
請用工具 query_knowledge 查 conv2d 的輸入大小限制，
回答時每個數字都要附 REF。
```

對「最大值、預設值、timing、reset 時間」這類答錯會造成風險的題目，用嚴格模式：

```text
請用工具 query_knowledge_strict 查 reset assert 最小持續時間，
證據不足就拒答，不要用常識補。
```

基本判斷：

- `query_knowledge(...)` 適合一般查文件，速度較快。
- `query_knowledge_strict(...)` 適合規格數字與限制，較慢但會做證據檢查。
- 每次新增或移除文件後都要 `reload_knowledge_base(...)`。
- `knowledge.json` 會保存切碎後的文件內容，NDA 場景不要 commit。

完整流程、支援格式、圖片 OCR、binary/ELF 匯入和舊文件移除見 [RAG、附件與知識庫操作](rag.md)。

---

## 4. 最小驗收流程

剛裝好時，建議照順序跑一次：

```text
請用工具 list_dir 看專案兩層目錄，列出 entry point、測試目錄和設定檔。
```

```text
請用工具 read_file 讀 README.md 前 80 行，整理這個專案怎麼啟動。
```

```text
請用工具 import_external_file 匯入 ~/Downloads/error.log，
再用 read_file 讀回傳的新路徑，整理最重要的錯誤。
```

```text
請用工具 ingest_document 匯入 docs/spec.pdf，
完成後 reload_knowledge_base，
再用 query_knowledge 查一個 spec 問題，回答要附 REF。
```

前兩個驗證正常對話與專案讀檔；第三個驗證附件匯入；第四個驗證 RAG。若暫時沒有外部 log 或 spec，可以先建立小型 `.txt` 測試檔放在 `~/Downloads` 或專案 `docs/` 底下。

---

## 5. 要改檔時

先讓模型查證，再允許 patch：

```text
根據上面的 file:line 證據，請做最小修改。
套用 patch 前先說會改哪些檔案；套用後跑最小相關測試。
如果 run_command 被白名單拒絕，請列出你原本想跑的命令。
```

`apply_patch(...)` 會真的寫檔，`run_command(...)` 會執行白名單命令。只想分析時要明講「不要改檔」。安全邊界與副作用工具說明見 [安全邊界與工作節奏](security.md)。
