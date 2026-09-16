# Kimi WebBridge 操作细则（调研类任务）

本技能依赖 Kimi WebBridge 守护进程 `http://127.0.0.1:10086` 控制用户真实浏览器。

## 浏览器扩展归属（本技能强制用 Edge）

- **必须用 Edge 扩展**（`bnlffdbcfnanfbknnlaflhlhkocccckg`），不是 Chrome（`fldmhceldgbpfpkbgopacenieobmligc`）。
- 守护进程**同一时刻只允许一个浏览器扩展占用 slot**；Chrome 长期占坑时 Edge 会被拒（daemon 日志反复 "rejected extension ... slot held by ..."）。
- 检查当前连接的浏览器：`curl.exe -s http://127.0.0.1:10086/status`，看 `extension_id` 是否为 Edge 的 `bnlffdbcfnanfbknnlaflhlhkocccckg`。
- 若被 Chrome 占坑：需要用户重启守护进程让 Edge 接管（**不要**自己 `stop/restart`，让用户操作）。
- 核实扩展归属：`$env:LOCALAPPDATA\Microsoft\Edge\User Data\Default\Extensions\{id}` 存在=Edge，Chrome 同路径存在=Chrome。

## 启动守护进程

连接失败（connection refused）时自行启动，不要问用户：

- Windows（PowerShell）：
  ```powershell
  & "$env:USERPROFILE\.kimi-webbridge\bin\kimi-webbridge.exe" start
  ```
- macOS/Linux：
  ```bash
  ~/.kimi-webbridge/bin/kimi-webbridge start
  ```

重复执行是 no-op，安全。**不要**自动执行 `stop/restart/uninstall`。

## 请求格式（Windows 关键）

- 守护进程返回错误会明确提示："write the JSON body to a fresh temp file"。
- **Windows/PowerShell 下必须用文件工具**把请求体写入唯一临时文件（每次新文件名），再：
  ```powershell
  curl.exe -s -X POST http://127.0.0.1:10086/command -H "Content-Type: application/json" --data-binary "@$env:TEMP\webbridge-req-<random>.json"
  ```
  - 必须用 `curl.exe`（裸 `curl` 在 PowerShell 被别名成 Invoke-WebRequest）。
  - 请求体形如 `{"action":"<tool>","args":{...},"session":"<任务名>"}`。
  - 用后删除临时文件。
- 涉及中文/特殊字符的命令，一律走文件体，避免内联被破坏。

## 会话（session）与 tab 定位

- 每个任务固定一个 `session` 名（按任务命名，不按站点）；首次 `navigate` 传 `group_title`（用户语言）。
- tab 定位顺序：
  1. `list_tabs` —— 查看本会话已有 tab（新会话通常为空）。
  2. `find_tab` 传 `active:true` —— 借取用户**当前正看**的 tab（返回 `borrowed:true`）。仅当用户在看该页时有效。
  3. 仍找不到 → `navigate`（`newTab:true`）打开目标 URL。
- `find_tab` 默认只搜本会话的 tab，不会伸到用户其他窗口/历史 tab。
- 关闭 tab 仅当用户明确要求时调用 `close_session`。

## 常用工具

| 工具 | 用途 |
|------|------|
| `navigate` | 打开/切换 URL（`newTab:true` 新开） |
| `find_tab` | 按 URL 找本会话 tab，或借用户当前 tab（`active:true`） |
| `snapshot` | 无障碍树快照，读页面文本、找 `@e` 引用 |
| `evaluate` | 页内执行 JS（支持 async/await），采集/自动化首选 |
| `screenshot` | 截图（返回本地路径，用 Read 查看） |
| `cdp` | 原始 CDP 通道（低层逃生舱） |

## 已知限制与坑

1. **`evaluate` 的 `const/let` 跨调用共享页面 JS 环境**：重复声明同变量会 `SyntaxError`，用 `(() => {...})()` 包一层。
2. **CDP `Input.dispatchMouseEvent`（滚轮）可能长时间挂起**：不要依赖它触发滚动加载，优先 evaluate 的 fetch 路径。
3. **本地 `file://` URL 导航被扩展禁止**（`Navigating to local URL is not allowed`）：本地交付文件无法用浏览器预览，改用静态校验或 `present_files`。
4. **大响应被持久化到 tool-results 文件**：需要时用脚本二次读取，避免命令行内联解析截断。
5. **`event.isTrusted` 严格校验的站点**（部分银行/验证码）会忽略 `click`/`fill`，此时告知用户手动操作。
6. 版本不匹配报错 "Please update the Kimi WebBridge extension" 时，请用户更新扩展后重试。
