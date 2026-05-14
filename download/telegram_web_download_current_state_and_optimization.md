# Telegram Web 群文件采集脚本现状与优化方案

本文记录 `download/telegram_web_download_pua1.py` 的最新状态、运行方法、采集结果与已知问题。

## 1. 当前状态（已重写）

入口脚本：[download/telegram_web_download_pua1.py](telegram_web_download_pua1.py)

主要改动：

- **放弃外部依赖 `app.长篇自动发布.app.edge_util`**，直接用 `playwright.chromium.launch_persistent_context` 启动浏览器。
- **使用 sender-group 作为采集单位**（Telegram Web 用 `first-in-group` / `last-in-group` 标记一次连续发送的消息，多条消息在 UI 上聚合成一张卡片）。
- **每条消息都保存文本和原始 HTML**（不依赖右键下载是否成功）：
  - `<msg_id>__<text_pre>__text.txt`
  - `<msg_id>__<text_pre>__raw.html`
- **媒体下载用 JS 派发 `contextmenu` 事件**，等 `.MessageContextMenu .MenuItem` 出现，再点 `Download`，最后用 `expect_download` 抓取下载事件并 `save_as` 到组目录。
- **滚动按"变 first id"等待**，避免无限等待和重复处理。
- **断点续跑**：脚本检测组目录里是否已有同 `msg_id` 的非 text/html 文件，若有则跳过媒体下载。文本/HTML 仍会刷新。
- **每条消息写一行 `manifest.jsonl`**，记录类型、是否下载、错误。
- **每个 sender-group 写一个 `_combined.md`**，把组内所有消息的纯文本按顺序合并，便于阅读。

## 2. 命令行参数

```text
--target            目标频道 hash（默认 #-1001395144198，即"恋爱心理学 追爱脱单 视频教程"）
--out-base          输出根目录（默认 download/output）
--user-data-dir     持久化 profile 目录（默认 D:\UserData\.playwright_user_data--claude）
--browser-channel   浏览器 channel，建议 chrome；留空走 playwright 自带 chromium（可能跟系统 Chrome 写的 profile 不兼容）
--max-groups        最多处理的 sender-group 数量（不传则处理本次滚动可见的全部）
--scroll-max        最多向上滚动加载次数（默认 200；本次实跑用了 30 轮就到顶）
--headless          无头运行
--skip-existing     组目录已有同 msg_id 媒体则跳过下载（默认开启）
--no-skip-existing  强制重新下载
--dry-run           仅识别和记录，不下载
--only-kinds        逗号分隔，只处理指定类型（file,video,album,webpage）
--skip-kinds        逗号分隔，跳过指定类型
--start-msg-id      只处理 msg_id <= 该值的消息（适合分批跑）
```

## 3. 输出目录布局

```text
output/<channel_slug>/
  group_<yyyymmdd>_<sgseq>__<first_msg_id>/
    <msg_id>__<text_pre>__text.txt        每条消息的纯文本
    <msg_id>__<text_pre>__raw.html        每条消息的原始 outerHTML
    <msg_id>__<text_pre>__file_<原名>     文件下载
    <msg_id>__<text_pre>__video<ext>      视频下载
    <msg_id>__<text_pre>__video_thumb.png 视频封面截图
    <msg_id>__<text_pre>__photo.png       图片
    <msg_id>__<text_pre>__album_<sub_id>__video<ext>
    <msg_id>__<text_pre>__album_<sub_id>__photo.png
    <msg_id>__<text_pre>__webpage_preview.png
    _combined.md                           sender-group 内所有消息的纯文本合并
  manifest.jsonl                           运行级别记录（每条消息一行）
  _channel_summary.txt                     频道基本信息
```

## 4. 本次实跑结果（恋爱心理学 追爱脱单 视频教程，`--only-kinds file --scroll-max 30`）

| 指标 | 数量 |
|---|---|
| sender-group 目录 | 337 |
| 消息记录（manifest 行数） | 1113 |
| 文本文件（.txt） | 1115 |
| 原始 HTML（.html） | 1114 |
| 合并 md（每组一个） | 316 |
| 下载的 PDF | 53 |
| 下载的 RAR | 1 |
| 总占用 | 27 MB |
| 时间跨度 | 2025-05-24 ~ 2026-03-23 |

> 注：本次只跑 `--only-kinds file`，所以视频/相册等媒体未下载，但文本/HTML/manifest 都齐了。再跑一次去掉 `--only-kinds` 就会把视频也拉下来。

## 5. 视频下载需要注意

- 视频下载流程：右键 → `Download`，Telegram Web 把整段视频取回，包成 blob 触发浏览器下载。
- 这个频道里的视频是 1~2 小时长课程，单文件 200-500MB 级别。Telegram Web 在打包时浏览器标签容易 crash，并且时长越长越容易卡。
- 当前脚本：单次尝试、15 分超时（`right_click_download(..., timeout_ms=900000, max_attempts=1)`）。crash 时该条消息会失败，但脚本不会整体退出——文本/HTML 仍然保留。
- 短视频（<30 分钟）测试通过；长视频需要更稳的方案（例如：先点开播放预热缓存，再下载；或直接走 MTProto/tdlib 而不是 Web UI）。

## 6. 运行步骤

1. 确保有 Chrome（系统版）安装，并且 `D:\UserData\.playwright_user_data--claude` 这个 profile 已经登录 Telegram Web。
2. 关闭所有用该 profile 启动的 Chrome（包括 playwright MCP），避免 profile 锁冲突。

   PowerShell 杀残留：
   ```powershell
   Get-CimInstance Win32_Process -Filter "Name='chrome.exe'" |
     Where-Object { $_.CommandLine -like '*playwright_user_data--claude*' } |
     ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
   ```
3. 运行：
   ```bat
   cd D:\code\telegram_manage\download
   python telegram_web_download_pua1.py --browser-channel chrome --scroll-max 50
   ```
4. 想分类型分次跑：
   ```bat
   # 只补文本+文件（快）
   python telegram_web_download_pua1.py --browser-channel chrome --only-kinds file --scroll-max 50

   # 然后补视频（慢，可能 crash）
   python telegram_web_download_pua1.py --browser-channel chrome --only-kinds video --scroll-max 50
   ```
5. 想换频道：传 `--target #-1001xxxxx`。`#-` 前面 hash 也要带。

## 7. 已知问题与可改进点

- **页面 hash 跳转不可靠**：`page.goto('https://web.telegram.org/a/#xxx')` 会被 SPA 清掉 hash。当前用 sidebar 链接点击进入，已稳定。
- **长视频下载会让 Chrome 标签 crash**：见第 5 节。
- **date 文本无年份时按当前年算**：Telegram 只在跨年时显示年份，所以"March 11"会被解析为今年。不是 bug，但若运行时刚跨年要注意。
- **图片以截图为主**：网页预览图、相册图都是 screenshot，不是原图。如要原图：点开 media viewer 用其 download 按钮，或读取 `<img src>` 的 blob 后存盘。
- **评论尚未在新版接入**：旧脚本里的 `process_comments` 被去掉了。若需要采集评论，需要在 `process_message` 里加入"点评论按钮 → 等评论容器 → 抓 → 退出"流程。
- **input 是单标签页**：profile 同时只能被一个 chrome 进程使用，不能两个脚本同时跑。

## 8. 下一步建议

1. 跑一次完整的 `--scroll-max 200`（去掉 `--only-kinds`）拉完文本+文件+视频。
2. 视频成功率不满意时，把视频下载切换成"打开 media viewer 并点 viewer 内的 download 按钮"路径——viewer 用流式下载，比 right-click → Download 更稳。
3. 加 `process_comments`，按需开启。
4. 把 `manifest.jsonl` 做成一个简单的 SQLite，方便后续按类型/日期/sender-group 查询。
