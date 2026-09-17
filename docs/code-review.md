# 工作區程式碼審查

[回到 README](../README.md)。

在要審查的 Git repository 根目錄啟動 `aicode`，輸入：

```text
/review
```

不需要參數、外部 review CLI 或 Node/npm。審查使用目前設定的 llama.cpp 主模型；
A/B 分離部署時，沿用已授權的模型端點。

## 範圍與結果

範圍是 **HEAD 到目前工作目錄的淨變更，加上未追蹤且未忽略的新檔案**。
尚未有 HEAD 的新 repo 使用空基底。不提供 branch、commit 或全 repo scan 模式。
若 index 與工作目錄的修改互相抵銷，會列出 index 仍有變更，
不會把「目前淨變更為零」說成沒有 staged 內容。未解決的 merge conflict 會中止審查。

畫面依序顯示來源與逐檔進度，最後列出有位置、觸發條件、影響及原文證據的問題。
模型可以用唯讀工具查閱相關 caller/callee；結果必須通過檔名、changed hunk、
行號與逐字證據檢查，才會當成 finding 顯示。沒有通過格式檢查的回答不算零問題。

變更中的二進位、特殊檔案、未知 Git 內容轉換與超出資源上限的候選來源會明列缺口；
模型或工具錯誤、context gate 拒絕、取消與來源漂移也會顯示未完成。
審查完成前會重新核對快照，避免將過時行號套用到已變動的工作區。
只有全部選定來源完成時才顯示完整結果；空 findings 表示此次未確認問題。

## 操作與資料保留

結果在獨立、可捲動的 TUI 畫面顯示，**不寫入聊天歷史，也不自動匯出檔案**。
中間模型草稿不當成已確認問題呈現。關閉後可重新輸入 `/review` 取得新快照。

Ctrl-C 取消正在進行的審查，包括來源收集、MCP 啟動、模型串流與工具等待。
審查佔用目前對話的回合鎖；完成或取消後才可送出一般對話、壓縮或切換 session。
進行中關閉審查畫面同樣會先取消並收尾。

審查使用獨立的 `--readonly` MCP、唯讀 policy 與受限的 context 工具集合；
原本聊天的 MCP、工具權限及訊息保留原狀。模型請求沿用同一把模型鎖、live n_ctx、
精確 token 計數與 context gate，不啟動額外的預熱或自動壓縮。

## Git 來源與相容性

Git 來源收集不執行 repo 的 filter、hook 或 fsmonitor，並隔離繼承的 Git 設定。
一般 CRLF/text 正規化會核對 index 與屬性，並安全讀取全域 `core.autocrlf`。
先以有界串流掃描確認候選，已證明未變更的來源不佔逐檔審查與 payload 預算。
一般 clone 或 deinit 留下的空 submodule，在確認目錄為空且 gitlink 未變時不列缺口；
非空、指標變更或無法確認內容的 submodule 仍會明列。
未追蹤的巢狀 Git repo 會單獨列為缺口，其他檔案仍可完成審查。
單檔送審內容上限為 512 KiB；身分掃描另有每檔 64 MiB、總計 512 MiB 的上限。
無法在上限內證明未變更的來源仍會明列缺口，不以檔案時間戳猜測。
目前不重現全域 config include、外部 attributes 或 ignore 規則；偵測到這些來源時，
會明確中止並說明範圍無法確認，不會把被忽略的檔案偷偷加入審查。
Git LFS 或 filter 已轉換的工作區內容，若無法與 HEAD/index 核對為未變更，
會列為未知轉換缺口，不執行外部轉換程式。
全域 Git 設定支援 dotfiles 常見的檔案或父目錄 symlink，讀取仍有大小與時間上限，
並核對連結與目標是否改變；這項支援不放寬 repo 來源的 no-follow 防線。

## 參考設計

參考 [Alibaba OpenCodeReview](https://github.com/alibaba/open-code-review/tree/e556dfad56f11afe15cce9b1a0ca7f8993a65134)
的確定性檔案選取、逐檔覆蓋、相關程式碼查證及結果定位設計。
CodeTrail 採原生 Python／Textual 實作，不安裝或呼叫上游 CLI。
