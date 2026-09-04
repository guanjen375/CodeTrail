"""壓縮門檻公式與 canonical 規則文字的決定性契約(docs/compaction-rules.md)。

這裡守的是 runtime 那一半 —— 算錯只是門檻不對,壓縮照樣會發生,沒有任何錯誤訊息:

  * 受管值的推導公式(`compaction_formula.derive_settings` / `combine_settings`)。
  * 摘要格式核對用的七個欄位標題,必須從 canonical 文件解析,不是再抄一份字面值。
  * 單次工具結果的 context 佔比要與 runtime 的那一份相同。

`aicode` 狀態列那一行「目前壓縮模式」(`client_status.py`,經
`codetrail_chat.py status`)也在這裡,同樣只寫會靜默失敗的東西:

  * **顯示的模式必須來自 runtime 用的同一份設定**(`client.json`)。印 `codetrail`
    而實際上沒接管(或反過來)比不印還糟。
  * **這一行是資訊,不是閘**。任何讀取問題都必須 exit 0 並退成「未接管」。
  * **實驗標示**。`codetrail` / `manual` 還在測試階段;標示被拿掉沒有人會收到警告。

opencode.json 的 ownership 那一半(狀態檔、受管鍵、plugin 項、native 還原)在
`tests/test_opencode_migrate.py` —— 它守的是使用者手動執行的一次性升級工具。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import client_status as status  # noqa: E402
import compaction_formula as cm  # noqa: E402

pytestmark = pytest.mark.smoke

def _derived():
    return cm.derive_settings(context_limit=131072, output_limit=8192)


# ---------------------------------------------------------------------------
# 1. 受管值推導
# ---------------------------------------------------------------------------
def test_derive_settings_follows_the_upstream_formula():
    """131072 / 8192 是本機實際的 limit;數字寫死才看得出公式被改過。"""
    derived = cm.derive_settings(context_limit=131072, output_limit=8192)
    assert derived.usable == 131072 - 8192              # overflow.ts usable()
    assert derived.tool_result_budget == 15728          # floor(131072 * 0.12)
    assert derived.headroom == 15728 + 8192
    assert derived.idle_threshold == 122880 - 23920
    assert derived.tail_cap == 98960 - 8192
    assert derived.preserve_recent_tokens == 23920
    assert derived.tail_holds_a_full_headroom_turn is True
    # 受管值(opencode.json 的形狀)是升級工具那一半的事,見
    # tests/test_opencode_migrate.py::test_the_managed_values_come_from_the_formula。


def test_derive_settings_uses_limit_input_and_reserved_when_present():
    """有 limit.input 時 usable 走另一條分支,reserved 才會被扣掉。"""
    derived = cm.derive_settings(
        context_limit=131072, output_limit=8192, input_limit=100_000
    )
    assert derived.reserved == 8192                     # min(20000, 8192)
    assert derived.usable == 100_000 - 8192
    explicit = cm.derive_settings(
        context_limit=131072, output_limit=8192, input_limit=100_000, reserved=20_000
    )
    assert explicit.usable == 100_000 - 20_000


def test_preserve_recent_tokens_is_capped_by_a_derived_budget_not_a_percentage():
    """tail 上限是「門檻 − 一次摘要輸出」,不是 context 的固定百分比。

    小 context 的模型會落在 tail_cap 這一側:此時 tail 裝不下一整個
    headroom 大小的回合,呼叫端要看得到這件事(而不是以為有保證)。
    """
    derived = cm.derive_settings(context_limit=32768, output_limit=8192)
    assert derived.tail_cap == derived.idle_threshold - derived.output_limit
    assert derived.preserve_recent_tokens == min(derived.headroom, derived.tail_cap)
    assert derived.preserve_recent_tokens == 4260
    assert derived.tail_holds_a_full_headroom_turn is False
    # 壓縮完之後「摘要 + tail」必須仍低於門檻,否則下一次 idle 立刻再壓一次
    assert derived.preserve_recent_tokens + derived.output_limit <= derived.idle_threshold


def test_combining_two_models_keeps_the_single_model_relationships():
    """合併值不是逐欄取 min/max 拼出來的,同一組關係式必須仍然成立。

    `limit.output` 不同時逐欄取值會讓 tail_cap 算得比實際寬:主模型 32768/8192
    配 131072/32000 的摘要模型,一份 25K 的合法摘要就已經塞不回主模型,而
    `compaction.auto=false` 已經把上游的自動回復關掉了。這種組合必須被拒絕。
    """
    live = cm.derive_settings(context_limit=65536, output_limit=8192)
    summariser = cm.derive_settings(context_limit=1048576, output_limit=8192)
    combined = cm.combine_settings(summariser, live)
    assert combined.headroom == combined.tool_result_budget + combined.output_limit
    assert combined.tail_cap == combined.idle_threshold - combined.output_limit
    assert combined.preserve_recent_tokens == min(combined.headroom, combined.tail_cap)
    # 壓縮完之後「摘要 + tail」仍要低於門檻 —— 摘要那一份用的是較大的 output。
    assert (
        combined.preserve_recent_tokens + combined.output_limit
        <= combined.idle_threshold
    )
    assert combined.idle_threshold <= live.idle_threshold
    assert combined.preserve_recent_tokens <= live.preserve_recent_tokens

    # 兩個模型相同時逐欄與單一模型完全一樣。
    assert cm.combine_settings(live, live).as_dict() == live.as_dict()

    # 摘要模型的 output 大到 tail 不存在 → fail-loud,不給一個算不出來的值。
    with pytest.raises(cm.CompactionModeError):
        cm.combine_settings(
            cm.derive_settings(context_limit=131072, output_limit=32000),
            cm.derive_settings(context_limit=32768, output_limit=8192),
        )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"context_limit": 0, "output_limit": 8192},
        {"context_limit": 131072, "output_limit": -1},
        {"context_limit": 131072, "output_limit": True},
        {"context_limit": "131072", "output_limit": 8192},
        {"context_limit": 8192, "output_limit": 8192},
    ],
)
def test_derive_settings_refuses_unusable_limits(kwargs):
    with pytest.raises(cm.CompactionModeError):
        cm.derive_settings(**kwargs)


def test_effective_max_output_matches_upstream_transform():
    """`min(limit.output, 32000) || 32000` —— 包含 `|| 32000` 那一段。

    照抄很重要:`limit.output` 是 0 或很大時,上游算出來的有效輸出額跟
    `limit.output` 本身完全不同,門檻也就跟著不同。
    """
    assert cm.effective_max_output(8192) == 8192
    assert cm.effective_max_output(65536) == cm.UPSTREAM_OUTPUT_TOKEN_MAX
    assert cm.effective_max_output(0) == cm.UPSTREAM_OUTPUT_TOKEN_MAX
    # 大 output 的模型:usable / reserved 都要用 cap 過的值
    derived = cm.derive_settings(context_limit=200_000, output_limit=64_000)
    assert derived.output_limit == cm.UPSTREAM_OUTPUT_TOKEN_MAX
    assert derived.usable == 200_000 - cm.UPSTREAM_OUTPUT_TOKEN_MAX
    assert derived.reserved == cm.UPSTREAM_COMPACTION_BUFFER


@pytest.mark.parametrize(
    "context_limit,output_limit",
    [
        (16384, 8192),   # threshold 直接是負的
        (32768, 10000),  # threshold 正的,但 tail_cap 塌掉
    ],
)
def test_derive_settings_refuses_a_model_too_small_for_the_contract(
    context_limit, output_limit
):
    """推不出門檻或 tail 上限時只能報錯 —— 回一個假的門檻等於每次 idle 都壓縮。"""
    with pytest.raises(cm.CompactionModeError):
        cm.derive_settings(context_limit=context_limit, output_limit=output_limit)


def test_tool_result_fraction_matches_the_runtime_contract():
    """12% 是 tool_result_adapter 的契約;兩邊漂移就是門檻與實際預算對不上。"""
    import tool_result_adapter

    assert cm.TOOL_RESULT_CONTEXT_FRACTION == tool_result_adapter.DEFAULT_CONTEXT_FRACTION


# ---------------------------------------------------------------------------
# 2. canonical 規則文字
# ---------------------------------------------------------------------------
def test_canonical_rule_block_has_exactly_seven_numbered_rules():
    block = cm.canonical_block(cm.RULES_BLOCK_MARKER)
    numbered = [
        line for line in block.splitlines() if line[:2] in {f"{i}." for i in range(1, 10)}
    ]
    assert len(numbered) == 7, block
    assert cm.canonical_block(cm.RECONCILIATION_BLOCK_MARKER).startswith(
        cm.RECONCILIATION_BLOCK_MARKER
    )


def test_rule_headings_come_from_the_document(tmp_path):
    """七個欄位標題必須是從文件解析出來的,不是另外抄一份。

    格式核對(plugin 的 summary_format)拿這七個去驗模型產出的摘要。抄一份的話,
    改了規則卻沒改核對,合法摘要會被判成漂移、漂移摘要會被放行 —— 兩種都是靜默的。
    """
    headings = cm.rule_headings()
    assert len(headings) == cm.RULE_HEADING_COUNT
    block = cm.canonical_block(cm.RULES_BLOCK_MARKER)
    for name in headings:
        assert f"## {name}" in block

    doc = tmp_path / "rules.md"
    doc.write_text(
        "```text\n[CodeTrail 壓縮規則]\n1. 固定欄位:## 任務、## 只有兩個。\n"
        "2. 其他\n```\n",
        encoding="utf-8",
    )
    with pytest.raises(cm.CompactionModeError):
        cm.rule_headings(doc=doc)


def test_canonical_block_is_fail_loud_when_the_doc_drifts(tmp_path):
    doc = tmp_path / "rules.md"
    doc.write_text("# 沒有規則區塊\n", encoding="utf-8")
    with pytest.raises(cm.CompactionModeError):
        cm.canonical_block(cm.RULES_BLOCK_MARKER, doc=doc)


# ---------------------------------------------------------------------------
# 3. 狀態檔


# ==========================================================
# 客戶端的壓縮狀態行(client_status.py)
# ==========================================================
def test_render_indents_continuation_lines_under_the_prefix():
    """第二行以後要對齊在 `[aicode] ` 之後,否則橫幅會裂開。"""
    out = status.render(["第一行", "第二行"], "[aicode]").splitlines()
    assert out[0] == "[aicode] 第一行"
    assert out[1] == " " * len("[aicode] ") + "第二行"






# ============================================================
# 客戶端的壓縮狀態行(client_status.py;原 scripts/compaction_status.py)
# ============================================================
# 這一行是資訊,不是閘:任何讀取問題都必須 exit 0 並退成「未接管」。
# 顯示的模式必須來自 runtime 用的同一份設定(client.json)——印 codetrail 而實際
# 上沒接管(或反過來)比不印還糟。



@pytest.mark.smoke
def test_no_client_config_reads_as_untouched(tmp_path):
    lines = status.status_lines({"HOME": str(tmp_path)})
    assert "壓縮模式=manual" in lines[0]
    assert "未接管" in lines[0]


@pytest.mark.smoke
def test_a_takeover_shows_the_mode_and_the_experimental_tag(tmp_path):
    import client_config

    path = client_config.config_path({"HOME": str(tmp_path)})
    path.parent.mkdir(parents=True)
    path.write_text('{"schema": 1, "compaction_mode": "codetrail", "permission": {}}',
                    encoding="utf-8")
    path.chmod(0o600)
    lines = status.status_lines({"HOME": str(tmp_path), "AICODE_N_CTX": "131072"})
    assert "壓縮模式=codetrail" in lines[0]
    assert "🧪" in lines[0]
    assert any("idle 門檻=" in line for line in lines)


@pytest.mark.smoke
def test_an_unreadable_config_never_shows_a_stale_mode(tmp_path):
    import client_config

    path = client_config.config_path({"HOME": str(tmp_path)})
    path.parent.mkdir(parents=True)
    path.write_text('{"schema": 1, "compaction_mode": "codetrail"}', encoding="utf-8")
    path.chmod(0o644)                       # 權限過寬 → runtime 也起不來
    lines = status.status_lines({"HOME": str(tmp_path)})
    assert "不可信" in lines[0]
    assert "壓縮模式=codetrail" not in lines[0]


@pytest.mark.smoke
def test_a_context_too_small_is_disclosed_next_to_the_mode(tmp_path):
    import client_config

    path = client_config.config_path({"HOME": str(tmp_path)})
    path.parent.mkdir(parents=True)
    path.write_text('{"schema": 1, "compaction_mode": "codetrail", "permission": {}}',
                    encoding="utf-8")
    path.chmod(0o600)
    # n_ctx 來自 deployment profile(`config` import 期解析的那一份),不是
    # `AICODE_N_CTX`:殼層殘留一個值,門檻就會按一個沒有人在用的數字印出來。
    import config

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(config, "N_CTX", 8192)
        patch.setenv("AICODE_N_CTX", "1048576")  # 殘留值不得改變結果
        lines = status.status_lines({"HOME": str(tmp_path)})
    assert any("推不出可用的壓縮門檻" in line for line in lines)


@pytest.mark.smoke
def test_the_status_line_never_raises(tmp_path):
    """讀取問題不得變成新的失敗來源。"""
    assert status.status_lines({})
    assert status.main(["--prefix", "[aicode]"]) == 0


@pytest.mark.smoke
def test_the_reasoning_line_says_what_the_client_will_actually_do(tmp_path):
    """「舊回合 reasoning 進不進模型」是 client.json 的鍵,不是環境變數。

    2026-09-04:`CODETRAIL_KEEP_REASONING` 刪除。行為為什麼該變:它決定送進
    模型的 payload 有多大(每段對話三到五成的成長差異),而一個殼層變數等於
    這件事隨環境漂移,狀態行卻只有一份。
    """
    import client_config

    path = client_config.config_path({"HOME": str(tmp_path)})
    path.parent.mkdir(parents=True, exist_ok=True)

    def write(keep: bool) -> None:
        path.write_text(
            json.dumps(
                {
                    "schema": 1,
                    "compaction_mode": "codetrail",
                    "keep_historical_reasoning": keep,
                }
            ),
            encoding="utf-8",
        )
        path.chmod(0o600)

    write(False)
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("CODETRAIL_KEEP_REASONING", "1")  # 殘留值不得翻轉它
        on = status.status_lines({"HOME": str(tmp_path)})
    assert any("舊回合 reasoning=不進模型" in line for line in on)

    write(True)
    off = status.status_lines({"HOME": str(tmp_path)})
    assert any("舊回合 reasoning=保留" in line for line in off)
