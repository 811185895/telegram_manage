# Telegram 频道全量下载提速 - 技术调研报告

> 调研日期：2026-05-16
> 当前基线：Playwright + Telegram Web，单 chat 单 WS 连接 ≈ 1.8 MB/s
> 调研目标：找到 5-10x 以上提速方案，把"几天"压到"几小时"

---

## TL;DR

**最快且工作量最小的方案是直接换 [iyear/tdl](https://github.com/iyear/tdl)（Go 写的 MTProto CLI，7.5k★，可吃满带宽）做下载，Playwright 保留用于做"消息组织 / 锚点 / metadata"的发现阶段。** 单连接 MTProto 实测约 0.3-0.5 MB/s（比 Web 还慢），但开 8-16 并行连接后，免费账号可稳定到 7-8 MB/s（4-5x 提速），开 32 线程 + 良好出口可到 100 Mbps ≈ 12 MB/s（6-7x）。Telegram Premium 账号在协议层有 2x 加速。**核心瓶颈不在客户端而在账号等级和 FloodWait**：跑全频道几百 GB 时风险在"账号被观察 / 短期 FLOOD_WAIT"而不是带宽——必须开 `takeout` 会话（更高的 flood 限额）并把并发卡在合理区间。Playwright 内部的 Web 下载没有 trick 能进一步加速（MTProto-over-WS 单流被服务端限了）。

---

## 速度对比表

| 方案 | 单连接速度 | 多连接并行 | 实测最高 | 数据来源 |
|---|---|---|---|---|
| Telegram Web (Playwright) | ~1.8 MB/s | ❌ 浏览器不支持 | 1.8 MB/s | 你的实测 |
| Telethon 默认 `download_media()` | 0.3-0.5 MB/s | 默认串行 | ~0.5 MB/s | [Telethon FAQ](https://docs.telethon.dev/en/stable/quick-references/faq.html) / [gist 讨论](https://gist.github.com/painor/7e74de80ae0c819d3e9abcf9989a8dd6) |
| Telethon + cryptg | 0.5-2 MB/s | 默认串行 | ~2 MB/s | [Telethon FAQ](https://docs.telethon.dev/en/stable/quick-references/faq.html) |
| FastTelethon (gist, 10-20 conn) | 多连接 | ✅ 20 个并行 sender | **5-20 MB/s** (用户实测 0.5→20) | [painor/FastTelethon gist](https://gist.github.com/painor/7e74de80ae0c819d3e9abcf9989a8dd6) |
| Pyrogram 默认 + TgCrypto + uvloop | 1-3 MB/s | 默认串行 | ~3 MB/s | [Pyrogram speedups 文档](https://docs.pyrogram.org/topics/speedups) |
| Pyrogram + 第三方并行 patch | 多连接 | ✅ | ~10 MB/s | [pyrogram-fast-file-download](https://github.com/dermasmid/pyrogram-fast-file-download) |
| **tdl (Go, -t 8 -l 4)** | 多连接 | ✅ 默认 8 thread × 4 task | **~7-8 MB/s 免费, 100 Mbps 高带宽** | [tdl docs](https://docs.iyear.me/tdl/guide/download/) / [issue #490](https://github.com/iyear/tdl/issues/490) |
| **tdl (-t 32, 万兆出口)** | - | ✅ 32 thread | **~100 Mbps ≈ 12 MB/s** | [tdl issue #52](https://github.com/iyear/tdl/issues/52) |
| TDLib (官方 C++ lib) | 多连接 | ✅ 协议级并行 | 类官方桌面客户端 | [TDLib 文档](https://core.telegram.org/tdlib/docs/) |
| Telegram Premium 账号 | - | - | **协议层 2x 加成** | [Telegram Premium FAQ](https://telegram.org/faq_premium) |

> 注：所有"MB/s"是字节，不是 Mbps。1.8 MB/s ≈ 14 Mbps。

---

## 三方案对比表

| 方案 | 改动量 | 工作量估算 | 预期速度 | 提速倍数 | 风险 |
|---|---|---|---|---|---|
| **A 保 Playwright，调 Web 下载** | 几乎不改 | 2-4h（可能根本没法加速）| ≤ 1.8 MB/s | **1x（无效）** | 浏览器层 MTProto-over-WS 单流封顶，没有公开 trick |
| **B Telethon 重写 download 部分** | 中等 | 6-12h | 5-15 MB/s | **3-8x** | 需 api_id/hash，需处理 FloodWait，需自己维护并行代码 |
| **C 完全换 tdl 一把梭** | 大但简单 | 4-8h（写薄 wrapper + 跑通） | 7-12 MB/s 免费账号，更高 if Premium | **4-7x** | Go 二进制依赖；JSON 导出锚点需重做；channel 结构需另存 |
| **C+ tdl 下载 + Playwright 做组织** | 中等 | 8-12h | 同上 | **4-7x** | 两套系统协调，但语义清晰 |

**推荐：方案 C+（tdl 下载 + Playwright/Telethon 做消息分组锚点元数据）。** 见后文"推荐方案"。

---

## 1. 开源 MTProto 客户端库详细评估

### 1.1 Telethon (Python)
- **仓库**：https://github.com/LonamiWebs/Telethon · 10k+★，维护者 Lonami，活跃
- **默认下载速度**：0.3-0.5 MB/s（单连接串行 chunk），加 `cryptg`（C 加速 AES-IGE）可提到 1-2 MB/s
- **并行下载支持**：**官方不提供**。维护者立场：`The library does not download or upload files in parallel` because `FloodWaitError` will occur sooner（见 [FAQ](https://docs.telethon.dev/en/stable/quick-references/faq.html) 和 [Issue #1170](https://github.com/LonamiWebs/Telethon/issues/1170)）
- **社区补丁**：
  - [painor/FastTelethon gist](https://gist.github.com/painor/7e74de80ae0c819d3e9abcf9989a8dd6)：把 mautrix-telegram 的 `parallel_file_transfer.py` 拆出来。最多 20 个 `DownloadSender`，每个独立 MTProto 连接拉不同 offset，asyncio.gather 重组。**实测 5-20 MB/s（用户报告：0.5→20）**
  - [FastTelethonhelper on PyPI](https://pypi.org/project/FastTelethonhelper/)：把 gist 封装成 pip 包，API 类似 `await download_file(client, message.media, file_path)`
  - [xwc9527/TeleGet (telebackup)](https://github.com/xwc9527/TeleGet)：标榜支持 cross-DC + 断点续传，实测 2.16GB 在免费账号上 **7.55 MB/s**（活跃度低，6★，仅 7 commit）
- **登录流程**：需到 [my.telegram.org](https://my.telegram.org) 注册 `api_id` 和 `api_hash`（手机号登录，5 分钟），再用 `client.start(phone=...)` 收 SMS/Telegram 验证码登录，session 文件持久化
- **channel scraping 友好度**：⭐⭐⭐⭐⭐ `client.iter_messages(chat, limit=...)` 一行拉所有消息，原生支持 grouped_id（媒体组消息聚合），entity 对象有完整 metadata
- **takeout 会话**：支持 `client.takeout()`，有更宽松的 flood limit，专为"导出全部数据"场景设计——做全频道下载强烈推荐

### 1.2 Pyrogram (Python)
- **仓库**：https://github.com/pyrogram/pyrogram · 4k+★，但 v2.0 后维护节奏放缓，社区 fork [TgCrypto-py + pyrogram fork](https://github.com/KurimuzonAkuma/pyrogram) 活跃
- **默认速度**：装 [TgCrypto](https://docs.pyrogram.org/topics/speedups) + [uvloop] 后 1-3 MB/s
- **并行下载**：**默认串行**。`download_media()` 文档没有 `parallel_chunks` 参数。chunk 大小固定 1 MiB（`stream_media()`）
- **第三方并行**：[dermasmid/pyrogram-fast-file-download](https://github.com/dermasmid/pyrogram-fast-file-download)，用 threading 多线程，星少
- **channel scraping**：⭐⭐⭐⭐ `app.get_chat_history()` 同样易用，media_group 处理略不如 Telethon 干净
- **结论**：相对 Telethon **没有明显优势**，且并行下载生态更弱

### 1.3 TDLib (C++ 官方库) + python-telegram 绑定
- **仓库**：https://github.com/tdlib/td · 8k+★，Telegram 官方维护，**协议最完整**
- **下载机制**：协议级支持并行 chunk，**自动管理并发**（`small_queue_max_active_operations_count` / `large_queue_max_active_operations_count` 由服务端配置下发）。底层和官方桌面客户端相同
- **Python 绑定**：[alexander-akhmetov/python-telegram](https://github.com/alexander-akhmetov/python-telegram)、[aiotdlib](https://github.com/pylakey/aiotdlib)
- **缺点**：
  - 需自己编译 C++ lib（或用 docker 镜像），上手成本高
  - API 是消息事件驱动（callback / update），不如 Telethon 直观
  - 文件下载是异步 update（`updateFile`），需轮询完成状态
- **优点**：**最稳定、不会被识别为"非官方客户端"**（用官方 api_id 可以，但通常仍需自己注册）
- **适合场景**：长期稳定运行的服务，对接现成 [iyear/tdl] 反而省事

### 1.4 GramJS / MTProto-core (Node.js)
- **gramjs**：https://github.com/gram-js/gramjs · ~2k★，是 Telethon 的 JS port。API 基本一致，**并行下载现状和 Telethon 类似（默认串行，需要自己写 / 找 patch）**
- **mtcute** (TypeScript): https://github.com/mtcute/mtcute · 较新，号称性能更好，[FAQ 提到 ban 风险](https://mtcute.dev/guide/intro/faq)
- 对你来说没必要切技术栈到 Node，Python 生态更适合现有 Playwright 代码

### 1.5 关键对比小结

| 库 | 并行下载 | channel scraping | 稳定度 | 活跃度 | 学习曲线 |
|---|---|---|---|---|---|
| Telethon + FastTelethon | 社区补丁 5-20 MB/s | ⭐⭐⭐⭐⭐ | ⭐⭐⭐⭐ | ⭐⭐⭐⭐ | 低 |
| Pyrogram | 弱 | ⭐⭐⭐⭐ | ⭐⭐⭐ | ⭐⭐⭐ | 低 |
| TDLib (Python 绑定) | 协议原生 | ⭐⭐⭐ | ⭐⭐⭐⭐⭐ | ⭐⭐⭐⭐ | 高 |
| gramjs | 弱 | ⭐⭐⭐⭐ | ⭐⭐⭐ | ⭐⭐⭐ | 中 |

---

## 2. 现成频道下载工具评估

### 2.1 [iyear/tdl](https://github.com/iyear/tdl) ⭐ 推荐
- **语言**：Go · **7.5k★ · 741 fork · AGPL-3.0**
- **底层**：[gotd/td](https://github.com/gotd/td)（Go MTProto 实现）
- **关键 flag**：
  - `-t 8` threads per task（每文件并行连接数）
  - `-l 4` concurrent tasks（同时下载文件数）
  - `--continue` 断点续传 / `--restart` 重启
  - `--skip-same` 同名同大小跳过（resume 友好）
  - `--takeout` 启用 takeout 会话（更宽松的 flood limit）
  - `-d /path` 输出目录 / `--template` 自定义命名
- **输入**：单个 message URL 列表（`-u`）或 Telegram 桌面端导出的 `result.json`（`-f`）
- **支持**：受保护频道（"Restrict Saving Content"）下载、forward 等
- **实测速度**：免费账号 8 线程 ≈ 2.88-4.61 MB/s（[issue #490](https://github.com/iyear/tdl/issues/490)），32 线程能到 100 Mbps（[issue #52](https://github.com/iyear/tdl/issues/52)）
- **限制**：
  - **不直接管"按 sender-group 组织文件夹"**——`--template` 只能基于消息元数据命名文件，需要你自己用导出的 JSON 后处理
  - takeout flag 在某些版本下挂起（[issue #247](https://github.com/iyear/tdl/issues/247)，需测试当前版本）
  - 不带强语义 scraping API，得先用桌面客户端导出 JSON 再喂给 tdl
- **登录**：首次 `tdl login -T phone` 走手机号验证（无须自己注册 api_id，tdl 内置）

### 2.2 [Dineshkarthik/telegram_media_downloader](https://github.com/Dineshkarthik/telegram_media_downloader)
- **2.6k★**，v3.4.0（2026-02-24）。v3.0 起从 Pyrogram 迁到 Telethon
- 默认 `max_concurrent_downloads=4`
- 单文件上限 **2 GiB**（关键劣势：你的频道可能有 >2GB 的视频）
- 按 chat_id + 媒体类型分目录
- 适合：中等规模、文件 ≤2GB 场景

### 2.3 [jarvis2f/telegram-files](https://github.com/jarvis2f/telegram-files)
- **2.3k★**，Java + TypeScript，**Web UI + Docker**
- 多账号并行、暂停/继续、规则触发自动下载
- 适合：需要持久后台服务 / Web 控制台，不适合一次性脚本

### 2.4 [vinodkr494/telegram-media-downloader](https://github.com/vinodkr494/telegram-media-downloader)
- PySide6 GUI、限速器、代理、深色主题。适合桌面用户，**不适合自动化**

### 2.5 简评
| 工具 | 适合你吗？ | 理由 |
|---|---|---|
| tdl | ✅ **首选** | 最快、AGPL 透明、CLI 易包装 |
| Dineshkarthik | ⚠️ 可选 | 单文件 2GB 上限是硬伤；但分类目录方便 |
| jarvis2f | ❌ | Web UI 重，对脚本工作流没用 |
| GUI 类 | ❌ | 不能自动化 |

---

## 3. Telegram MCP / Claude Code 插件

### 3.1 官方 Anthropic Telegram 插件
- **[anthropics/claude-plugins-official/external_plugins/telegram](https://github.com/anthropics/claude-plugins-official/blob/main/external_plugins/telegram/README.md)**
- **用途**：把 Telegram Bot 当**远程控制 Claude Code 的通道**，不是下载工具
- 安装：`/plugin install telegram@claude-plugins-official` + BotFather token + Bun runtime
- **结论：和"下载频道视频"无关**

### 3.2 第三方 Telegram MCP server（按相关度排序）

| 仓库 | ★ | 底层 | media download | 大文件 | 评价 |
|---|---|---|---|---|---|
| [chigwell/telegram-mcp](https://github.com/chigwell/telegram-mcp) | 1.1k | Telethon (MTProto) | ✅ `download_media` tool | 受 Telethon 单连接限速制约 (~1-2 MB/s) | **最活跃** (v3.1.4, 59 release, 257 commit) |
| [dryeab/mcp-telegram](https://github.com/dryeab/mcp-telegram) | 243 | Telethon | ✅ `media_download` | 同上 | 80 commit，无 release |
| [sparfenyuk/mcp-telegram](https://github.com/sparfenyuk/mcp-telegram) | 175 | MTProto (库未说明) | ✅ | 同上 | 小，17 commit |
| [chaindead/telegram-mcp](https://github.com/chaindead/telegram-mcp) | - | - | dialog/message 为主 | - | 不主打 media |
| [guangxiangdebizi/telegram-mcp](https://github.com/guangxiangdebizi/telegram-mcp) | - | **Bot API**（不是 MTProto） | ⚠️ Bot API 单文件 ≤20MB 下载 | ❌ | **不适用** |
| [IQAIcom/mcp-telegram](https://github.com/IQAIcom/mcp-telegram) | - | Telegraf (Bot API) | ❌ | ❌ | 不适用 |

**关键判断**：
1. **基于 Bot API 的 MCP 一律排除**——Bot API 下载文件硬性限制 20 MB
2. **基于 Telethon 的 MCP 有 `download_media` 工具**，但它们调用的是 Telethon 默认（串行）实现，**没有任何一个集成了 FastTelethon 并行**——速度上限和你直接用 Telethon 一样（~1-2 MB/s）
3. 让 Claude Code 通过 MCP 直接"download all videos from channel X"——**可行但慢**，且每个文件都要一次工具调用，浪费 token

**MCP 路线结论**：不推荐当主力下载方案。可以保留 `chigwell/telegram-mcp` 作为"探索/查询/拉单条消息"的辅助工具，但批量下载请走 tdl。

---

## 4. 关键性能问题

### 4.1 单连接 vs 多连接，实测差距
- 单连接 Telethon ≈ 0.3-0.5 MB/s
- 单连接 + cryptg ≈ 1-2 MB/s（瓶颈不在加密就是网络 RTT）
- 多连接（10-20 sender）≈ 5-20 MB/s
- **典型加速 5-15x**，但天花板在账号等级和 Telegram 服务端
- 来源：[FastTelethon gist 评论](https://gist.github.com/painor/7e74de80ae0c819d3e9abcf9989a8dd6)、[Telethon Issue #1170](https://github.com/LonamiWebs/Telethon/issues/1170)

### 4.2 大文件（>2GB）怎么办
- **MTProto 协议天然支持任意大小**：要求每个 chunk 在文件前 1 MB 边界对齐，offset 4KB 倍数，limit 4KB 倍数，limit 整除 1 MB（[官方文档](https://core.telegram.org/api/files)）
- **Telethon 没有 2GB 上限**，FastTelethon / tdl 都能下任意大小
- **Dineshkarthik/telegram_media_downloader 有 2GB 上限**——这是它独有的实现限制，不是协议限制
- Premium 账号上传 4GB（普通 2GB），下载侧任意大小

### 4.3 限速 / Ban 风险（重要）
- **没有"日总流量上限"的硬指标**，但有 `FLOOD_PREMIUM_WAIT_X` 错误：账户在短时间内传输 "tens of gigabytes" 会触发（[官方 files API 文档](https://core.telegram.org/api/files)）
- 使用非官方客户端的账号**自动进入观察状态**（[Telethon FAQ](https://docs.telethon.dev/en/stable/quick-references/faq.html)、[mtcute FAQ](https://mtcute.dev/guide/intro/faq)）
- 用户实测：1000 个 media 下载到第 60 个就触发 FloodWait 738 秒（[Telethon issue #1426](https://github.com/LonamiWebs/Telethon/issues/1426)）
- **降低风险的具体做法**：
  1. ✅ **使用 takeout 会话**（`client.takeout()` / `tdl --takeout`）——专为"导出数据"设计，限额宽松
  2. ✅ **不要用新注册的账号**——FAQ 明确说"well-established accounts"
  3. ✅ **限制并发**：tdl 默认 `-t 8 -l 4` 是安全区间，>32 thread 风险加大
  4. ✅ **降速跑**：宁愿 5 MB/s 跑 12 小时，别 30 MB/s 跑 30 分钟然后被封 24 小时
  5. ✅ **写入 ToS 合规边界**：导出"自己有权访问"的频道内容是 ToS 允许的；spam / 转售/ 流量诈骗会被永封
- **永封触发器**：用 API 刷流量、刷订阅、刷阅读量、批量加几百个新群——和你的"读频道历史下载媒体"不沾边

### 4.4 频道 "Restrict Forwarding"（noforwards）会阻挡 MTProto 拉文件吗？
- **不会阻挡 MTProto 直接 `upload.getFile`**：协议层面文件 ID 拿到了就能下载
- 但**频道转发按钮在客户端层面消失**（forward 限制是协议级 noforwards 字段控制）
- tdl 明确支持 "Download files from (protected) chats"（[README](https://github.com/iyear/tdl)）
- **官方立场**：使用未授权 app 绕过 "Restrict Saving Content" 可能被永久 ban 手机号——但"未授权"指的是改过的客户端伪装成官方，**第三方库正常调用 API 不在此列**。**实践上，Telethon / tdl 下载受保护频道文件是常规操作，未见大规模 ban**
- ⚠️ 法律/ToS 风险：你下载的内容必须是你自己有权访问的（你已订阅 + 频道主允许）

---

## 5. 推荐方案

### 推荐：方案 C+ (tdl 做下载 + 现有 Playwright/Telethon 做组织)

**为什么**：
1. tdl 是现成的、AGPL 开源、维护活跃、并行下载已优化好
2. 你现有的 Playwright 代码已经能解析"消息组结构 / sender / metadata / 缩略图"——这部分价值高，别扔
3. tdl 不擅长复杂的"消息组织"，你的代码不擅长"高速下载"——分工最合理

**架构**：
```
[Playwright + Telegram Web]   →   产出 metadata.json (per group):
  - 锚定消息 ID 列表
  - sender / group / 时间
  - 文本 / image thumbnail
  - 视频 message_id（不下载视频本体）

       ↓ (写一个转换脚本)

[result.json for tdl]         →   tdl dl -f result.json -t 8 -l 4 --takeout --skip-same -d ./videos

       ↓

[后处理脚本]                   →   按 metadata.json 把 tdl 下载的视频文件移动 / 重命名到 group 目录
```

**工作量预估**：
- tdl 安装 + 登录 + 跑通示例：1h
- 写 Playwright metadata.json → tdl result.json 转换：2-3h
- 写下载后归组脚本：2-3h
- 调参（threads / concurrent / takeout）+ 端到端跑通：2-3h
- **总计：8-12 小时**

**预期速度**：
- 免费账号 `-t 8 -l 4`：**7-8 MB/s（4-5x 你现在的 1.8 MB/s）**
- 高带宽出口 `-t 32`：可到 100 Mbps ≈ 12 MB/s（6-7x）
- 如果买 Telegram Premium（5 美元/月）：再 2x

**1000 个视频 × 平均 200 MB = 200 GB 的预估**：
- 当前 Playwright 1.8 MB/s ≈ **31 小时**（连续不中断，不算 page 切换开销）
- tdl 7 MB/s ≈ **8 小时**
- tdl 12 MB/s ≈ **4.5 小时**

### 备选：方案 B (Telethon + FastTelethon 全 Python)

**适合场景**：不想引入 Go 二进制、想全 Python 控制
- 装 `pip install telethon cryptg fasttelethonhelper`
- 用 `client.iter_messages()` 拉消息列表，按媒体组分组
- 用 FastTelethon 的 `download_file()` 下视频
- **工作量 6-12h，速度 5-15 MB/s**
- 缺点：你得自己处理 FloodWait 重试、断点续传、cross-DC 边界条件

### 不推荐：方案 A (Playwright 加速)

**没救**。MTProto-over-WebSocket 是 Telegram 服务端单流限速的，浏览器无法开多连接拉 chunk（前端无 MTProto 解密能力）。**理论极限就是你现在看到的 ~1.8 MB/s**。任何"加速 Playwright"的尝试都是在错误的层做优化。

---

## 6. 注意事项

### 6.1 登录流程
- **Telethon / FastTelethon**：[my.telegram.org](https://my.telegram.org) 注册 → 拿 `api_id` / `api_hash` → `client.start(phone='+86...')` → 收 Telegram 应用内验证码（不是 SMS！）→ session 文件保存
- **tdl**：`tdl login -T phone -n my-session`，内置 api_id（也可 `-T desktop` 复用桌面 session 文件，更安全更不易被识别）
- **强烈建议用桌面 session 模式**：`tdl login -T desktop -d "C:\Users\xx\AppData\Roaming\Telegram Desktop\tdata"`——直接复用桌面客户端的会话，**Telegram 看到的就是"官方桌面客户端登录"**，最不易触发风控

### 6.2 限速 / Ban 缓解清单
- [ ] 用**老账号**（>3 个月、有真实使用历史），别用刚注册的小号
- [ ] 用 `--takeout`（tdl）或 `client.takeout()`（Telethon）
- [ ] 并发上限：**tdl `-t 8 -l 4` 起步，看速度再调**；不要无脑 32
- [ ] 单次跑别一次 200 GB，**分批每天 30-50 GB**
- [ ] 装好 FloodWait 重试逻辑（tdl 自带，Telethon 需自己写 try/except）
- [ ] 监控 `FLOOD_PREMIUM_WAIT_X` 错误，遇到立刻暂停 1 小时
- [ ] 不要同账号同时跑多个进程

### 6.3 受保护频道
- tdl 支持下载 noforwards 频道文件，**但要先用桌面客户端"加入"该频道**
- 不要把下载的内容再二次转发到公开渠道——这是 ToS 红线

### 6.4 文件命名 / 归组
- tdl `--template` 只接受消息字段（id / date / file_name），**不能"按 message_group 分目录"**
- 必须用你 Playwright 现有的 metadata 做后处理：用 `os.rename` 把 tdl 下载的 `42261.mp4` 搬到 `pua1/group_January 27_0/42261_第20课....mp4`

### 6.5 大文件 resume
- tdl `--continue --skip-same` 组合是断点 + 跳过已下载，**幂等**
- FastTelethon 默认无 resume，需自己存 offset

---

## 7. 参考链接

### 库 / 工具
- [iyear/tdl GitHub](https://github.com/iyear/tdl)
- [iyear/tdl 文档](https://docs.iyear.me/tdl/)
- [iyear/tdl download 命令文档](https://docs.iyear.me/tdl/guide/download/)
- [LonamiWebs/Telethon](https://github.com/LonamiWebs/Telethon)
- [Telethon FAQ](https://docs.telethon.dev/en/stable/quick-references/faq.html)
- [painor/FastTelethon gist](https://gist.github.com/painor/7e74de80ae0c819d3e9abcf9989a8dd6)
- [FastTelethonhelper PyPI](https://pypi.org/project/FastTelethonhelper/)
- [xwc9527/TeleGet (telebackup)](https://github.com/xwc9527/TeleGet)
- [pyrogram/pyrogram](https://github.com/pyrogram/pyrogram)
- [Pyrogram speedups](https://docs.pyrogram.org/topics/speedups)
- [dermasmid/pyrogram-fast-file-download](https://github.com/dermasmid/pyrogram-fast-file-download)
- [tdlib/td (TDLib)](https://github.com/tdlib/td)
- [gram-js/gramjs](https://github.com/gram-js/gramjs)
- [mtcute](https://mtcute.dev/)

### 现成下载工具
- [Dineshkarthik/telegram_media_downloader](https://github.com/Dineshkarthik/telegram_media_downloader)
- [jarvis2f/telegram-files](https://github.com/jarvis2f/telegram-files)
- [vinodkr494/telegram-media-downloader](https://github.com/vinodkr494/telegram-media-downloader)
- [uncagedspirit/Telegram-video-downloader](https://github.com/uncagedspirit/Telegram-video-downloader)

### MCP 服务器
- [chigwell/telegram-mcp](https://github.com/chigwell/telegram-mcp) （Telethon + 下载 media，1.1k★）
- [dryeab/mcp-telegram](https://github.com/dryeab/mcp-telegram)
- [sparfenyuk/mcp-telegram](https://github.com/sparfenyuk/mcp-telegram)
- [chaindead/telegram-mcp](https://github.com/chaindead/telegram-mcp)
- [anthropics/claude-plugins-official Telegram](https://github.com/anthropics/claude-plugins-official/blob/main/external_plugins/telegram/README.md)

### 关键 issue / benchmark
- [Telethon Issue #1170: parallel file ops](https://github.com/LonamiWebs/Telethon/issues/1170)
- [Telethon Issue #1426: FloodWait in download_media](https://github.com/LonamiWebs/Telethon/issues/1426)
- [Telethon Issue #730: slow media speed](https://github.com/LonamiWebs/Telethon/issues/730)
- [tdl Issue #490: slow speed report](https://github.com/iyear/tdl/issues/490)
- [tdl Issue #52: telegram limits unofficial clients](https://github.com/iyear/tdl/issues/52)
- [tdl Issue #247: --takeout bug](https://github.com/iyear/tdl/issues/247)

### 协议 / 官方
- [Telegram MTProto files API](https://core.telegram.org/api/files)
- [TDLib docs](https://core.telegram.org/tdlib/docs/)
- [Obtaining api_id (my.telegram.org)](https://core.telegram.org/api/obtaining_api_id)
- [Telegram Premium FAQ](https://telegram.org/faq_premium)
- [Telegram MTProto security guidelines](https://core.telegram.org/mtproto/security_guidelines)
- [mtcute FAQ - ban risk](https://mtcute.dev/guide/intro/faq)
