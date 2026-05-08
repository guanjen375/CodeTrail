# 第一輪 OpenCode 對話範例

裝完 ai_code、`aicode` 也 wrapper 好之後,這些 prompt 是新手第一次進
OpenCode TUI 可以照貼的範例。每個都會驅動模型用到 ai_code 的工具。

---

## 1. 看一下這個 repo

```
請列出這個 repo 的目錄結構,告訴我主要 entry point 是哪個檔案。
```

模型會做:`list_dir(".")` → 摘要主要檔。

---

## 2. 找測試指令

```
請找出這個 repo 的測試命令(pytest / make test / cargo test 之類)。
```

模型會做:`grep_code` 找測試框架線索 → 視情況再 `read_file` 看設定檔。

---

## 3. 語意搜程式碼

```
找一下處理錯誤(error / exception)相關的核心程式,給我 file:line。
```

模型會做:`code_rag_search(query="error handling")` → 列出 5 個位置 →
用 `read_file` 看其中關鍵的。

---

## 4. 灌規格 PDF 進 KB,再查

> 前提:把 `xxx-spec.pdf` 放到 `$AICODE_ROOT` 下。

```
幫我把 xxx-spec.pdf 灌進知識庫,完了 reload knowledge base。
```

```
這顆 NPU 的 conv2d 最大輸入大小是多少?根據 spec。
```

模型會做:`ingest_document` → `reload_knowledge_base` → `query_knowledge`。
回答應該帶 `根據 REF1 (xxx-spec.pdf p.45)...` 形式的引用。

---

## 5. 分析錯誤截圖

> 前提:把 `error.png`(compile error / runtime crash 截圖)放到 `$AICODE_ROOT/error.png`。

```
analyze error.png 那是什麼錯誤,該怎麼修?
```

模型會做:`analyze_file("error.png")` → `qwen3-vl` OCR 出錯誤訊息 →
解釋並提建議修法。

---

## 6. 改 bug + 跑測試驗證

```
ai_code/utils.py 的 should_ignore_dir 對大寫資料夾名沒 normalize,
幫我改成 case-insensitive,然後跑相關測試確認沒爆。
```

模型會做:`read_file` → 產 unified diff → `apply_patch` → `run_command("pytest -k ignore_dir")`。

> ⚠ `apply_patch` 真的會寫檔。確認專案已 git 控管,出錯可 `git checkout -- .` 還原。
