# CodeTrail — 本地 Code-RAG / RAG / MCP 工作台

CodeTrail 是公開原始碼專案，包含本地 RAG、Code-RAG、20 個 MCP 工具，以及自己的
Python／Textual 全螢幕聊天客戶端。模型透過工具搜尋程式、閱讀規格、分析圖片與 firmware，
再以檔案行號或 REF 回答；回答用到知識庫時，另一個小模型（審核模型）會逐條核對引用。使用者入口是終端指令 `aicode`，推理服務使用
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

預設使用 `~/llama.cpp/build/bin/llama-server`；已有設定時沿用保存的執行檔路徑。
其他位置可在 `./scripts/configure-advanced.sh` 選「local」後指定。
CodeTrail 會檢查必要的 server 能力，包含 chat token 計數、reranking 與多模態能力；
缺少時會報錯並要求更新 build。

<a id="models"></a>

## 準備模型：兩個聊天模型＋三個附屬模型

模型預設放 `~/models`；其他目錄可在 `./scripts/configure-advanced.sh` 選「local」後指定。
主聊天模型以 `<CODE_MODEL>` 表示，
請自行準備支援工具呼叫且硬體能負擔的 GGUF；本專案不指定唯一主模型。
多 shard 模型必須下載完整，精靈會以第一片為入口並檢查缺片。

| 角色 | 預設 port | 用途／內建模型 key |
|---|---:|---|
| main | 8080 | 聊天與工具呼叫；`<CODE_MODEL>` |
| auditor | 8084 | 審核模型（小模型）：主模型的回答用到知識庫時，逐條核對引用；建議 4B–14B 指令模型，可直接用 VL 的 `qwen3.5-9b` 同一個 GGUF |
| embedding | 8081 | 文件與程式向量；`bge-m3` |
| reranker | 8082 | 檢索排序；`bge-reranker-v2-m3` |
| VL | 8083 | 圖片理解；`qwen3.5-9b`，需配對 mmproj |

前兩個是聊天模型（主模型＋審核模型），後三個是附屬模型。五個服務都是啟動必要條件，
reranker 不可用時不會改用較差排序；審核模型沒有內建預設，由設定精靈選擇，它不載入 mmproj，
所以沿用 VL 的 GGUF 不必另外下載。
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

精靈直接掃描 `~/models`，並沿用保存的 llama-server 路徑或上述預設，不再詢問這兩個位置。
接著依序選主聊天、審核模型（小模型）、embedding、reranker、VL 的模型與 GPU，再選必要容量
參數。單一候選可自動選用；多個候選由使用者決定。主模型問 n_ctx；審核模型也問 n_ctx
（建議 32768，要放得下問題、回答與本回合查到的知識庫證據）；MoE 模型另問
留在 RAM 的 expert 層數；reranker 的 internal buffer 與主 n_ctx 分開設定。
五個服務固定單 slot；主聊天與內部工作共用主模型鎖，審核模型用自己的鎖，不會擋住主模型。
本機精靈保留既有壓縮模式；尚無 client.json 時不因設定模型而建立它。
需要調整壓縮模式，見[壓縮設定](docs/compaction-rules.md)。

DSpark 在主模型設定中一起選，預設 off。相同主模型的既有 draft 配對可以明示沿用，
換主模型不能帶入舊配對。啟用時指定配對的本地 draft GGUF 與 1–64 的 token 上限；
開啟不代表必然加速，啟動還會驗證 `/slots` 的實際狀態。詳細選項見
[DSpark](developer.md#dspark)。

最後只確認一次完整摘要，Enter 寫入、`q` 取消且不寫檔。若目前設定為 A／B 分離模式，
精靈會先明示轉成本機與移除遠端端點授權；取消保留原設定。

**從四個模型的舊版升級**：舊設定沒有審核模型。`git pull` 之後先重跑 `./set_config.sh`
選審核模型，再 `~/start.sh stop`、`~/start.sh`。還沒設定之前，`~/start.sh` 與 `aicode`
都會直接說明缺審核模型並拒絕啟動，不會跳過審核。A／B 分離部署要在 A 更新設定、
重新匯出 manifest，再到 B 重新匯入，見[進階部署](developer.md#advanced-deployment)。

**調度**：審核模型是獨立的 llama-server（8084），啟動順序是
main → embedding → reranker → 審核模型 → VL；VL 最後啟動並依剩餘 VRAM 自動 offload，
所以審核模型與 VL 同卡時不會互搶。審核只在主模型答完之後、同一回合內執行，兩個聊天模型
不會同時為同一題生成；沒用到知識庫的回合不審核。實際放不放得下以 `~/start.sh` 啟動結果為準。

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

五個服務通過 health 與必要能力檢查後才算 ready。服務預設只綁 `127.0.0.1`。
主模型在 tmux session `codetrail-main`；附屬模型與審核模型在 `codetrail-rag`（審核模型是 `audit` window）。
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
用滑鼠左鍵拖選文字，放開即自動複製，不需按鍵；閒置時有選取也可按 **Ctrl+C** 複製，
詳見[選取與複製](docs/usage.md#copy)；回合進行中 **Ctrl+C** 仍中斷。
`/theme` 切換介面主題（`default` 或仿 Codex CLI 的 `codex`），選擇會保存，詳見[介面主題](docs/usage.md#theme)。
SSH／tmux 的剪貼簿排查見
[開發與維運](developer.md#clipboard-ssh-tmux)。

離開 TUI 可用 `/exit` 或閒置時 Ctrl+D，再停止服務：

```bash
~/start.sh stop
```

`set_config.sh`、`aicode` 與附加 shell 入口都不接受參數；`~/start.sh` 只接受無參數啟動
或單一 `stop`。更換模型、GPU、n_ctx 或 Python 後重跑設定並重啟。

<a id="slow"></a>

### 回答很慢或打字卡

先看主模型的解碼速度：`main.log` 裡的 `tg = … t/s`。已知有兩個原因：

- **主模型的 CPU threads 佔滿了每一顆 CPU。** llama.cpp 的 `-t` 預設是實體核心數。
  VM 把 vCPU 攤平（`lscpu` 顯示 `Thread(s) per core: 1`）或關掉 SMT 時，
  實體核心數就等於**全部** CPU。主模型每一步都要等所有 thread 同步，
  只要 aicode、MCP 工具或 SSH 佔住一顆 CPU，解碼就會慢 5–10 倍
  （實測 60 vCPU：13–21 t/s 掉到 2.1 t/s）。
  `set_config.sh` 遇到這種 CPU 拓撲時，會自動把 `-t` 設成 CPU 數的一半；
  看得到 SMT 的機器照舊交給 llama.cpp 決定。
  - 舊設定：重跑 `./set_config.sh` 即可。
  - 要指定其他值：`python3 scripts/set_config.py --threads N`。
  - 確認：`python3 scripts/launch_servers.py --scope all --dry-run` 顯示的主模型指令要帶 `-t`。
  - 生效：`~/start.sh stop`，再執行 `~/start.sh`。
- **舊版 TUI 接回長對話時很吃 CPU。** 2026-09-22 到 2026-09-29 之間的版本有這個問題：
  用 `/session` 接回幾百則的對話後，狀態列、每個按鍵、每個串流 token 都會重排整個畫面，
  TUI 因此佔住一顆 CPU。結果是打字延遲，也會觸發上一個原因。
  更新到之後的版本、重開 `aicode` 即可；新對話不受影響。

<a id="rag"></a>

## 附件、VL 與知識注入

在訊息裡用 `@` 夾帶檔案（輸入框提示字就寫著）：`@相對路徑`，或 `@` 後面打檔名的一部分搜尋整個專案，
Tab 補全（路徑含空白寫成 `@"路徑"`）；把檔案拖進終端機或貼上路徑，也會自動改寫成 `@`：

```text
@screenshots/error.png 畫面上的錯誤是什麼？
@build/firmware.elf 列出 UART 相關的 symbol。
```

送出時客戶端先用既有工具讀附件：圖片、PDF、ELF 與 firmware binary 走 `analyze_file`（圖片經 VL），
其他檔案走 `read_file`；結果以工具卡顯示後模型才回答。單則最多 5 個附件，專案內只收一般檔
（符號連結與目錄不附加），詳見[夾帶附件](docs/usage.md#attachments)。
也可以在對話指定工具與相對路徑：

```text
請用 read_file 讀 logs/build.log，找出第一個失敗。
請用 analyze_file 看 screenshots/error.png，列出畫面上的錯誤文字。
請用 analyze_file 分析 build/firmware.elf，view=symbols、target=uart。
```

讀取結果進對話，不會自動加入可反覆檢索的知識庫。要讓規格、PDF、圖片或 firmware 之後反覆查詢，
用 `/kb` 直接入庫與覆核（不必請模型代勞）：

```text
/kb                    看知識庫狀態：文件、段數、待處理項目
/kb add @docs/spec.pdf 匯入（@ 後可 Tab 補全；圖片會自動經 VL）
/kb review             覆核待確認的圖表與 OCR
/kb remove spec.pdf    移除一份文件（會跳核准框）
```

`/kb add` 完成後顯示結果卡：✓ 完成、⚠ 有待處理項目或 ✗ 失敗，並告訴你還有幾項要到 `/kb review` 處理。
`/kb review` 列出待覆核的表格與圖、需修復與抽取失敗的項目，以及待確認的 OCR 段落；選一項會顯示
原圖的完整路徑、PDF 頁碼與抽出的內容。你對照原圖或原 PDF 確認無誤後按 `y`，有錯就按 `e` 修改再送出；
兩者都會再跳出核准框顯示完整參數。單張抽壞只讓那一張缺席，其餘有效內容可以入庫；
VL 服務、來源身分或文件級契約失敗則整份中止。也可以請模型呼叫工具（例如
「請用 ingest_document 匯入 docs/spec.pdf」）；`preflight_only`、`fresh` 等進階參數只能這樣用。

### 回答審核（小模型）

主模型這一輪用過 `query_knowledge` 或 `query_table` 才回答時，審核模型會在回答下方加一張「審核」卡：
把回答裡依賴知識庫的陳述逐條拿去對照**這一輪查到的證據**，引用必須逐字出現在證據裡才算數。

| 卡片 | 意思 |
|---|---|
| ✔ | 每一項陳述都在證據裡找得到 |
| ⚠ | 有陳述找不到證據，或只有待覆核的圖表／OCR 支持（先 `/kb review` 覆核） |
| ✘ | 有陳述與證據矛盾 |
| ？ | 證據太長沒有全部送審或無法核對，不能給整體結論 |

審核卡只給你看，不會送回主模型；它是核對過引用的提示，不是證明回答完整正確
（一次最多 12 項，也可能漏列）。審核中按 Ctrl+C 只中斷審核，回答保留。

專案外的截圖、下載檔用 `@~/Downloads/…` 夾帶：先在 TUI 輸入 `/import on` 並重開 `aicode`
（或在 `client.json` 設 `external_import` 與 `external_import_roots`）。之後每個專案外附件都會跳出
`import_external_file` 核准框顯示來源與落點，核准後複製進 `.aicode_uploads/` 再讀。用 SSH 連線時，
先把本機檔案傳到這台主機的來源目錄。也可以請模型用 `import_external_file` 匯入後使用回傳路徑。
詳細白名單、表格查值、OCR 覆核、續跑與移除文件見[使用指南](docs/usage.md#attachments)。
`knowledge.json`、上傳副本、review artifacts 與 session 可能含 NDA 內容，不要 commit。

## 工具與人工核准

| 用途 | 工具 |
|---|---|
| 程式探索 | `list_dir`、`read_file`、`grep_code`、`code_rag_search`、`file_info` |
| 知識查詢 | `query_knowledge`、`query_table` |
| 修改與驗證 | `git_status`、`git_diff`、`apply_patch`、`run_lint`、`run_command` |
| 附件與知識庫 | `analyze_file`、`ingest_document`、`remove_document`、`reload_knowledge_base`、`review_figures`、`review_text`、`import_external_file` |
| 行為規則 | `record_lesson` |

八個工具每次需人工核准：`apply_patch`、`run_lint`、`run_command`、`remove_document`、
`record_lesson`、`review_figures`、`review_text`、`import_external_file`。
核准框完整顯示參數；Esc 拒絕該工具，Ctrl+C 中斷整輪。

`apply_patch` 接受 SEARCH/REPLACE、unified diff。最多 5 個檔案、單檔 200 行（udiff 算 added+removed；S/R 算 payload budget = SEARCH+REPLACE 行數）。
套用後只做唯讀 syntax check，lint／test 必須分別呼叫並核准。
`run_command`：timeout 只接受整數 1..600 秒（server 端上限；client 可能更早截止）。
裸名只跑內建白名單；專案內工具以專案相對路徑呼叫（例如 `tools/bin/x`），不需要任何授權指令，
每次仍驗證並需核准，詳見[執行專案內工具](docs/usage.md#project-tools)。
每次 MCP 呼叫固定 read timeout 為 **660 秒**；到期會取消工具，寬限期過則重啟該 MCP instance。

工具文字結果以 `status: ok|partial|error` 開頭。`partial` 與 `next:` 表示需續讀或修復；
省略 `max_chars` 時依 live `n_ctx` 的 12% 配置，明示較大的值會標 `context_risk`。
完整上限、唯讀政策與逐工具說明見[MCP 工具契約](docs/mcp-tools.md)。

## 授權與使用責任

原始碼依 [LICENSE](LICENSE) 提供。模型、GGUF、第三方依賴與被分析資料各自受其授權約束。
請閱讀 [Responsible Use](RESPONSIBLE_USE.md) 與 [Disclaimer](DISCLAIMER.md)，
在分享輸出或部署到自己的環境前核對來源與存取權限。
