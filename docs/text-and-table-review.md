# 表格精確查值與 OCR 正文覆核

[回到 README](../README.md)。

`query_table` 直接查目前入庫版本的 canonical table，沒有 LLM、embedding、reranker 或向量 top-k。`review_text` 保留 MinerU 原始 OCR 與人工校字，確認記錄綁定文字版本及 PDF／content_list 雜湊。

## 精確表格查值

```python
query_table(document_id="spec.pdf::<document-hash>", figure_id="fig_<16-hex>",
            register="CTRL", address="0x4000", column="Reset")
query_table(figure_id="fig_<16-hex>", row=3, column="2")
```

至少提供 `row`、`register` 或 `address` 之一；可用 `document_id`、`figure_id` 限定範圍。`row` 是 canonical 的 1-based `row_index`，不含表頭。`column`／`register_column`／`address_column` 接受完整欄名或 1-based 正整數字串；數字字串一律視為序號。未填 `column` 會回傳唯一符合列的全部格子。

register 名稱逐字、區分大小寫比對，不做模糊搜尋。地址僅正規化十進位數字或 `0x` 十六進位整數，因此 `0x004000` 可匹配 `16384`；不推導 base + offset、不解析範圍或運算式。原始值不改寫。

未指定 selector 欄時，只接受唯一完整表頭：register 欄為 `Register`、`Register Name`、`Name`、`寄存器`、`暫存器`、`名稱`；地址欄為 `Address`、`Register Address`、`Addr`、`地址`、`位址`。此表頭辨識忽略頭尾空白及大小寫。無法唯一辨識時須明示 selector 欄，不能在任意數字格猜地址。

僅採用 verification 可信、品質為 accept、canonical payload revision 與 KB 完全一致的表格。多列符合或重名欄回 `ambiguous`；inherited／merged 格不代填，unreadable／conflict／空格不當作有效值。缺少目前 revision 的 artifact 不回退到歷史 payload。

回傳結構：

```json
{"has_ref":true,"status":"ok","reason":"...","ambiguous":false,
 "matches":[{"value":"0x0001","row_index":3,"column_id":"c2","column_label":"Reset",
             "cell_state":"observed","inherited_from_row":null,"source":"spec.pdf",
             "document_id":"...","page":7,"bbox":[1,2,3,4],"figure_id":"fig_...",
             "revision":2,"evidence_ref":".codetrail/figures/.../manifest.json"}],
 "excluded":[]}
```

`status` 為 `ok`、`not_found`、`ambiguous`、`unverified` 或 `error`。有歧義或選中的格不可信時 `matches` 為空；`excluded` 只含定位與原因，不帶未驗證數值。`ok` 僅表示在所選、可信、目前入庫的表中唯一符合；排除項目仍會列出。

## 正文校字與確認

```python
review_text(action="list", source="spec.pdf")
review_text(action="show", source="spec.pdf", text_id="text_<24-hex>")
review_text(action="correct", text_id="text_<24-hex>", expected_revision=1,
            expected_sha256="<show 中完整 text_content_sha256>", text="校正後的逐字正文")
review_text(action="confirm", text_id="text_<24-hex>", expected_revision=2,
            expected_sha256="<correct 後完整 text_content_sha256>", confirm_against_source=True)
review_text(action="revoke", text_id="text_<24-hex>", expected_revision=3,
            expected_sha256="<目前完整 text_content_sha256>")
```

每個 unit 是 MinerU 抽取後保留的段落／切片，具有 source、page、block 座標及原文 char span，`text_id` 綁定該位置。`show` 列出原始 OCR、目前正文、校字 overlay、bbox、來源路徑、版本、雜湊與覆核記錄。`text` 只替換正文，不包含系統加入的 `[HEADING]` 前綴；原始 OCR 永遠保留。

`correct`、`confirm`、`revoke` 都要求目前 `expected_revision` 與完整 `expected_sha256`，每次成功都增加版本。校字不等於確認；確認需要人對照來源 PDF 後明示 `confirm_against_source=True`。寫入經 MCP 互動核准及唯讀保護；list/show 的核心讀取不建立目錄、鎖檔或模型快取，也不連模型。

內容雜湊、來源 PDF、content_list、位置、rendered prefix 或品質紀錄有變化，原確認立即失效。已知缺行、截斷或來源不完整不能靠按 confirm 洗成可信；必須修復來源並重新 ingest。不可讀字元可先校字，再獨立確認。沒有新 provenance 的舊 OCR 可供檢視，須重新 ingest 後才能覆核。

寫入會在鎖外準備必要的 chunk／gate／section 向量，核對實際 embedding 模型身分，再於 KB exclusive lock 內比對整份快照和 revision。競態會回 `conflict`，不覆蓋他人更新。JSON／NPZ 仍使用現有原子 store，校字不沿用舊文字的向量。

## 重灌、續跑與嚴格查詢

同一來源 re-ingest／resume／局部 redo 僅在 PDF SHA256、content_list SHA256、穩定段落位置及原始 OCR SHA256 全部一致時，沿用目前 KB 的校字 overlay 與確認。checkpoint 的舊覆核不是權威來源；提交前還會比對 ingest 開始時的文字 revision 基線，拒絕覆蓋稍後的校字、確認或撤銷。同一 `text_id` 的來源變更會增加版本並列待覆核。

嚴格查詢共用同一 eligibility 判斷：正文內容、確認版本、目前來源及品質都有效才可入選；section 展開中的每個成員仍各自通過，未確認鄰段不會隨已確認段落合併入選。OCR 段落保留獨立單位，標題前綴只影響召回，不提升 gate 分數。回傳 REF 與 `excluded_text` 帶 `text_id`、`text_revision`、`text_content_sha256`，可直接定位覆核。

此功能只覆核已有 MinerU 正文，沒有宣稱整份 PDF 已完成 OCR。缺頁及 figure 品質仍由原本 ingest／figure 報告列出。校字上限 128 KiB；KB metadata 讀取上限 128 MiB，來源 hash 上限 1 GiB。路徑須在專案根內；根目錄與檔案須由目前使用者擁有，metadata／來源仍拒絕 symlink、hardlink 與非普通檔。既有專案 0775、來源／knowledge.json／store lock 0664 是合法模式，唯讀工具不修改其權限。新建 store lock 與原子發布的 knowledge.json 為 0600；其他 CodeTrail 私有 state 仍依各自的 0600／0700 防線，不因來源讀取相容性而放寬。寫入依賴 POSIX flock／dir-fd／nofollow 與 Linux `/proc/self/fd`，安全能力不足直接拒絕。
