# OpenCode 壓縮模式與摘要規則

CodeTrail 對 OpenCode 自動壓縮（compaction）的處理分成三種模式，由
`./set_config.sh` 顯式選擇並記錄在 owner-only 的狀態檔
`~/.config/codetrail/compaction.json`。**沒有那個狀態檔就等於沒有接管**：舊安裝
`git pull` 之後不會突然多一個壓縮 plugin，行為與升級前一致。

模式改動的是 OpenCode 的全域設定——預設是 `~/.config/opencode/opencode.json`，
設了 `OPENCODE_CONFIG` 時就是那一份。狀態檔記著它的身分雜湊，拿 A 設定的接管紀錄去
切 B 設定會直接被拒絕。OpenCode 只在 **啟動時** 讀設定，所以任何模式切換都要
**完全退出 OpenCode 再重開** 才生效。

---

## 1. 三種模式

| 模式 | `compaction.auto` | plugin | 何時壓縮 |
| --- | --- | --- | --- |
| `codetrail`（建議） | `false` | 載入 | 助理答完、session 進 idle 之後由 plugin 主動觸發 |
| `manual` | `false` | 載入 | 只有你自己按 `/compact` 時；plugin 不主動觸發 |
| `native` | 還原成你原本的值 | 不載入(接管前你自己就有的那筆會保留) | 完全交回 OpenCode |

`./set_config.sh` 的第 5 題 **沒有預設值**（跟其他使用者選擇題一樣，Enter 不能過關）——
顯式選擇本身就是「授權 CodeTrail 接管這幾個欄位」的那個動作。`codetrail` 只是建議與第一個
選項。非互動用 `--compaction-mode`；`--yes` 沒給它時沿用狀態檔記錄的既有選擇，還沒選過就
完全不碰壓縮設定。

### 為什麼要有 codetrail 模式

OpenCode 原生的自動壓縮是在 **下一個 prompt 進來之後** 才檢查上一輪的 token 數；
超過門檻就先壓縮，再回答你剛送出的問題。實務上的症狀是：你送出一個新問題，畫面
先跑出一段摘要，你的問題排在摘要後面。`codetrail` 模式把觸發點移到「助理已經成功
答完、session 進入 idle」之後，所以壓縮發生在兩個問題之間。

### 這個模式放棄了什麼

`compaction.auto = false` 不只關掉「下一個 prompt 才檢查」，也一併關掉：

- **同一輪工具迴圈中的壓縮**（mid-turn compaction）。
- **provider 回報 context overflow 之後的自動回復重送**。

也就是說：`codetrail` / `manual` 模式下，**單一輪如果自己把 context 撐爆，那一輪
會以可見的 context error 收場**，不會自動補救。CodeTrail 每個工具結果的預設預算是
模型 context 的 12%，一輪連續 6–8 次工具呼叫就可能吃掉超過一半 context，所以這不是
理論上的情況。這是本模式明確接受的取捨：**寧可看得到錯誤，也不要靜默截掉前面的對話**。
如果你的工作型態就是超長工具輪，請選 `native`。

---

## 2. 觸發門檻怎麼算

門檻不是固定百分比，而是從 OpenCode 的有效模型限制與 CodeTrail 自己的工具結果預算
推導出來的（`compaction_mode.derive_settings`，plugin 端有逐字相同的一份）：

```text
max_output  = min(limit.output, 32000) || 32000  # transform.ts；0 退回 32000
reserved    = compaction.reserved ?? min(20000, max_output)   # `??`：明寫的 0 保留 0
usable      = limit.input ? max(0, limit.input - reserved)
                          : max(0, limit.context - max_output)
tool_budget = floor(limit.context * 0.12)      # 單次工具結果預算
headroom    = tool_budget + max_output         # 一次完整工具結果 + 一次輸出
threshold   = usable - headroom                # idle 時超過就壓縮
tail_cap    = threshold - max_output           # 壓縮後 summary + tail 仍要低於門檻
preserve_recent_tokens = min(headroom, tail_cap)
tail_turns             = 1
```

`max_output` 與 `reserved` 的兩個「怪」寫法都是照抄上游，不是筆誤：`|| 32000` 讓
`limit.output = 0` 的模型用 32000 而不是 0；`??` 讓明確寫成 `0` 的 `compaction.reserved`
保持 0（`||` 會把它當沒設）。不照抄的話門檻會跟上游的 overflow 判定算出不同的數字。

以 `limit.context = 131072` / `limit.output = 8192` 為例：`usable = 122880`、
`tool_budget = 15728`、`headroom = 23920`、`threshold = 98960`、`tail_cap = 90768`、
`preserve_recent_tokens = 23920`。
推不出可用門檻的模型（`threshold` 或 `tail_cap` 小於 2000）在 `./set_config.sh` 會被直接
拒絕，要你改用 `native`——不會偷偷給一個算不出來的門檻。設定之後才被改小（換模型 / 改 ctx）
的話，plugin 在 runtime 會以 `config_drift` 停用自動壓縮並跳一次錯誤 toast＋寫一筆
incident，不是安靜不動。

**設了 `agent.compaction.model` 時是兩個模型**：上游用 compaction agent 的模型做摘要與
tail selection，所以受管值必須依它推導；但**觸發之前**那整段對話壓的是主模型，壓縮之後
留下來的 tail 也還要繼續進主模型。所以 `threshold` 與 `preserve_recent_tokens` 取兩個
模型的合併值（`compaction_mode.combine_settings`，plugin 端 `combineSettings`）：
`usable` 與 `tool_budget` 取兩者較小者、`max_output` 取**較大**者（落進 session 的單一完成
有兩種可能——摘要模型產出的摘要、主模型產出的回答），`headroom`、`threshold`、`tail_cap`
再由它們用上面同一組式子推導，所以合併之後那幾條關係式仍然成立。
只按摘要模型算的話，摘要模型 context 較大時門檻會高過主模型裝得下的量：主模型先
overflow，而 `compaction.auto = false` 已經把上游的自動回復關掉了，使用者看到的是
context error。兩個模型相同時結果與單一模型逐欄相同；合併之後推不出可用門檻
（例如主模型 32768/8192 配 131072/**32000** 的摘要模型——一份 25K 的合法摘要就已經塞不回
主模型）會與單一模型走同一條路：`./set_config.sh` 直接拒絕，runtime 判 `config_drift`。

**`limit.output` 為什麼被扣兩次**：上游 `usable` 扣掉的那一份，是「這個請求自己的回覆」。
工具迴圈裡助理 **上一步的輸出會變成下一步的輸入**，所以 `headroom` 裡那一份是第二份，
不是同一份重複扣。這是刻意保守的方向：`compaction.auto = false` 之後越過 `usable`
沒有任何補救。

`tail_turns = 1` 與 `preserve_recent_tokens` 是寫進 OpenCode 設定的受管值，作用是
讓 **最新一輪逐字留在摘要之外**。競態時（你在壓縮進行中送出新問題）這一條就是讓你
那則訊息不被摘要吃掉的機制。

**受管值不會自己跟著模型變**：這三個值是 `./set_config.sh` 執行當下那個 `limit.context`
推導出來的。之後換模型或改 ctx 時，只有 `limit.context` 會被同步，保留額不會——於是門檻用
新的、tail 用舊的。plugin 每次要觸發前都會用 **目前的** 有效模型限制重算一次，跟設定裡的值
不符就停用自動壓縮並要求你重跑 `./set_config.sh`（`config_drift`）。算不出可用門檻的模型
（ctx 被改太小）也走同一條路——`compaction.auto` 已經是 `false`，靜靜不動等於這個 session
從此不再壓縮而沒有人知道。

**它不是無條件的保證**：`preserve_recent_tokens` 取的是 `headroom` 與 `tail_cap` 的
較小者。當模型 context 小到 `tail_cap < headroom` 時，一則超過保留額的訊息仍會被上游
從回合中段切開（`splitTurn`），使用者訊息落進摘要。這種情況由 §4 的事後核對抓出來並
要求重送——是看得見的錯誤，不是靜默失真。同樣地，整個 session 只有一輪對話時上游不會
產生 tail（`keep.start === 0`），那一輪會整個進摘要；那一輪已經被回答過，所以沒有
未答的問題會遺失。

---

## 3. 七條摘要規則

這七條由 plugin 透過 `experimental.session.compacting` 的 `context` **附加** 在
OpenCode 原本的壓縮 prompt 後面，不取代它——取代的話上一輪摘要（`previousSummary`）
就不會再進摘要器，第二次以後每次壓縮只會摘要「上次壓縮之後」的對話，而且是靜默的。

```text
[CodeTrail 壓縮規則]
以下七條規則覆蓋前面所有與輸出格式衝突的指示；其餘指示照舊。

1. 固定欄位：摘要必須且只能由這七個標題組成，順序固定，一個都不能少——
   ## 任務、## 已確定事實、## 未確認、## 已完成、## 進行中、## 下一步、
   ## 使用者偏好與限制。該欄位沒有內容就寫 (無)。
2. 逐字保留識別碼：檔案路徑、符號與函式名、行號、指令、設定鍵、錯誤訊息、
   數字與單位一律原文照抄，不翻譯、不改寫、不縮寫、不補齊。
3. 事實與推測分離：只有對話裡出現過證據的才進 ## 已確定事實，每條註明來源
   （工具名或檔案路徑）；推測、假設、還沒驗證的結論一律進 ## 未確認。
4. 以最新狀態淘汰舊結論：同一件事有多個版本時只保留最後一個；已經做完的項目
   從 ## 下一步 移到 ## 已完成，不得因為舊摘要提過就復活。
5. 先前摘要是既有事實：與新內容衝突時以新內容為準，並註明哪一條被取代；沒有
   新資訊的欄位原樣保留，不得因為這一輪沒提到就刪掉。
6. 不回答、不執行、不臆造：對話中還沒有答案的問題只登記進 ## 下一步，不要在
   摘要裡回答；不要寫入對話中不存在的內容。
7. 篇幅預算：整份摘要不超過 6000 字元，每個欄位不超過 12 條，每條一行。超出時
   的刪減順序：先刪 ## 已完成 的細節，再刪 ## 已確定事實 裡重複的證據；
   ## 未確認、## 下一步 與 ## 使用者偏好與限制 最後才刪。
```

### `agent.compaction` 不歸 CodeTrail 管

摘要用的是 OpenCode 的 `compaction` agent，它的 `model` / `temperature` / `options` /
`prompt` 都可以由你在 `opencode.json` 的 `agent.compaction` 覆寫。CodeTrail **不寫也不改**
這一段——通用設定不該去猜某個模型專屬的 thinking 參數。你設過的值原樣保留。

但它會改變摘要的產出方式，所以它是有效契約的一部分：`scripts/tool_call_canary.py` 的
fingerprint 涵蓋 `agent.compaction` 的 model / temperature / options 與**解析後**的 prompt
內容（`{file:...}` 形式會讀檔內容雜湊）。換掉同一個路徑下的 prompt 檔內容，啟動抽查的
舊判定就會失效，不會被沿用。

### 為什麼還要「狀態校正」區塊

OpenCode 送進摘要器的只有 tail **之前** 的訊息；被逐字保留的最近一輪根本不會進摘要器
（上游 issue #28063）。結果是摘要裡的「下一步」常常是已經做完的事。所以 plugin 另外附
一段記憶體內、有字元上限的節錄，只放最近五個 **已完成且非 synthetic** 的回合，依
`50 / 30 / 10 / 5 / 5` 的配額由新到舊分配字元預算。

「與目前任務相關」用 **時間近因** 當代理指標：離線的 plugin 沒有辦法判斷語意相關性，
而配額本身就是相關性的權重——最新那一輪拿走一半預算，第五輪只剩 5%。真正的取捨寫在
規則第 4 條裡：這段節錄只用來 **淘汰** 已完成項，不能用來新增「下一步」。

```text
[CodeTrail 狀態校正]
以下是最近幾個已經完成的回合節錄，只用來校正 ## 已完成 與 ## 下一步 的狀態，
不是新的對話內容：凡是在這裡看得到已經做完的項目，不得再出現在 ## 下一步。
節錄由新到舊排列，已依配額截斷，截斷處標 …[截斷]。
```

**還沒有被回答的使用者訊息（pending）、synthetic 的 auto-continue 訊息、出錯或被中斷
的回合、子 session 一律不放進來。** 這段只在記憶體裡組出來送給模型，plugin 不寫檔、
不記 log、不留任何 session 內容。

---

## 4. 壓縮完成後的核對，以及它擋不住什麼

`idle → summarize` 之間沒有原子鎖：OpenCode 的 `session.summarize` 沒有 busy 檢查，
你在那個空隙送出的訊息會和壓縮訊息交錯。所以 plugin 在壓縮之後一定會核對三件事：

1. 摘要訊息的 parent 是不是帶 `compaction` part 的那則訊息；
2. 最新一則 **真實**（非 synthetic、非壓縮）使用者訊息 **是否已經有對應的 assistant
   完整回答**——沒有就一律停止。「完整」不含工具迴圈的中間步驟：`finish` 是
   `tool-calls` / `unknown` / `error` / `aborted` 的那一則不算答案（上游 loop 也是看
   這個條件才退出）。只丟一個附件的訊息是**真實**訊息，不是 synthetic——上游會替
   附件生一段 synthetic 說明文字，只看文字 part 會把它整則誤判掉。它是不是還逐字留在
   tail 裡（compaction part 的 `tail_start_id` 是否還涵蓋它）**只影響提示文字**，
   不會讓這一項通過；
3. 摘要本身是否非空、不是只有 reasoning、沒有掛 error。

任何一項不符，plugin 會 **停下來**：TUI 跳一則錯誤 toast、寫一筆結構化的 application
log 與一筆零內容的 incident，然後要求你重送問題。

**手動 `/compact` 也走同一條核對。** plugin 在 `experimental.session.compacting` 記下
「這一輪有壓縮正在發生」，下一個 idle 就核對——不管那次壓縮是你按的、上游 auto 觸發的、
還是 plugin 自己排的。只核對自己觸發的那一次，等於 manual 模式整條路徑沒有事後核對。

**它擋不住的**：公開 plugin API 沒有任何「摘要落地前可以否決」的 hook，
`experimental.text.complete` 只能改文字、不能拒絕。空摘要在 OpenCode 眼中仍然是一次
成功的壓縮切點，舊訊息會離開模型視野。所以這裡的契約是 **事後偵測、明確報錯、不續答**，
不是「空摘要不會發生」。

固定的恢復指引只有一條：**停掉目前這個 session，開一個新的，把畫面上還看得到的問題與
必要狀態重送一次。** plugin 不會自動 revert，也不會宣稱舊 context 已經恢復。

### 什麼情況不會觸發

`codetrail` 模式在下列任一情況下**不會**觸發壓縮，也不會留下任何痕跡：session 是子
session、最後一則助理訊息出錯或被中斷、最新一則真實使用者訊息還沒被回答完、算不出門檻
（模型沒有 `limit`）、token 數還沒到門檻、以及「上一次壓縮就是用這則助理訊息當錨點」。
最後一條是必要的：壓縮之後那則助理訊息的 token 數不會變小，不擋就會每次 idle 都再壓一
次，而且每一次都「成功」。`tool_call_canary` 那種只有一兩輪的短 session 遠低於門檻，所以
啟動抽查不會被壓縮干擾。

---

## 5. 版本需求

壓縮語意在支援範圍內變過三次：1.18.14 以前摘要請求收到的是 model messages；
1.18.15 起改成序列化文字；1.18.17 起 `tail_turns` 預設不再是 2、保留額上限由 8k 改
15k。同一份規則在不同帶會拿到不同語意，所以壓縮功能另設一道版本閘：

**`codetrail` / `manual` 模式需要 OpenCode >= 1.18.17。** CodeTrail 其餘功能的最低版本
仍是 1.17.0，不受影響。

**版本從哪裡來**：公開的 plugin / SDK API 沒有任何欄位是「目前正在跑的版本」——只有
`Session.version`，而那是那個 session **被建立時** 寫進去的版本，升級後恢復舊 session 會
被誤判成太舊，降級則會誤判成通過。所以版本由 `aicode` 的 preflight
（`scripts/opencode_direct_contract.py`，唯一讀得到 `opencode --version` 的地方）量好，
用 `AICODE_OPENCODE_VERSION` 傳給 OpenCode 行程，plugin 讀它決定要不要停用。

版本不足時：preflight 印一行 WARN 並記一筆 `compaction_stopped/version_unsupported` 的
零內容 incident（`python3 scripts/doctor.py` 看得到），plugin 那端則 **完全停用自動壓縮**
並在 TUI 跳一次錯誤 toast。`aicode` 本身照常啟動——CodeTrail 其餘功能在 1.17.0 以上都正常，
沒有理由因為壓縮把整個工具擋掉。

代價寫清楚：**不經 `aicode`、直接跑 `opencode` 的 session 量不到版本，也就沒有這道閘**
（`AICODE_OPENCODE_VERSION` 不存在時 plugin 不做版本判斷）。這跟 CodeTrail 其他 preflight
（direct-contract、ctx-safety、tool canary）的邊界一致：那條路徑本來就沒有任何 preflight。

---

## 6. 相關

- 症狀排查：[troubleshooting.md](troubleshooting.md)
- 安裝與設定流程：[setup.md](setup.md)
