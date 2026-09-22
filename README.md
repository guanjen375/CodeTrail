# CodeTrail — 本地 Code-RAG / RAG / MCP 工作台

CodeTrail 是公開原始碼專案，包含本地 RAG、Code-RAG、21 個 MCP 工具，以及自己的
Python／Textual 全螢幕聊天客戶端。模型透過工具搜尋程式、閱讀規格、分析圖片與 firmware，
再以檔案行號或 REF 回答。使用者入口是終端指令 `aicode`，推理服務使用
[llama.cpp](https://github.com/ggml-org/llama.cpp) 的 `llama-server` 與本地 GGUF。
部署不需要 Node / npm。

安裝後的日常路線只有四步：

```text
./set_config.sh → ~/start.sh → 在要分析的專案執行 aicode → ~/start.sh stop
```

本機是預設部署方式；模型主機與工作主機分開時，請看
[進階部署](developer.md#advanced-deployment)。模型端點、使用者授權和檔案落點決定資料邊界；
處理 NDA 資料前請讀[安全邊界](developer.md#security)與 [Responsible Use](RESPONSIBLE_USE.md)。

- [安裝依賴](#install) → [準備模型](#models) → [設定](#configure) → [啟動與停止](#start)
- [聊天、快捷鍵、附件與 RAG 詳細操作](docs/usage.md)
- [完整 MCP 工具契約](docs/mcp-tools.md)
- [進階配置、維護、評測與故障排解](developer.md)

<a id="install"></a>

## 安裝依賴

以下命令以 Ubuntu／Debian 為例。`<CODETRAIL_REPO>` 是這個 CodeTrail checkout；
`<PROJECT_TO_ANALYZE>` 是之後要分析的專案，兩者不必相同。

```bash
sudo apt update
sudo apt install -y git curl wget build-essential cmake pkg-config \
  python3 python3-venv python3-pip ripgrep tmux
cd <CODETRAIL_REPO>
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -U pip
python3 -m pip install -r requirements.txt
python3 -m pip install "pymupdf4llm==1.28.0"   # 需要 PDF 入庫時安裝
mkdir -p "$HOME/.local/bin"
ln -sfn "$PWD/aicode" "$HOME/.local/bin/aicode"
export PATH="$HOME/.local/bin:$PATH"
command -v aicode
```

把 `export PATH="$HOME/.local/bin:$PATH"` 加到自己的 shell 啟動檔，讓重新登入後也能找到
`aicode`。依賴只裝在 venv 時，每次設定和使用前都先執行
`source <CODETRAIL_REPO>/.venv/bin/activate`。重建 venv 或更換 Python 後重新設定。
其他安裝方式與完整依賴見 [開發與維運](developer.md#dependencies)。

有 NVIDIA GPU 時，先確認 driver、CUDA Toolkit 與 GPU 架構相容：

```bash
nvidia-smi
nvcc --version
```

Blackwell 需要支援它的 Toolkit（最低 12.8）；安裝與 CMake 錯誤處理見
[CUDA 與 llama.cpp](developer.md#cuda-build)。模型載入量由 RAM／VRAM 與 GGUF 決定，
設定精靈的容量提示不是能成功啟動的保證。

Build llama.cpp：

```bash
cd ~
git clone https://github.com/ggml-org/llama.cpp ~/llama.cpp
cd ~/llama.cpp
cmake -B build -DGGML_CUDA=ON -DLLAMA_CURL=OFF
cmake --build build --config Release -j
~/llama.cpp/build/bin/llama-server --version
```

預設使用 `~/llama.cpp/build/bin/llama-server`；放在其他位置可在設定精靈指定。
CodeTrail 會檢查必要的 server 能力，包含 chat token 計數、reranking 與多模態能力；
缺少時會報錯並要求更新 build。

<a id="models"></a>

## 準備四類 GGUF 模型

模型預設放 `~/models`，也可在設定精靈改目錄。主聊天模型以 `<CODE_MODEL>` 表示，
請自行準備支援工具呼叫且硬體能負擔的 GGUF；本專案不指定唯一主模型。
多 shard 模型必須下載完整，精靈會以第一片為入口並檢查缺片。

| 角色 | 預設 port | 用途／內建模型 key |
|---|---:|---|
| main | 8080 | 聊天與工具呼叫；`<CODE_MODEL>` |
| embedding | 8081 | 文件與程式向量；`bge-m3` |
| reranker | 8082 | 檢索排序；`bge-reranker-v2-m3` |
| VL | 8083 | 圖片理解；`qwen3.5-9b`，需配對 mmproj |

四個服務都是啟動必要條件，reranker 不可用時不會改用較差排序。
以下是附屬模型的下載範例；下載需連網，部署後可使用本地檔案運作：

```bash
python3 -m pip install -U huggingface_hub
hf download CompendiumLabs/bge-m3-gguf bge-m3-f16.gguf \
  --local-dir ~/models/bge-m3
hf download gpustack/bge-reranker-v2-m3-GGUF bge-reranker-v2-m3-Q8_0.gguf \
  --local-dir ~/models/bge-reranker-v2-m3
hf download unsloth/Qwen3.5-9B-GGUF Qwen3.5-9B-Q6_K.gguf mmproj-F16.gguf \
  --local-dir ~/models/qwen3.5-9b
```

VL 的模型與 mmproj 放在同一個目錄；也可以選其他相容 GGUF。
`analyze_file` 看圖一次，`ingest_document` 則會自動用 VL 讀圖後加入可反覆查詢的知識庫。

<a id="configure"></a>

## 設定本機部署

在 CodeTrail checkout 執行無參數精靈：

```bash
cd <CODETRAIL_REPO>
source .venv/bin/activate
./set_config.sh
```

直接進入本機設定，依序選主聊天、embedding、reranker、VL 的模型與 GPU，再選必要容量
參數。單一候選可自動選用；多個候選由使用者決定。主模型問 n_ctx；MoE 模型另問
留在 RAM 的 expert 層數；reranker 的 internal buffer 與主 n_ctx 分開設定。
四個服務固定單 slot，主聊天與內部工作共用模型鎖。
本機精靈保留既有壓縮模式；尚無 client.json 時不因設定模型而建立它。
需要調整壓縮模式，見[壓縮設定](docs/compaction-rules.md)。

DSpark 在主模型設定中一起選，預設 off。相同主模型的既有 draft 配對可以明示沿用，
換主模型不能帶入舊配對。啟用時指定配對的本地 draft GGUF 與 1–64 的 token 上限；
開啟不代表必然加速，啟動還會驗證 `/slots` 的實際狀態。詳細選項見
[DSpark](developer.md#dspark)。

最後只確認一次完整摘要，Enter 寫入、`q` 取消且不寫檔。若目前設定為 A／B 分離模式，
精靈會先明示轉成本機與移除遠端端點授權；取消保留原設定。

| 產物 | 用途 |
|---|---|
| `~/.config/codetrail/deployment.json` | 模型、GPU、容量、服務與 llama-server 路徑；主模型在 `services.main.model`（簡稱 `main.model`） |
| `~/.config/codetrail/models.json` | `<CODE_MODEL>` 等 registry key 對應本地 GGUF |
| `~/.config/codetrail/client.json` | 僅在需清除先前端點授權等情況更新，保留其他合法使用者設定 |
| `~/start.sh` | 固定指向產生它的 checkout，顯示來源、產生時間與版本 |

設定採同一筆交易保存並備份既有檔案。檔案在使用者 home，不放進公開 repo；
還原、endpoint manifest、A／B 設定及獨立 DSpark 調整集中在
`scripts/configure-advanced.sh`，見[進階部署](developer.md#advanced-deployment)。

壓縮的 `codetrail`／`manual` 仍是實驗功能；前者在回答完成或接續歷史後檢查是否摘要，
後者只在 `/compact` 執行。`off` 不壓縮，context 不足時明確報錯。
規則與代價見[壓縮規則](docs/compaction-rules.md)。

<a id="start"></a>

## 啟動、使用、停止

```bash
~/start.sh
cd <PROJECT_TO_ANALYZE>
aicode
```

四個服務通過 health 與必要能力檢查後才算 ready。服務預設只綁 `127.0.0.1`。
大模型載入時會顯示等待進度；失敗會回滾本次啟動，原因保存在
`~/.local/state/codetrail/logs/<role>.log`。查看主模型：

```bash
tail -n 120 ~/.local/state/codetrail/logs/main.log
nvidia-smi
tmux attach -t codetrail-main
```

`aicode` 必須從具體專案根目錄啟動，不能從 `/` 或 `$HOME` 啟動。該目錄就是沙箱，
換專案需到新目錄另開客戶端。啟動後對話區空白，`/status` 查看健康檢查與警告，
`/tools` 查看工具。第一次直接送出問題或 `/new` 才建立對話，`/session` 接續歷史。

```text
先不要改檔。請用 list_dir 看兩層目錄，再用 read_file 讀 README.md，
用 file:line 說明入口與主要模組，分開證據與推測。
```

要看到真正的工具呼叫卡、結果及模型回答；模型只印 XML 或聲稱「已讀取」不算執行。
滑鼠拖選文字後按預設 **F2** 複製；`/copykey f3` 更換並保存，`/copykey reset` 回 F2。
頁尾常駐目前複製鍵；回合進行中 **Ctrl+C** 仍中斷。SSH／tmux 的剪貼簿排查見
[開發與維運](developer.md#clipboard-ssh-tmux)。

離開 TUI 可用 `/exit` 或閒置時 Ctrl+D，再停止服務：

```bash
~/start.sh stop
```

`set_config.sh`、`aicode` 與附加 shell 入口都不接受參數；`~/start.sh` 只接受無參數啟動
或單一 `stop`。更換模型、GPU、n_ctx 或 Python 後重跑設定並重啟。

<a id="rag"></a>

## 附件、VL 與知識注入

檔案已在專案內時，在對話指定相對路徑：

```text
請用 read_file 讀 logs/build.log，找出第一個失敗。
請用 analyze_file 看 screenshots/error.png，列出畫面上的錯誤文字。
請用 analyze_file 分析 build/firmware.elf，view=symbols、target=uart。
```

讀取結果進對話，不會自動加入可反覆檢索的知識庫。要讓規格、PDF、圖片或 firmware 之後反覆查詢，使用入庫：

```text
請用 ingest_document 匯入 docs/spec.pdf，完成後 reload_knowledge_base，回報 chunks。
請用 query_knowledge_strict 查 reset assert 最小時間，附 REF，證據不足就拒答。
```

圖片直接交給 `ingest_document`，它會自行使用 VL，不必先 `analyze_file`。
聊天截圖加 `mode="chat"`。圖多的 PDF 先用 `preflight_only=True` 估成本，零寫入；
入庫後看 `review_figures` 的狀態與原因。單張抽壞只讓那一張缺席，其餘有效內容可以入庫；
VL 服務、來源身分或文件級契約失敗則整份中止。未驗證圖面不能供 strict 回答數值。

外部檔案先在 `client.json` 開啟 `external_import`，再請模型用 `import_external_file`
匯入並核准來源／目的；之後使用回傳的 `.aicode_uploads/...` 路徑。
詳細白名單、表格查值、OCR 覆核、續跑與移除文件見[使用指南](docs/usage.md#attachments)。
`knowledge.json`、上傳副本、review artifacts 與 session 可能含 NDA 內容，不要 commit。

## 工具與人工核准

| 用途 | 工具 |
|---|---|
| 程式探索 | `list_dir`、`read_file`、`grep_code`、`code_rag_search`、`file_info` |
| 知識查詢 | `query_knowledge`、`query_knowledge_strict`、`query_table` |
| 修改與驗證 | `git_status`、`git_diff`、`apply_patch`、`run_lint`、`run_command` |
| 附件與知識庫 | `analyze_file`、`ingest_document`、`remove_document`、`reload_knowledge_base`、`review_figures`、`review_text`、`import_external_file` |
| 行為規則 | `record_lesson` |

八個工具每次需人工核准：`apply_patch`、`run_lint`、`run_command`、`remove_document`、
`record_lesson`、`review_figures`、`review_text`、`import_external_file`。
核准框完整顯示參數；Esc 拒絕該工具，Ctrl+C 中斷整輪。

`apply_patch` 接受 SEARCH/REPLACE、unified diff。最多 5 個檔案、單檔 200 行（udiff 算 added+removed；S/R 算 payload budget = SEARCH+REPLACE 行數）。
套用後只做唯讀 syntax check，lint／test 必須分別呼叫並核准。
`run_command`：timeout 只接受整數 1..600 秒（server 端上限；client 可能更早截止）。
每次 MCP 呼叫固定 read timeout 為 **660 秒**；到期會取消工具，寬限期過則重啟該 MCP instance。

工具文字結果以 `status: ok|partial|error` 開頭。`partial` 與 `next:` 表示需續讀或修復；
省略 `max_chars` 時依 live `n_ctx` 的 12% 配置，明示較大的值會標 `context_risk`。
完整上限、唯讀政策與逐工具說明見[MCP 工具契約](docs/mcp-tools.md)。

## 授權與使用責任

原始碼依 [LICENSE](LICENSE) 提供。模型、GGUF、第三方依賴與被分析資料各自受其授權約束。
請閱讀 [Responsible Use](RESPONSIBLE_USE.md) 與 [Disclaimer](DISCLAIMER.md)，
在分享輸出或部署到自己的環境前核對來源與存取權限。
