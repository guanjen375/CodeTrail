/**
 * CodeTrail 通知 plugin —— **已停用的過渡期 stub**。
 *
 * ingest 待辦通知與假工具呼叫偵測都搬進 Python 客戶端
 * (`client_notify.py`)。這個檔留著的理由與 codetrail-compaction.js 相同:
 * 使用者的全域 `opencode.json` 可能還註冊著這個路徑,直接刪掉會讓他在其他
 * 專案開 OpenCode 時失敗。
 *
 * 單一 export、零副作用:只在 session 建立時 toast 一次,**不碰任何工具結果**。
 */
export const CodetrailNotify = async ({ client }) => {
  let told = false;
  const tell = async () => {
    if (told) return;
    told = true;
    try {
      await client.tui.showToast({
        body: {
          message:
            "CodeTrail 已不再使用 OpenCode。請在 CodeTrail 目錄執行一次 python3 opencode_migrate.py 完成遷移" +
            "(會取消註冊這個 plugin)。",
          variant: "warning",
        },
      });
    } catch {
      /* fail-open */
    }
  };
  return {
    event: async ({ event }) => {
      if (event?.type === "session.created") await tell();
    },
  };
};
