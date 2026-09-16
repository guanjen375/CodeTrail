# 文件入庫續跑與局部重做

[回到 README](../README.md)。

文件入庫預設 `resume=True`。再次執行相同輸入，會重新驗證來源、parser／抽取設定與使用到的實際模型身分，再復用已原子完成的頁面、圖表與 embedding。文件提交仍會建立完整 `ExtractedDocument`，由原有 KB lock 與 JSON／NPZ 原子交易發布。

```sh
python3 RAG.py spec.pdf knowledge.json
python3 RAG.py spec.pdf knowledge.json --redo-pages 2,5-7
python3 RAG.py spec.pdf knowledge.json --redo-figures fig_0123456789abcdef
python3 RAG.py spec.pdf knowledge.json --retry-failed
python3 RAG.py spec.pdf knowledge.json --no-resume
python3 RAG.py ingest-status spec.pdf --root /path/to/project
```

`ingest-status` 只讀本機進度，不建立狀態、不呼叫模型。輸入路徑與 `--root` 必須指向同一專案；普通 CLI 的 checkpoint 根目錄是已設定 sandbox，或輸出 KB 的父目錄。原圖、正文、canonical payload 及向量都可能含 NDA，狀態與快取必須留在本機。

CLI 明確指定根外文件時保留原生抽取流程，並顯示「不支援 checkpoint/resume」；不啟用根外 structured figure，不接受根外 selective redo 或 MinerU。MCP 仍先依專案 sandbox 拒絕外部來源。根內來源 symlink 會在作業開始固定到 canonical 檔案讀取，保留原 basename，提交前另外檢查原 alias 沒有被重新指向。

`add_document()` 接受 `resume=True, redo_pages=None, redo_figures=None, retry_failed=False`；`rebuild` CLI 使用相同旗標。直接圖片／聊天截圖入庫與 `--image -y`／`--chat -y` 支援 `resume`／`--no-resume` 的文件級復用；互動式預覽與 URL 匯入維持原有流程。頁／圖選擇器僅適用 PDF，三種選擇器互斥，不能搭配 `fresh`、`preflight_only` 或 `resume=False`。選中的頁碼為從 1 起算的實際 PDF 頁碼，圖 ID 必須來自既有 checkpoint／figure 清單。

局部重做需要同來源、同抽取設定的既有 checkpoint。未知頁／圖 ID 在修改 checkpoint 前拒絕；來源或設定已變時，必須先執行一次完整入庫。實際 VL／embedding／context 模型改變會使相應快取失效；局部重做遇到模型漂移會拒絕，避免保留另一個模型世代的單元。無法取得可靠 live model identity 時 fail-loud。Split 部署使用核對過的版本 alias，是 A 端對權重版本的聲明，不冒稱 B 已讀過 A 的權重檔。

原生文字逐頁保存，structured figures 逐候選保存；成功的 figure 保存 canonical payload 與實際送模 variant bytes/hash，沒有依賴舊 `.codetrail/figures/` run 的路徑。原生幾何與候選計畫仍從來源重建，覆核圖片可重新 render。跨頁表格修復與頁內 figure 編號對全集重算；局部重做相同影像的任一 occurrence，會一併重做引用同一份模型輸入的影像群，避免 duplicate 指向不同世代的圖。

已完成但 `needs_review`／`unverified` 的抽取結果可復用，復用不提高 verification。品質抽取失敗保留在清單中，可用 `--retry-failed` 重做；transport failure 或中斷時尚未原子完成的單元會於續跑重新執行。空白／不可取得原生文字的頁、排除與抽取失敗分開列帳，不能由完成頁數推論全 PDF 已 OCR 或已核實。完成通知仍列既有的修復、缺席及待覆核項目；checkpoint 報告補上完整單元清單與成功／復用／重做數。終端顯示最多 40 個單元問題，完整狀態可由 `ingest-status` 取得。

checkpoint 存於 `<project>/.codetrail/ingest/doc-<來源相對路徑hash>/`，私有目錄 0700、檔案 0600；共用的 `.codetrail/` 可沿用同 owner 的 0775 目錄，但仍拒絕 world-write。所有私有讀寫沿 dir-fd 與 `O_NOFOLLOW`，拒絕 hardlink、錯誤 owner／權限與損壞內容。`runner.lock` 的 flock 覆蓋抽取、embedding 到 KB 提交，同文件只能有一個 runner。

成功單元依序 fsync 產物、追加並 fsync 單元 journal，再原子發布小型 committed head；每筆 embedding 不重寫全份清單。`state.json` 在模型身分、候選計畫與文件階段邊界保存完整快照和 journal watermark。續跑及唯讀 `ingest-status` 從快照重播已發布增量，核對 checkpoint 身分、序號、offset 與 SHA-256 鏈；舊版 v1 快照可保留完成單元並升級到 v2，舊版 writer 會拒絕新格式。

head 之外的未發布尾端不能復用，只有取得 runner lock 的續跑會截去該尾端；status 不截斷、不建檔、不探測模型。head 發布失敗會回滾，連回滾也失敗則明示拒絕恢復，不能猜測成功。已提交 journal／head 缺失或損壞會拒絕，不退回舊快照重新抽取；程序中斷留下 `running` 不會被當成功。狀態與單一產物有 128 MiB 上限，journal 有 512 MiB 上限，來源 hash 有 1 GiB 上限，超過會明示拒絕。

figure retention 不清這個目錄，`fresh` 也不刪它。未引用的舊產物目前不自動回收；需要釋放空間時，在無入庫作業執行的情況下刪除對應 checkpoint 目錄，後續入庫會重新抽取。不要只刪 state 指向的單元檔：下次讀取會以損壞 checkpoint 拒絕，不會偷偷重跑掩蓋資料缺損。

人工 figure／OCR 版本基線取自本次入庫開始時的現行 KB，不取自舊 checkpoint。提交前在 KB exclusive lock 內重驗；入庫期間的新校字、確認或撤銷會形成 conflict，舊 checkpoint 不得覆蓋較新覆核。OCR overlay 由正文覆核功能依來源、artifact、paragraph 與原始文字 hash 沿用，checkpoint 保留原始抽取與本次 revision provenance。
