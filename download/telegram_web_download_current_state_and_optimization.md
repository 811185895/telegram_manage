# Telegram Web 群文件采集脚本现状与优化方案

本文基于当前仓库里的 `download/telegram_web_download_pua1.py`、历史记录 `download/cursor命令记录.md`、旧分析文档 `download/code_analysis_result.md`、问题记录 `download/问题/groups查找方案.md`，以及已落盘产物 `download/pua1` 做静态梳理。

## 1. 当前目标

这个程序的目标是通过 Playwright 自动操作 Telegram Web，从入口群跳转到目标频道/群，然后按日期组遍历消息，保存文本、文件、视频、相册图片/视频，以及评论文本和评论里的媒体。

当前目标频道配置在脚本常量里：

- 入口页：`https://web.telegram.org/a/#-5132543141`
- 目标页：`https://web.telegram.org/a/#-1001395144198`

脚本依赖 Edge 持久化登录态：

- Playwright 用户数据目录：`D:\UserData\.playwright_user_data`
- 浏览器下载目录：`D:\Download`
- Edge 创建逻辑来自外部项目：`D:\code\myNovels\app\长篇自动发布\app\edge_util.py`

## 2. 当前功能状态

### 2.1 已具备的能力

当前脚本已经具备下面这些功能：

1. 打开入口 Telegram Web 页面。
2. 如果入口页面存在 `Go to bottom` 按钮，则点击它。
3. 在入口页中找最后一个日期组的最后一条消息，并点击 `Focus message` 右箭头，尝试跳转到目标页。
4. 如果入口跳转失败，则直接打开目标页。
5. 等待目标页消息列表加载，并按 `.message-date-group` 遍历当前可见日期组。
6. 每个日期组建立一个输出目录。
7. 对每条消息按类型处理：
   - 文件消息：右键菜单 `Download` 下载，然后移动并重命名。
   - 单视频消息：右键菜单下载视频，并截图保存视频封面。
   - 相册消息：遍历相册子项；视频尝试下载，图片使用截图保存。
   - 文本消息：保存为 txt。
   - 评论：点击评论按钮进入讨论区，保存评论文本，并尽力处理评论中的相册/视频/图片。
8. 当前可见组处理完成后，向上滚动加载更多历史消息。

### 2.2 已观察到的落盘结果

仓库里已有下载产物位于：

- `download/pua1/group_January 27_0`
- `download/pua1/group_Wednesday_1`

这些目录里已经出现：

- 文本文件：如 `42261_第20课：长期恋爱关系经营_text.txt`
- 视频文件：如以 `42261_第20课：长期恋爱关系经营_video_` 开头的本地视频文件
- 视频封面：如 `42261_第20课：长期恋爱关系经营_video_image.png`

这说明之前至少成功处理过部分视频消息、文本和封面截图。

### 2.3 当前输出目录存在不一致

当前脚本里配置的是：

```python
OUT_BASE = SCRIPT_DIR / "output" / "pua1"
```

也就是理论输出目录应为：

```text
download/output/pua1
```

但当前仓库中可见的实际产物在：

```text
download/pua1
```

这说明历史版本、运行方式或手动整理可能使用过不同输出目录。后续优化前应先统一输出目录，否则容易误判“没有下载成功”或重复下载。

## 3. 当前代码结构

脚本是一个单文件异步 Playwright 程序，主要分为这些层：

1. 配置层：URL、输出目录、下载目录、用户数据目录、CSS selector。
2. 工具函数层：日志、文件名清洗、文本摘要、日期转换、下载文件移动。
3. 页面导航层：`goto_target_via_entry()` 从入口页跳到目标页。
4. 消息抽取层：文本、文件、视频、相册、评论分别处理。
5. 调度层：`run_once()` 控制日期组遍历、去重和滚动加载。
6. 启动层：命令行参数、创建 Edge page、运行主流程、释放浏览器。

核心流程：

```text
main()
  -> get_page()
  -> goto_target_via_entry()
  -> run_once()
      -> wait date groups
      -> foreach visible group
          -> foreach message
              -> process_one_message()
                  -> process_file_message()
                  -> process_video_message()
                  -> process_album_message()
                  -> save_text_message()
                  -> process_comments()
      -> scroll_once_to_load_more()
```

## 4. 当前主要问题

### 4.1 Playwright selector 强依赖 Telegram Web DOM

脚本大量依赖 Telegram Web 当前 DOM class，例如：

- `.Transition_slide-active`
- `.Transition.MessageList`
- `.messages-container`
- `.message-date-group`
- `.CommentButton`
- `.Album`
- `.File.interactive`

这些 class 不是稳定 API，Telegram Web 前端更新后可能失效。历史记录里已经出现过 MessageList、date group、Focus message 右箭头定位反复修正的问题。

### 4.2 多个 MessageList/隐藏 slide 容易导致选错容器

当前 selector 已经限定：

```python
MESSAGE_LIST_ROOT_SELECTOR = ".Transition_slide-active div.Transition.MessageList:has(div.messages-container)"
```

这个方向是对的，因为 Telegram Web 可能同时保留隐藏 slide。但仍有两个风险：

1. 页面上可能同时存在多个 active-like 容器，`.first` 未必是目标频道消息列表。
2. 评论区、搜索结果、转发面板等也可能出现类似消息列表结构。

建议后续增加“容器校验”，例如校验容器内是否有目标频道的消息、当前 URL 是否已到目标频道、容器内 date group 和 message count 是否稳定增长。

### 4.3 `download_via_context_menu()` 的等待下载逻辑有潜在卡顿/超时问题

当前逻辑是先进入：

```python
async with page.expect_download(timeout=timeout_ms) as download_info:
    await element_handle.click(button="right")
    await download_btn.wait_for(state="visible", timeout=3000)
    await download_btn.click()
```

如果右键菜单没有 `Download`，函数内部会 `return None`。但此时仍在 `expect_download()` 上下文里，退出上下文时仍可能等待下载事件直到超时。也就是说，“没有 Download 菜单”这种常见失败场景，可能会变成长时间卡住。

更稳的方式是：

1. 先右键。
2. 等菜单出现。
3. 确认有 `Download` 菜单项。
4. 再用 `expect_download()` 包住点击 `Download` 的动作。

### 4.4 滚动策略较粗，可能漏组或重复组

当前滚动策略：

```python
el.scrollTo(0, 0)
el.scrollBy(0, -800)
```

这对 Telegram Web 的虚拟列表不一定稳定。可能出现：

- 滚动没有触发历史消息加载。
- 加载后 DOM 替换，旧 locator 失效。
- `date_group_count` 与实际遍历到的 group 数不一致。
- 同一日期组重复处理。

建议改成可观测的滚动：滚动前记录首条消息 id、日期组数量；滚动后等待首条消息 id 变化或日期组数量变化；失败时重试若干次。

### 4.5 日期解析会导致目录名不稳定

当前 `date_to_timestamp()` 支持 `September 6, 2021`、`January 17` 等格式，但如果 Telegram 显示 `Wednesday`，当前会退化为 `group_Wednesday_N`。

仓库中已有 `download/pua1/group_Wednesday_1`，说明此问题已经发生。

后续建议：

- 优先从消息 DOM 的 `datetime`、`title`、`data-*` 属性提取完整日期。
- 如果只能拿到 `Wednesday` 这类相对日期，需要结合当前页面上下文或消息时间推断，至少记录原始 date text。
- 目录名建议保留 `yyyymmdd`，无法解析时用 `unknown_<message_id>`，避免不同周的 `Wednesday` 冲突。

### 4.6 文本保存条件可读性差，可能产生空文本文件

当前代码：

```python
if text or await msg_el.locator(FILE_MESSAGE_SELECTOR).first.count() == 0:
    await save_text_message(msg_el, message_id, text_pre, group_dir, text)
```

含义是：有文本则保存；如果不是文件消息，即使没文本也保存空 txt。这会让视频、图片、相册消息都生成空文本文件。

这可能是有意设计，用于保证每条消息都有一个文本侧车文件；但如果目标是“只保存真实文本”，需要调整。

### 4.7 评论区处理是尽力而为，回退不够可靠

`process_comments()` 点击评论后，用全局 selector 查找：

```python
.sender-group-container, [id^='message-group-']
```

这容易抓到非评论区的消息组。返回主消息列表时使用：

```python
.Button.back-button, [aria-label='Back'], .Header .Button
```

这个 selector 较宽，可能点到非预期按钮。后续应把评论区处理设计成状态机：

1. 进入前记录主列表状态和当前 URL。
2. 点击评论后确认已进入评论上下文。
3. 只在评论容器内抓取。
4. 退出时循环确认主列表恢复。

### 4.8 图片目前主要靠截图，不是原图下载

相册图片和部分媒体图片当前使用 Playwright screenshot。优点是简单、一定能落盘；缺点是：

- 不是原图。
- 分辨率受页面展示尺寸影响。
- 如果图片懒加载或被遮罩，截图可能不完整。

后续如果需要高质量，应增加“打开媒体预览并下载”或“读取图片资源请求”的方案。

### 4.9 缺少断点续跑和下载索引

当前去重只存在于单次运行内：

```python
processed_group_ids: set[str] = set()
```

脚本重启后不知道哪些消息已经处理过，可能重复下载，也无法可靠续跑。后续建议引入本地索引文件，例如：

```text
download/output/pua1/index.sqlite
```

或较轻量的：

```text
download/output/pua1/manifest.jsonl
```

记录每条消息的 message_id、类型、文本摘要、输出文件、处理状态、错误信息。

### 4.10 配置硬编码较多

当前脚本硬编码了：

- Telegram URL
- 输出目录
- 下载目录
- 用户数据目录
- 外部 `myNovels` 项目路径

这降低了复用性和调试效率。建议用命令行参数或配置文件管理。

## 5. 推荐优化路线

### 阶段 1：先做稳定性和可观测性

目标：不大改架构，先让问题可定位、可复现、可续跑。

建议改动：

1. 统一输出目录，明确到底使用 `download/output/pua1` 还是 `download/pua1`。
2. 增加 `--out-dir`、`--download-dir`、`--user-data-dir`、`--entry-url`、`--target-url` 参数。
3. 增加运行日志文件 `run.log`。
4. 增加 `manifest.jsonl`，每处理一条消息就写一条记录。
5. 修复 `download_via_context_menu()` 的 `expect_download()` 包裹时机。
6. 增加 `--dry-run`，只识别消息和类型，不下载。

这一阶段完成后，就能清楚回答：

- 当前识别了多少个日期组？
- 每个组有多少条消息？
- 哪些消息识别为文本/文件/视频/相册/评论？
- 哪些下载成功？
- 哪些失败，失败在哪一步？

### 阶段 2：优化消息列表与滚动加载

目标：减少漏爬、重复爬和 selector 选错容器。

建议改动：

1. 封装 `TelegramMessageList` 类，统一负责定位 active 消息列表。
2. 为消息列表增加校验函数：
   - 当前 URL 是否包含目标 id。
   - 容器是否 visible。
   - 容器内是否存在 date group。
   - 容器内首尾 message id 是否可读取。
3. 把滚动改为“滚动并等待变化”：
   - 记录滚动前最早 message id。
   - 执行小步滚动或 wheel。
   - 等待最早 message id 变化，或 date group 数量变化。
   - 多次失败后停止。
4. 每次滚动后重新获取 locator，避免旧 DOM locator 失效。

### 阶段 3：优化消息类型识别

目标：让每条消息识别结果更可控，减少“误判成视频/相册/文件”。

建议改动：

1. 新增 `classify_message(msg_el) -> MessageKind`。
2. 每条消息先输出结构化识别结果，再决定是否下载。
3. 把文件、单视频、相册、纯文本、系统消息、评论入口拆开处理。
4. 为每个类型记录 selector 命中情况，便于调试 Telegram Web DOM 更新。

推荐消息类型：

```text
system
text
document
single_video
single_photo
album
mixed
unknown
```

### 阶段 4：优化评论采集

目标：评论区单独建模，避免抓到主消息或点错返回按钮。

建议改动：

1. 点击评论前保存上下文：
   - 当前 URL
   - 主列表首尾 message id
   - 当前消息 id
2. 点击评论后等待评论容器出现。
3. 只在评论容器内查找评论 group。
4. 评论媒体沿用主消息媒体处理逻辑，但文件名增加评论 id。
5. 退出评论后确认主列表恢复。
6. 如果无法恢复，重新打开目标页并定位回近似位置。

### 阶段 5：优化图片和视频原始文件质量

目标：从“能保存”提升到“尽量保存原始质量”。

建议改动：

1. 对图片优先尝试打开预览并下载。
2. 如果无法下载原图，再退回截图。
3. 视频下载失败时记录原因，不只吞异常。
4. 对下载文件校验大小，避免保存 0 字节或未完成文件。
5. 文件名后缀缺失时，根据下载对象 suggested filename 或 MIME 做补全。

### 阶段 6：测试与回归样本

目标：减少每次改 selector 都靠真实 Telegram 页面试错。

建议改动：

1. 把 `download/resources/*.html` 作为离线 DOM 样本。
2. 为 selector 和文本解析写离线单元测试。
3. 至少覆盖：
   - 普通文本消息。
   - 文件消息。
   - 单视频消息。
   - 相册消息。
   - 评论文本。
   - 评论相册。
   - date group 日期解析。
4. 对真实页面保留一个 `--dry-run --max-groups 1` 的冒烟测试命令。

## 6. 建议优先修的 5 个点

如果只做一轮小优化，建议按这个顺序：

1. 统一输出目录，并把路径做成参数。
2. 修复 `download_via_context_menu()` 的 `expect_download()` 时机，避免无下载菜单时长时间卡住。
3. 增加 `manifest.jsonl` 和更详细的每消息日志。
4. 改造滚动逻辑，滚动后等待首条消息 id 或日期组数量变化。
5. 把消息类型识别从 `process_one_message()` 中拆出来，先分类再处理。

## 7. 后续可执行改造方案

建议后续代码结构拆成下面这样：

```text
download/
  telegram_web_download_pua1.py        # 保留为 CLI 入口
  telegram_downloader/
    config.py                          # 路径、URL、参数
    selectors.py                       # Telegram Web selector 集中管理
    browser.py                         # get_page 封装
    message_list.py                    # 消息列表定位、滚动、日期组遍历
    message_parser.py                  # 文本抽取、日期解析、类型识别
    media_downloader.py                # 右键下载、截图、移动文件
    comments.py                        # 评论区进入、解析、退出
    manifest.py                        # 处理记录与断点续跑
```

这种拆法的好处是：

- selector 更新只改 `selectors.py`。
- 滚动问题只看 `message_list.py`。
- 下载问题只看 `media_downloader.py`。
- 评论区问题不会污染主消息处理逻辑。
- 后续可以用 `resources/*.html` 给 `message_parser.py` 写离线测试。

## 8. 当前结论

当前程序不是从零开始的状态，已经具备完整采集链路，也确实下载到过一部分文本、视频和封面。主要短板不是“完全不能用”，而是稳定性、可观测性、续跑能力和 Telegram Web DOM 变化适配。

下一步最务实的做法不是立刻大重构，而是先做一轮小而关键的稳定性改造：统一目录、补日志/manifest、修下载等待、增强滚动和消息识别。这样后面再优化评论和原图下载时，会有足够证据判断问题发生在哪一层。
