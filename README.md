# Task Assignment v9

目前版本：9.1.0｜本次更新：2026-09-20 00:17（台北時間）

Task Assignment v9 是獨立於 v8 的 Windows 桌面複習管理專案。v8 僅供技術參考；v9 不匯入舊資料，也不保留 CSV、難易度或舊通知流程。

目前已具備任務與複習管理、月曆、儀表板、個人化素材、完整 ZIP 備份／取代匯入，以及使用 Gmail API 手動寄送備份的流程。本版本定位為個人使用；Gmail 使用 External／Testing 設定，未規劃對外發布。

## 文件

- `docs/v9 專案規格與畫面流程.md`
- `docs/v9 開發流程.md`
- `docs/v9 驗證目標.md`

個人完整版 ZIP：`release/遺忘曲線大禮包 必上岸版本-個人完整版-20260914-final.zip`。解壓縮後執行 `TaskAssignment/遺忘曲線大禮包 必上岸版本.exe`。

## 開發原則

先依照規格建立功能，再依照驗證目標進行測試。開發過程中的程式碼、測試與必要文件都放在本專案內。

## 開發環境

需求為 Python 3.11 以上（程式使用標準函式庫的 `StrEnum`）。目前本機完整回歸環境為 Python 3.12；3.11 為最低版本要求，尚未完成獨立版本矩陣驗證。使用封裝 EXE 的一般使用者不需安裝 Python。於 PowerShell 執行：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m pytest -q
```

啟動應用程式：

```powershell
.\.venv\Scripts\python.exe -m task_assignment
```

測試與驗收時可設定 `TASK_ASSIGNMENT_DATA_DIR`，將所有可寫入資料隔離到指定目錄。正式模式使用 `%LOCALAPPDATA%\TaskAssignment\`。

## Gmail OAuth（發布者專用）

一般使用者不需要輸入 Gmail 密碼、API key、client ID 或 client secret。發布者應在 Google Cloud 建立 Desktop app OAuth client，啟用 Gmail API，並完成 Production 狀態及 `gmail.send` 所需驗證。

本機開發時，可將下載的 Desktop client JSON 命名為 `resources/google_oauth_client.json`；此檔已被 Git 忽略，但 PyInstaller 建置時若找到它會自動納入成品。另可在開發環境設定：

```powershell
$env:TASK_ASSIGNMENT_GOOGLE_CLIENT_ID = "<desktop-client-id>"
$env:TASK_ASSIGNMENT_GOOGLE_CLIENT_SECRET = "<desktop-client-secret>"
```

可提交的格式範例位於 `resources/google_oauth_client.example.json`。若發布者設定不存在，應用程式會禁用 Gmail 寄送並保留本機 ZIP 功能。refresh token 只儲存在 Windows Credential Manager，不寫入 SQLite、ZIP 或文字日誌。

## 本機發行候選包

2026-09-10 最新使用性修訂：`release/TaskAssignment-v9-usability-20260910-win64.zip`。修正月曆選日回饋、新增任務分類／標籤入口、自訂日期選擇、預覽位置與不可用按鈕呈現；196 項測試通過。Gmail 缺發布者設定時明確顯示尚未開放使用。

目前建議使用：`release/TaskAssignment-v9-integrated-acceptance-20260909-win64.zip`。整合背景工作接續狀態修正、啟動診斷及最新驗收說明，沿用 10／1／2 秒標準。191 項測試通過，ZIP 校驗與解壓成品啟動檢查通過；仍是驗收版，Gmail 不含發布者設定。

9.1.0 使用性修訂：長篇說明／筆記會在卡片、詳細資料與新增任務表單中受控顯示並可捲動；今日任務與任務管理保留完整筆記提示；個人化設定新增介面縮放、貼圖套用位置提示與版本資訊。

最新啟動改善驗收包：`release/TaskAssignment-v9-startup-acceptance-20260909-win64.zip`。已加入首頁首次刷新去重與啟動分段日誌。2026-09-09 使用者同意放寬啟動至 10 秒、一般查詢至 1 秒；目前本機首次啟動約 7.6～7.8 秒符合新時間上限，受控冷啟動與其他發布條件仍待驗。舊 ZIP 內的說明保留當時門檻，以目前文件修訂為準。

2026-09-14 個人完整包：完整解壓縮後執行 `TaskAssignment/遺忘曲線大禮包 必上岸版本.exe`，保留 `_internal` 資料夾。驗收說明位於包內 `TaskAssignment/_internal/resources/驗收說明.md`（原始文件為 `resources/驗收說明.md`）。

完成 clean PyInstaller 建置後，可執行下列指令。它會先拒絕資料庫、OAuth 認證檔、測試／日誌目錄與本機絕對路徑，再建立 ZIP 與 SHA-256：

```powershell
.\.venv\Scripts\python.exe tools\build_release.py
```

目前產物是 `local-validation-only` 候選包；在 Production Gmail OAuth、真實帳號、參考機冷啟動、DPI 矩陣與乾淨 Windows 10／11 驗收完成前，不可當作正式發布版。
