/**
 * CodeTrail 壓縮 plugin —— **已停用的過渡期 stub**。
 *
 * CodeTrail 不再啟動 OpenCode;結構化壓縮搬進 Python 客戶端
 * (`client_compaction.py`)。這個檔留著只有一個理由:使用者的全域
 * `opencode.json` 可能還註冊著這個路徑。直接刪掉的話,從 `git pull` 到跑
 * `python3 opencode_migrate.py` 之間,他在**其他專案**開 OpenCode 都會因為載不到 plugin
 * 而起不來 —— 而錯誤訊息不會提到 CodeTrail。
 *
 * 所以這裡是一個單一 export、零副作用的 stub:只在 session 建立時 toast 一次
 * 「請執行遷移」,不掛任何其他 hook、不讀任何檔、不改任何工具結果。
 *
 * 遷移完成後(`python3 opencode_migrate.py`)這個註冊項會被移除,這個檔就再也不會被載入。
 */
export const CodetrailCompaction = async ({ client }) => {
  let told = false;
  const tell = async () => {
    if (told) return;
    told = true;
    try {
      await client.tui.showToast({
        body: {
          message:
            "CodeTrail 已不再使用 OpenCode:結構化壓縮搬進 CodeTrail 自己的客戶端了。" +
            "請在 CodeTrail 目錄執行一次 python3 opencode_migrate.py 完成遷移(會還原這裡的壓縮設定並取消註冊這個 plugin)。",
          variant: "warning",
        },
      });
    } catch {
      /* fail-open:toast 失敗不得影響使用者的 OpenCode */
    }
  };
  return {
    event: async ({ event }) => {
      if (event?.type === "session.created") await tell();
    },
  };
};
