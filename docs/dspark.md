# DSpark 推測解碼

[回到 README](../README.md)。

DSpark 使用針對主模型訓練的 draft 模型提出多個候選 token，再由主模型驗證。
CodeTrail 使用 llama.cpp 的 `draft-dspark` 實作；這是推論功能，不是部署角色或硬體名稱。
是否適用取決於主模型、draft 與 llama.cpp build，不能只由模型檔名判定，也不保證加速。
機制與模型來源見 [llama.cpp 官方文件](https://github.com/ggml-org/llama.cpp/blob/master/docs/speculative.md)
及 [DeepSeek DeepSpec](https://github.com/deepseek-ai/DeepSpec)。

## 開啟與關閉

先完成主模型設定，再於 CodeTrail checkout 執行：

```sh
./set_config.sh
```

選擇 **7. DSpark 推測解碼開關**。預設關閉；選 `on` 時提供與主模型配對的本地
draft GGUF，以及每次草擬的 token 上限（1–64，提示預設為 3）。上游可能依 draft
訓練的 block size 再縮小上限。檢查摘要並確認後才保存；`off` 關閉，`q` 取消。
這個選單只更新 `deployment.json`，使用既有交易備份與還原機制，不需要重新選 GPU
或重設其他角色。缺少 draft 或 build 不支援時，開啟操作會報錯；關閉不需要 draft、
GPU 偵測或 binary 檢查。

變更要重啟模型服務才生效：

```sh
~/start.sh stop
~/start.sh
```

使用本次 `set_config` 產生的 `start.sh`。它只接受無參數啟動與單一 `stop`；
`set_config.sh`、`aicode`、host/device 腳本都不接受使用者參數。
內部 Python 維護介面保留，詳見 [README_DEV.md](../README_DEV.md#內部設定與啟動介面)。

本功能只控制 main llama-server。重設相同主模型時保留 DSpark；更換主模型時清除舊
draft 配對並提示重新設定。`/think` 仍獨立控制聊天 thinking，沒有 DSpark 聊天指令。
CodeTrail 不下載、轉換或訓練 draft，也不依機器型號替你選擇配置。

## 分離部署

在模型主機 A（`model-host`）操作此開關，B（`client`）不持有 draft 或啟動設定。
A 切換後，main 的版本 alias 也會更新；關閉時仍可能需要重新計算主模型權重雜湊。
重啟 A 後，使用設定選單 **6. 顯示 A 的 endpoint manifest** 重新交接，並在 B
重新匯入。B 的 endpoint 授權與 live alias 核對仍保留，舊 manifest 不會被靜默接受。

## 設定與驗證

`services.main.dspark` 省略或為 `null` 代表關閉；開啟時的完整形狀是：

```json
{
  "draft_model": "/absolute/path/to/matching-draft.gguf",
  "draft_n_max": 3
}
```

`draft_model` 也可使用既有 model registry key。這個物件只允許 main 使用，且採整值
替換；不能把上一份 draft 的局部設定合併到新配對。模型路徑與 token 上限經驗證後，
由 launcher 產生 `--spec-type draft-dspark`、`--spec-draft-model` 與
`--spec-draft-n-max`。關閉時不產生這些參數，繼承的 `LLAMA_ARG_*` 也不能開啟它。

設定成功只表示設定已保存。啟動時先確認 draft 檔案與 binary 能力；main 的 health
通過後，還必須取得非空的 `/slots`，每個 slot 的 `speculative` 都是真正的 JSON
`true`。無法證實啟用便報錯，launcher 依原有規則回滾本次啟動，不把普通解碼當成成功。
host readiness 使用同一條檢查。

維護用 `python3 scripts/check_status.py --strict` 另外比對實際 process 的 draft
路徑、類型與 token 上限；關閉後仍在跑舊 DSpark process 也會報錯。離線檢查不發出
slot 請求，也不能宣稱已確認啟用。模型身分包含啟用時的 draft 內容與設定，避免換
draft 後重用舊的評測 checkpoint。這些檢查不等於特定模型配對的品質或效能驗收。
