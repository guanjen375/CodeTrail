# 使用實際 build target 分析程式

[回到 README](../README.md)。

CodeTrail 可匯入既有編譯資料，再讓 `code_rag_search` 的 semantic、context、neighbors、path 使用同一個 `build_target`。匯入器只讀資料，不執行 compiler、shell、response file 或 build 指令。

在專案根目錄內準備 `compile_commands.json` 或 verbose build log：

```bash
python3 /path/to/CodeTrail/scripts/import_build_context.py \
  --root /path/to/firmware --target board-debug --variant debug \
  --compile-commands compile_commands.json

python3 /path/to/CodeTrail/scripts/import_build_context.py \
  --root /path/to/firmware --target board-release --build-log build/verbose.log

python3 /path/to/CodeTrail/scripts/import_build_context.py \
  --root /path/to/firmware --show

python3 /path/to/CodeTrail/code_graph.py \
  --root /path/to/firmware --build-target board-debug
```

匯入成功表示保存了輸入與未知項目，並不表示重建結果完全確定。最後一個命令是 CodeTrail 的靜態關聯圖建置，不會編譯 firmware；各 target 的圖需要各建一次，後續會依內容身份更新。

MCP 查詢使用 `code_rag_search(query="初始化流程", build_target="board-debug", mode="context")`。其餘三個 mode 使用同一個 `build_target` 欄位。省略 target 保留原本全專案搜尋，並顯示 target unknown。明示未匯入的 target 直接報錯，避免意外回答另一份編譯設定。

## Target、variant 與來源

`--target` 是使用者明確命名的 profile，不能從 object 路徑推測。可將不同 target 的 compilation database 分開匯入；若資料列含擴充 `target` / `variant` 欄位，則依明示的 target / `--variant` 選取。只有 `--variant` 標籤而輸入沒有 variant 欄位時，標籤不會自行替資料消歧。

同一 source 有多筆 entry 時會保留歧義，即使它們被放進同一個 profile。使用重複的 `--entry-output` 明確選取資料列的原始 `output` 或 `-o` 值；比對為逐字相等，不是 glob。不會自動挑第一筆或假設某個 object 目錄就是 target。

`arguments` 優先於 `command`，command 只解析引用規則。保留 directory、source、output、compiler、原始與展開參數、資料列或 log 行號。支援 GNU GCC/Clang 的 ARM/AArch64 driver 名稱以及 MetaWare `ccac` 等名稱；工具供應商與安裝位置不代表硬體 ISA，匯入器不推測板子的架構或記憶體位置。

Log 接受逐行 compiler driver invocation、Ninja `[n/m]` 前綴、make 的 entering/leaving directory，以及字面的 `cd directory && compiler ...`。其他 shell expansion、複合指令與未辨識 log 行均列 unknown；不宣稱任意內部 compiler stage 或平行交錯目錄 log 都能重建。

## 巨集與包含順序

保存並依序處理 `-D` / `-U`，包含 response file 展開後的位置；`-I`、`-iquote`、`-isystem`、`-idirafter` 保留各群組的輸入順序並按 compiler 的搜尋群組順序解析。quoted include 先查包含它的檔案目錄。相同目錄同時出現在 `-I` 與 system 群組時，保留 system 身分。

`-imacros` 先取得巨集且丟棄一般程式碼，之後依序處理 `-include`；MetaWare `-Hinclude=file` 也支援。response file 的 `@path` 相對於 compilation directory，僅解析參數；MetaWare 第一欄 `!` 註解可被辨識。檔案與遞迴深度都有上限。未知旗標不會被默認忽略；常見 CPU/endian/optimization 選項保留原文，builtin 巨集仍需要輸入證據。

可用 `--builtin-macros path/to/builtin-macros.txt` 提供已取得的完整 `#define` dump，必須對應同一 compiler 與 CPU/語言旗標，且是在這次顯式 `-D`/`-U`、forced include 與 source 之前的初始巨集集合。CodeTrail 不呼叫 compiler 取得它。空檔、缺 dump、缺少動態 builtin 或無法解析的表達式都保留 unknown；不從 `gcc` / `ccac` 的檔名猜 builtin 值。

條件使用 active / inactive / unknown。支援巢狀 if/elif/else、object 巨集、defined、常見有號整數及邏輯運算，並合併未知分支的 define/undef 與 include 副作用。Function macro 展開、unsigned promotion、未支援的 pragma、raw string / trigraph 等保守列 unknown。只有可證明 inactive 的行會排除；unknown 符號可供查閱，但不是已確認編譯或呼叫關係。path 只走確認的邊。compiler forced include 的 graph evidence_line 為 0，resolution_basis 為 `compiler_forced_include`，來源是保存的 compilation entry。

## Generated headers 與失效

Manifest 固定為專案內 `.codetrail/build-context.json`，目錄 0700、檔案 0600，採原子替換與匯入鎖。所有輸入必須在選定 project root 內；逐層 dir-fd / nofollow 讀取會拒絕 symlink、hard link、非一般檔案與根外路徑。

匯入時解析到的 generated headers、response files 保存精確相對路徑與 SHA-256；`--generated-header` 可另列精確檔案。只有實際參與所選 target 的 header 才會進索引。`build/` 仍是全域 hard ignore，索引只額外取 manifest 准入的指定 header，沒有開放整個目錄或改變 grep/list_dir 的範圍。

資料庫、log、response、builtin dump、generated header 改變或消失時必須重新匯入；正常 source/header 每次載入重新判定。指紋包含 target、variant、include 順序、所有正面及缺席依賴的內容身份，連較高優先路徑後來新增同名 header 也會失效。索引與圖以 target 分開保存，查詢前後重驗依賴；查詢中途資料改變會報錯，不沿用舊 target 的快取。

主要格式依據：[Compilation database specification](https://clang.llvm.org/docs/JSONCompilationDatabase.html)、[GNU preprocessing options](https://gcc.gnu.org/onlinedocs/gcc/Preprocessor-Options.html)、[Synopsys MWDT/GCC option matrix](https://foss-for-synopsys-dwc-arc-processors.github.io/toolchain/gcc/option-matrix.html)。MetaWare argument file、`-D/-U/-I` 與 `-Hinclude` 同時依本機已授權的 Programmer’s Guide 查核；合約測試僅使用合成資料。
