# ELF、linker、preload 與記憶體配置核對

[回到 README](../README.md)。

在 `aicode` 對話指定檔案，請模型呼叫：

```python
analyze_file(
    path="build/firmware.elf", view="consistency",
    linker_map="build/firmware.map", linker_script="linker.ld",
    preload_log="logs/preload.json", dram_config="board-memory.json",
    map_format="auto",
)
```

所有輸入及 preload 引用的 binary 都必須位於目前 repo；符號連結、硬連結、越界路徑、
讀取中改變的檔案會被拒絕。此操作唯讀，不執行 linker、模擬器或 preload。
也可以只給 ELF；缺少的證據會列為 `unknown`，不需要先猜 DRAM 位址。

架構由 ELF header 判讀。Synopsys 工具與第三方硬體可以搭配使用；工具供應商不代表
板子的記憶體位址。第一版沒有預設硬體配置。

## 第一版格式

| 輸入 | 支援範圍 |
|---|---|
| ELF | pyelftools 解析 32／64 位元 ELF 的 SHF_ALLOC sections、PT_LOAD、NOBITS；報告 e_machine。ET_REL 沒有最終位址，ET_DYN 缺少執行時 load bias 時標未知。 |
| GNU map | `Linker script and memory map` 後的 output sections、換行 section 名稱、明示 load address；不把縮排的 input sections 或 discarded sections 當重複配置。 |
| MetaWare map | `SECTION SUMMARY`／`SECTIONS SUMMARY` 的 section、type、START、END、LENGTH；支援換行名稱。三個數字為十六進位，END 是 inclusive，會核對長度再轉半開區間。 |
| linker script | 常見 `MEMORY` ORIGIN/LENGTH、常數與有界算術、SECTIONS 明示地址／ALIGN／`>region`／`AT(address)`／`AT>region`；MetaWare GROUP 可繼承 region。 |
| preload | GDB `Loading section NAME, size 0xSIZE lma 0xADDRESS`；下述 JSON 記錄；獨立 `--preload ADDRESS FILE` 宣告。宣告本身不證明已執行。 |
| 記憶體配置 | 下述 JSON，或 MEMORY 宣告（視為 LMA region）。 |

格式不辨識、未展開的 INCLUDE／OVERLAY／INITDATA、未知表示式及缺少載入映射會保留
`unknown`。不會執行輸入中的命令，也不宣稱能解讀任意模擬器 log。GNU／MetaWare 的
格式相容性使用合成測資；MetaWare compiler 需要有效授權才能另外產生實際測試產物。

## 明示 preload 記錄

以下是合成示例，位址不能當作實際板級設定。數字可用 JSON 非負整數或 `0x` 字串；
`size` 以 bytes 計，`space` 必須明示。

```json
{
  "schema": 1,
  "records": [
    {"name": ".text", "start": "0x1000", "size": 4, "space": "lma", "operation": "load"},
    {"name": ".bss", "start": "0x1004", "size": 8, "space": "vma", "operation": "zero"}
  ]
}
```

`operation` 是 `load` 或 `zero`。可加 `file`，核對沙箱內原始 binary 的檔案長度；
這不是把 ELF 容器總長度當 PT_LOAD payload。此 JSON 表達提供者的載入／清零紀錄，
不是 CodeTrail 自動取得的硬體執行證明。已知 VMA＝LMA 映射才會跨空間比對。

## 明示可用與保留區域

```json
{
  "schema": 1,
  "complete": false,
  "regions": [
    {"name": "RAM", "start": "0x1000", "size": "0x100", "space": "vma"},
    {"name": "reserved", "start": "0x10f0", "size": "0x10", "space": "vma", "reserved": true}
  ]
}
```

`complete=false` 是預設，表示只提供一部分記憶體配置。完全落在此範圍之外的區間會列未知，
跨出已提供區域的部分會列越界。只有掌握完整配置時才設 `complete=true`；此時未涵蓋的區間
一律視為越界。VMA 和 LMA 的配置分開提供，不能只因數值相同就推定相通。

## 結果解讀

`pass` 表示本次提供且支援的證據相符；`conflict` 列出具體不一致；`unknown` 表示資料
不足或格式未完全解析。每個衝突帶 `[start,end)`、長度、交集／缺口、來源行或 ELF
section／segment index 與 SHA-256。section 包含在 segment 中是正常關係。

檢查包含位址／長度、同層配置重疊、對齊、檔案範圍、filesz≤memsz、map／script 約束、
preload 覆蓋及保留區。NOBITS 和 PT_LOAD 尾端需要清零證據；缺少紀錄會標未知，
不會因 ELF 宣告 `.bss` 就認為執行時已清零。結果過長會保留總數並明示截斷。

格式參考：[GNU LMA](https://sourceware.org/binutils/docs/ld/Output-Section-LMA.html)、
[GNU MEMORY](https://sourceware.org/binutils/docs/ld/MEMORY.html)、
[Synopsys 公開 ARC lab](https://github.com/foss-for-synopsys-dwc-arc-processors/arc_labs/blob/master/doc/documents/labs/level2/lab8.rst)。
