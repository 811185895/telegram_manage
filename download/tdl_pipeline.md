# tdl 高速下载管线（方案 C+）

调研结论：tdl + libmp3lame batch + 现有 Playwright metadata，整体提速 4-7x。

## 第一次启动需要你做的事（5 分钟一次性）

### 1. tdl 登录（一次性收 Telegram 验证码）

打开 PowerShell 跑：

```powershell
cd D:\code\telegram_manage\download\bin
.\tdl.exe login -T phone -n tg-session
```

按提示输手机号（带国家码，例如 `+8613812345678`）→ Telegram 应用里会收到一条验证码消息（不是 SMS） → 输验证码 → 如果开了两步验证再输密码。完成后 session 文件存在 `tdl-session-tg-session.dat`（在 `bin/` 目录），以后所有 `tdl` 命令带 `-n tg-session` 就会自动读取。

> **不会触发 ban**：tdl 内置 official api_id，登录方式和官方手机端等价。

完事跑这个验证（应该列出你的所有聊天）：

```powershell
.\tdl.exe chat ls -n tg-session 2>&1 | Select-String "恋爱心理学"
```

应该能看到 `-1001395144198` 这一行。

### 2. 我帮你做的事（已就绪，等 session 文件）

- 已下 tdl 0.20.2 → `download/bin/tdl.exe`
- 写 `tdl_download.py`：
  - 读 Playwright 写的 progress.json 拿 last_anchor_msg_id
  - 用 tdl 拉 chat history JSON
  - 把所有 `msg_id > anchor` 的视频/文件 → 喂给 `tdl dl` 并发拉
  - 下载后按 sender-group 移动到 `F:\telegram_download\<channel>\group_xxx_xxx/msg_xxx/`
  - 调用现有 `extract_audio_batch.py` 抽 mp3
  - 调用现有 main 的转发逻辑（保留 Playwright 部分）把每个 sg 最后一条转发回"下载进度"群
- 速度预期：**`-t 8 -l 4` 起步 7-8 MB/s（4-5x），看实测稳了再开到 `-t 16`**

## 切换前 main #11 会怎么样

main #11 不停（你看到的视频它该下还下），但下载的 video.mp4 / 转发逻辑会被 tdl 接管。新管线起来后会：

1. 跳过 main 已经下完的 sender-group（按 progress.json 锚点）
2. 视频走 tdl，元数据 / 转发 / sender-group 文件结构走现有 Python
3. 现有的 `转写_audio.mp3` 批处理保持不变（每 60s 扫一遍）

## 风险控制（按调研报告 6.2 节）

- 用 `--takeout`：tdl 自带，每次 `dl` 命令带 `--takeout` flag
- 并发：起步 `-t 8 -l 4`，跑稳后看监控数据再调
- 用老账号（>3 月）；新注册的小号易触发风控
- 每天单批 ≤50 GB，分批跑而不是连续 200GB

## 运行命令（你回来一键启动）

```powershell
cd D:\code\telegram_manage\download
python tdl_download.py --user-data-dir D:\UserData\.playwright_user_data--script --out-base F:\telegram_download --threads 8 --concurrent 4 --takeout
```

跑起来后 ctrl+C 可断点续跑，下次启动会从 progress.json 续。
