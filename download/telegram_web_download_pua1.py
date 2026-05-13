"""
Telegram Web 频道消息与媒体下载脚本 (Playwright)

先打开入口页 -5132543141，若有「Go to bottom」则点一下，
再点击最后一个 group 的最后一个 message 的右箭头跳转到目标频道 -1001395144198，
在消息列表中滚动加载更多，按日期组解析消息，
将文件/视频/图片/图文集/评论等保存到 output/pua1/group_yyyymmdd_N 目录。

用法:
  1. 安装: pip install -r requirements.txt && playwright install chromium
  2. 运行: python telegram_web_download_pua1.py [--max-groups N] [--scroll-times N]
  3. 首次需在浏览器中登录 Telegram Web，脚本会打开频道并开始抓取。

命令行参数:
  --max-groups N    最多处理的日期组数量（例: --max-groups 2 只处理前 2 组）
  --scroll-times N  向上滚动加载更多消息的次数（默认 5）

# 只处理前 2 个日期组，滚动 3 次加载
python telegram_web_download_pua1.py --max-groups 2 --scroll-times 3
# 处理全部组，但只滚动 10 次
python telegram_web_download_pua1.py --scroll-times 10
# 只看前 1 组（快速试跑）
python telegram_web_download_pua1.py --max-groups 1 --scroll-times 1


保存规则:
  - 组目录: output/pua1/group_{yyyymmdd}_{N}，如 group_20210906_0
  - 文件: messageid_textpre_file_原文件名
  - 视频: messageid_textpre_video_{上传时间戳}.mp4，封面 messageid_textpre_video_image.png
  - 相册子项: messageid_textpre_子id_video_* 或 messageid_子id_textpre_img.png
  - 文本: messageid_textpre_text.txt（无文本则 textpre=无文本）
  - 评论: messageid_textpre_comments_评论数.txt，评论内媒体带 cmt_pre、评论 id
"""
import argparse
import asyncio
import re
import shutil
import sys
from pathlib import Path
from datetime import datetime
from typing import Optional
# 项目根目录
project_root = Path(r"D:\code\myNovels")
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))
from app.长篇自动发布.app.edge_util import get_page

from playwright.async_api import Page, Download, TimeoutError as PlaywrightTimeoutError

# 脚本所在目录
SCRIPT_DIR = Path(__file__).resolve().parent
# 保存根目录：output/pua1
OUT_BASE = SCRIPT_DIR / "output" / "pua1"
# 浏览器默认下载目录（右键 Download 会落在这里）
DOWNLOAD_DIR = Path(r"D:\Download")
# 持久化用户数据目录（与 create_book_起点.py 一致，可用同一目录或单独目录）
USER_DATA_DIR = Path(r"D:\UserData\.playwright_user_data")

# 入口页：从此页点击「最后一个 group 的最后一个 message 的右箭头」跳转到目标频道
ENTRY_URL = "https://web.telegram.org/a/#-5132543141"
TARGET_URL = "https://web.telegram.org/a/#-1001395144198"
# Go to bottom 按钮（入口页若有则点一下）
GO_TO_BOTTOM_SELECTOR = 'button[aria-label="Go to bottom"], button[title="Go to bottom"]'
# 最后一个 group 的最后一个 message 的右箭头（Focus message）
# 最后一个 message 的右箭头：排除 sticky 区，只匹配带 Focus message 的按钮（download_flag.html 1765-1772）
LAST_MESSAGE_RIGHT_ARROW_SELECTOR = '.message-action-buttons:not(.message-action-button-sticky) button[aria-label="Focus message"]'
# MessageList 根：不写死 class 组合，只要内部有 .messages-container 即可（参考 groups查找方案.md）
# 限定在 Transition_slide-active 下，避免选到隐藏的 slide
MESSAGE_LIST_ROOT_SELECTOR = ".Transition_slide-active div.Transition.MessageList:has(div.messages-container)"
MESSAGE_LIST_SELECTOR = f"{MESSAGE_LIST_ROOT_SELECTOR} div.messages-container"
# 日期组（pua1.html 5587/5599 等 .message-date-group）
DATE_GROUP_SELECTOR = ".message-date-group"
# 组内时间戳（sticky-date 内 span 文本，如 "September 6, 2021"）
STICKY_DATE_SELECTOR = ".sticky-date span"
# 组内消息（含 message-group 下的 message）
MESSAGE_ITEM_SELECTOR = ".message-date-group div.message-list-item[data-message-id]"
# 文件消息：.message-content.document 或 .File
FILE_MESSAGE_SELECTOR = ".message-content.document .File, .message-content .File.interactive"
FILE_TITLE_SELECTOR = ".file-title"
# 视频消息：.message-content.media .media-inner video
VIDEO_MESSAGE_SELECTOR = ".message-content.media .media-inner"
VIDEO_TAG_SELECTOR = "video.full-media"
THUMBNAIL_SELECTOR = "img.thumbnail"
# 图文集（相册）
ALBUM_SELECTOR = ".Album"
ALBUM_ITEM_SELECTOR = "[id^='album-media-message-']"
# 文本内容（排除 MessageMeta）
TEXT_CONTENT_SELECTOR = ".text-content.with-meta, .text-content"
MESSAGE_META_SELECTOR = ".MessageMeta"
# 评论按钮
COMMENT_BUTTON_SELECTOR = ".CommentButton"
# 右键菜单 Download
CONTEXT_MENU_DOWNLOAD_SELECTOR = 'div.MenuItem:has-text("Download")'


def log(msg: str):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}")


def safe_filename(s: str, max_len: int = 80) -> str:
    """替换文件名非法字符，截断长度。"""
    s = (s or "").strip()
    for c in r'\/:*?"<>|':
        s = s.replace(c, "_")
    s = s.replace("\n", " ").replace("\r", "")
    if len(s) > max_len:
        s = s[:max_len]
    return s or "unnamed"


def text_preview(text: str, length: int = 30, default_empty: str = "无文本") -> str:
    """文本前 N 字符，用于命名；无则返回 default_empty（消息用 无文本，评论用 无文字）。"""
    t = (text or "").strip().replace("\n", " ")
    t = re.sub(r"\s+", " ", t)
    if not t:
        return default_empty
    return safe_filename(t[:length], length)


def date_to_timestamp(date_text: str) -> str:
    """将 'September 6, 2021' / 'January 17' 等转为 yyyymmdd 格式便于做目录名。"""
    raw = (date_text or "").strip()
    if not raw:
        return "unknown"
    try:
        # 带年份的格式
        for fmt in ("%B %d, %Y", "%b %d, %Y", "%Y-%m-%d", "%d.%m.%Y", "%B %d,%Y"):
            try:
                dt = datetime.strptime(raw, fmt)
                return dt.strftime("%Y%m%d")
            except ValueError:
                continue
        # 无年份（如 "January 17"）用当前年
        for fmt in ("%B %d", "%b %d"):
            try:
                dt = datetime.strptime(raw, fmt)
                dt = dt.replace(year=datetime.now().year)
                return dt.strftime("%Y%m%d")
            except ValueError:
                continue
    except Exception:
        pass
    return safe_filename(raw, 20)


async def goto_target_via_entry(page: Page) -> bool:
    """
    先打开入口页 ENTRY_URL，若有 Go to bottom 则点一下，
    再点击最后一个 group 的最后一个 message 的右箭头，跳转到 TARGET_URL，然后执行业务流程。
    返回是否成功跳转到目标页。
    """
    log("打开入口页...")
    await page.goto(ENTRY_URL, wait_until="domcontentloaded", timeout=60000)
    await page.wait_for_timeout(2000)
    # 若有 Go to bottom 按钮则点一下
    go_btn = page.locator(GO_TO_BOTTOM_SELECTOR).first
    try:
        if await go_btn.is_visible():
            await go_btn.click(timeout=1000)
            log("已点击 Go to bottom")
            await page.wait_for_timeout(1000)
    except Exception:
        pass
    # 点击最后一个 group 的最后一个 message 的右箭头
    container = page.locator(MESSAGE_LIST_SELECTOR).first
    try:
        await container.wait_for(state="visible", timeout=2000)
    except PlaywrightTimeoutError:
        log("未找到消息列表，尝试直接打开目标页")
        await page.goto(TARGET_URL, wait_until="domcontentloaded", timeout=2000)
        return True
    last_group = container.locator(DATE_GROUP_SELECTOR).last
    if await last_group.count() == 0:
        log("未找到任何日期组，尝试直接打开目标页")
        await page.goto(TARGET_URL, wait_until="domcontentloaded", timeout=60000)
        return True
    last_message = last_group.locator("div.message-list-item[data-message-id]").last
    if await last_message.count() == 0:
        log("未找到最后一条消息，尝试直接打开目标页")
        await page.goto(TARGET_URL, wait_until="domcontentloaded", timeout=60000)
        return True
    # 精确定位：只在「最后一个 group 的最后一个 message」内找，且排除空的 sticky 区，只点带 Focus message 的按钮（download_flag.html 1765-1772）
    right_arrow = last_message.locator(
        '.message-action-buttons:not(.message-action-button-sticky) button[aria-label="Focus message"]'
    ).first
    try:
        await right_arrow.wait_for(state="visible", timeout=3000)
        await right_arrow.hover()
        await page.wait_for_timeout(1000)
        await right_arrow.click()
        await page.wait_for_timeout(1000)
        if "-1001395144198" in page.url:
            log("已通过右箭头跳转到目标频道")
            return True
    except Exception as e:
        log(f"点击右箭头失败: {e}")
    log("尝试直接打开目标页")
    await page.goto(TARGET_URL, wait_until="domcontentloaded", timeout=60000)
    return True


async def scroll_once_to_load_more(page: Page, scroll_container_selector: str):
    """在消息列表内向上滚动一次以加载更多历史消息。"""
    container = page.locator(scroll_container_selector).first
    await container.evaluate("el => el.scrollTo(0, 0)")
    await page.wait_for_timeout(500)
    await container.evaluate("el => el.scrollBy(0, -800)")
    await page.wait_for_timeout(1200)


async def get_message_text_content(page: Page, msg_el) -> str:
    """提取消息内文本（排除 MessageMeta）。"""
    try:
        text_el = msg_el.locator(TEXT_CONTENT_SELECTOR).first
        if await text_el.count() == 0:
            return ""
        # 克隆节点，移除 MessageMeta 再取文本
        text = await text_el.evaluate("""
            el => {
                const clone = el.cloneNode(true);
                clone.querySelectorAll('.MessageMeta').forEach(n => n.remove());
                return (clone.textContent || '').trim();
            }
        """)
        return (text or "").strip()
    except Exception:
        return ""


async def get_group_date_text(page: Page, group_el) -> str:
    """获取日期组的时间戳文本。"""
    try:
        date_el = group_el.locator(STICKY_DATE_SELECTOR).first
        return (await date_el.inner_text()).strip() if await date_el.count() > 0 else ""
    except Exception:
        return ""


async def download_via_context_menu(page: Page, element_handle, download_dir: Path, timeout_ms: int = 60000) -> Optional[Path]:
    """
    在给定元素上右键 -> 点击 Download，等待下载完成，返回下载文件路径。
    若未出现 Download 菜单或下载失败则返回 None。
    """
    async with page.expect_download(timeout=timeout_ms) as download_info:
        await element_handle.click(button="right")
        await page.wait_for_timeout(400)
        download_btn = page.locator(CONTEXT_MENU_DOWNLOAD_SELECTOR).first
        try:
            await download_btn.wait_for(state="visible", timeout=3000)
            await download_btn.click()
        except Exception:
            # 关闭可能打开的菜单
            await page.keyboard.press("Escape")
            return None
    try:
        download: Download = await download_info.value
        path = await download.path()
        if path and Path(path).exists():
            return Path(path)
    except Exception as e:
        log(f"下载等待失败: {e}")
    return None


def move_download_to_group(download_path: Path, group_dir: Path, new_name: str) -> Optional[Path]:
    """将下载文件移动到组目录并重命名。"""
    if not download_path or not download_path.exists():
        return None
    group_dir.mkdir(parents=True, exist_ok=True)
    dest = group_dir / safe_filename(new_name)
    try:
        shutil.move(str(download_path), str(dest))
        return dest
    except Exception as e:
        log(f"移动文件失败 {download_path} -> {dest}: {e}")
        return None


async def process_file_message(page: Page, msg_el, message_id: str, text_pre: str, group_dir: Path):
    """处理文件类消息：右键 Download -> 等完成 -> 移动到组目录并重命名。"""
    file_block = msg_el.locator(FILE_MESSAGE_SELECTOR).first
    if await file_block.count() == 0:
        return
    try:
        title_el = msg_el.locator(FILE_TITLE_SELECTOR).first
        original_name = (await title_el.get_attribute("title")) or (await title_el.inner_text()) or "file"
        original_name = original_name.strip()
    except Exception:
        original_name = "file"
    ext = Path(original_name).suffix or ""
    new_name = f"{message_id}_{text_pre}_file_{original_name}"
    log(f"下载文件消息 {message_id}: {original_name}")
    await file_block.scroll_into_view_if_needed()
    await page.wait_for_timeout(300)
    got = await download_via_context_menu(page, file_block, DOWNLOAD_DIR)
    if got and got.exists():
        move_download_to_group(got, group_dir, new_name)
    await page.wait_for_timeout(500)


async def process_video_message(page: Page, msg_el, message_id: str, text_pre: str, group_dir: Path):
    """处理单条视频消息：下载视频 + 保存封面图（截图）。"""
    media_inner = msg_el.locator(VIDEO_MESSAGE_SELECTOR).first
    if await media_inner.count() == 0:
        return
    await media_inner.scroll_into_view_if_needed()
    await page.wait_for_timeout(300)
    # 1) 右键 Download 视频
    log(f"下载视频消息 {message_id}")
    got = await download_via_context_menu(page, media_inner, DOWNLOAD_DIR, timeout_ms=90000)
    if got and got.exists():
        # 下载的文件名通常为 video_YYYY-MM-DD_HH-MM-SS.mp4
        stem = got.stem
        new_name = f"{message_id}_{text_pre}_video_{stem}{got.suffix}"
        dest = move_download_to_group(got, group_dir, new_name)
        if dest:
            log(f"视频已保存: {dest.name}")
    # 2) 封面图：对 .media-inner 内 thumbnail 截图
    try:
        thumb = msg_el.locator(THUMBNAIL_SELECTOR).first
        if await thumb.count() > 0 and await thumb.is_visible():
            img_name = f"{message_id}_{text_pre}_video_image.png"
            dest_img = group_dir / safe_filename(img_name)
            group_dir.mkdir(parents=True, exist_ok=True)
            await thumb.screenshot(path=str(dest_img))
            log(f"视频封面已保存: {dest_img.name}")
    except Exception as e:
        log(f"视频封面截图失败: {e}")
    await page.wait_for_timeout(500)


async def process_album_item(page: Page, album_item_el, message_id: str, sub_id: str, text_pre: str, group_dir: Path):
    """处理相册中的一条：可能是视频或图片。"""
    # 视频：.media-inner video
    video = album_item_el.locator(VIDEO_TAG_SELECTOR).first
    has_video = await video.count() > 0
    if has_video:
        await album_item_el.scroll_into_view_if_needed()
        await page.wait_for_timeout(300)
        got = await download_via_context_menu(page, album_item_el, DOWNLOAD_DIR, timeout_ms=90000)
        if got and got.exists():
            stem = got.stem
            new_name = f"{message_id}_{text_pre}_{sub_id}_video_{stem}{got.suffix}"
            move_download_to_group(got, group_dir, new_name)
        try:
            thumb = album_item_el.locator(THUMBNAIL_SELECTOR).first
            if await thumb.count() > 0 and await thumb.is_visible():
                img_name = f"{message_id}_{text_pre}_{sub_id}_video_image.png"
                dest_img = group_dir / safe_filename(img_name)
                group_dir.mkdir(parents=True, exist_ok=True)
                await thumb.screenshot(path=str(dest_img))
        except Exception:
            pass
    else:
        # 图片：截图整块 media-inner 或其中的 img
        try:
            img_el = album_item_el.locator("img.full-media, img.thumbnail").first
            if await img_el.count() == 0:
                img_el = album_item_el.locator(".media-inner").first
            if await img_el.count() > 0 and await img_el.is_visible():
                img_name = f"{message_id}_{sub_id}_{text_pre}_img.png"
                dest_img = group_dir / safe_filename(img_name)
                group_dir.mkdir(parents=True, exist_ok=True)
                await img_el.screenshot(path=str(dest_img))
        except Exception:
            pass
    await page.wait_for_timeout(400)


async def process_album_message(page: Page, msg_el, message_id: str, text_pre: str, group_dir: Path):
    """处理图文集（相册）：遍历每个 album-media-message-XXX。"""
    album = msg_el.locator(ALBUM_SELECTOR).first
    if await album.count() == 0:
        return
    items = await msg_el.locator(ALBUM_ITEM_SELECTOR).all()
    for item in items:
        try:
            id_attr = await item.get_attribute("id")
            if id_attr and id_attr.startswith("album-media-message-"):
                sub_id = id_attr.replace("album-media-message-", "")
                await process_album_item(page, item, message_id, sub_id, text_pre, group_dir)
        except Exception as e:
            log(f"相册子项处理失败: {e}")


async def save_text_message(msg_el, message_id: str, text_pre: str, group_dir: Path, text: str):
    """保存纯文本或带文本消息到 messageid_textpre_text.txt。"""
    group_dir.mkdir(parents=True, exist_ok=True)
    name = f"{message_id}_{text_pre}_text.txt"
    path = group_dir / safe_filename(name)
    path.write_text(text or "", encoding="utf-8")


async def process_comments(page: Page, msg_el, message_id: str, text_pre: str, group_dir: Path):
    """
    若消息有评论按钮，点击进入讨论，解析评论组（message-group），
    保存评论文本与评论内媒体；然后关闭讨论返回。
    """
    comment_btn = msg_el.locator(COMMENT_BUTTON_SELECTOR).first
    if await comment_btn.count() == 0:
        return
    try:
        count_el = comment_btn.locator(".label").first
        count_text = (await count_el.inner_text()).strip() if await count_el.count() > 0 else "0"
        # 取数字部分，如 "7 comments" -> "7"
        m = re.search(r"\d+", count_text)
        count_text = m.group(0) if m else "0"
    except Exception:
        count_text = "0"
    await comment_btn.scroll_into_view_if_needed()
    await page.wait_for_timeout(300)
    await comment_btn.click()
    await page.wait_for_timeout(2000)
    # 讨论区可能在右侧或弹层，评论在 .sender-group-container 或 [id^='message-group-'] 下
    try:
        groups = await page.locator(".sender-group-container, [id^='message-group-']").all()
        comments_text = []
        for g in groups:
            try:
                msg_div = g.locator("[id^='message-'][data-message-id]").first
                if await msg_div.count() == 0:
                    continue
                mid = await msg_div.get_attribute("data-message-id") or ""
                sender_el = g.locator(".sender-title, .message-title-name").first
                sender = (await sender_el.inner_text()).strip() if await sender_el.count() > 0 else ""
                text = await get_message_text_content(page, msg_div)
                cmt_pre = text_preview(text, 30, default_empty="无文字")
                comments_text.append(f"[{sender}] {text}")
                # 评论内相册/视频：简化处理，仅下载可见媒体
                album = g.locator(ALBUM_SELECTOR).first
                if await album.count() > 0:
                    items = await g.locator(ALBUM_ITEM_SELECTOR).all()
                    for it in items:
                        try:
                            aid = await it.get_attribute("id") or ""
                            sub_id = aid.replace("album-media-message-", "") if aid else ""
                            video = it.locator(VIDEO_TAG_SELECTOR).first
                            if await video.count() > 0:
                                await it.scroll_into_view_if_needed()
                                await page.wait_for_timeout(200)
                                got = await download_via_context_menu(page, it, DOWNLOAD_DIR, timeout_ms=60000)
                                if got and got.exists():
                                    stem = got.stem
                                    new_name = f"{message_id}_{text_pre}_comments_{cmt_pre}_{mid}_video_{stem}{got.suffix}"
                                    move_download_to_group(got, group_dir, new_name)
                                thumb = it.locator(THUMBNAIL_SELECTOR).first
                                if await thumb.count() > 0 and await thumb.is_visible():
                                    img_name = f"{message_id}_{text_pre}_comments_{cmt_pre}_{mid}_video_img.png"
                                    group_dir.mkdir(parents=True, exist_ok=True)
                                    await thumb.screenshot(path=str(group_dir / safe_filename(img_name)))
                            else:
                                img_el = it.locator("img.full-media, img.thumbnail, .media-inner").first
                                if await img_el.count() > 0 and await img_el.is_visible():
                                    img_name = f"{message_id}_{text_pre}_comments_{cmt_pre}_{mid}_img.png"
                                    group_dir.mkdir(parents=True, exist_ok=True)
                                    await img_el.screenshot(path=str(group_dir / safe_filename(img_name)))
                        except Exception:
                            pass
            except Exception as e:
                log(f"评论项解析失败: {e}")
        if comments_text:
            group_dir.mkdir(parents=True, exist_ok=True)
            comment_file = group_dir / safe_filename(f"{message_id}_{text_pre}_comments_{count_text}.txt")
            comment_file.write_text("\n\n".join(comments_text), encoding="utf-8")
    except Exception as e:
        log(f"处理评论失败: {e}")
    # 关闭讨论：点击返回或关闭按钮
    await page.wait_for_timeout(500)
    back_btn = page.locator(".Button.back-button, [aria-label='Back'], .Header .Button").first
    if await back_btn.count() > 0:
        try:
            await back_btn.click()
            await page.wait_for_timeout(1000)
        except Exception:
            pass


async def process_one_message(page: Page, msg_el, group_dir: Path):
    """根据消息类型分发：文件 / 视频 / 相册 / 文本 / 评论。"""
    message_id = (await msg_el.get_attribute("data-message-id")) or (await msg_el.get_attribute("id") or "").replace("message-", "")
    if not message_id:
        return
    text = await get_message_text_content(page, msg_el)
    text_pre = text_preview(text, 30)
    # 跳过系统消息等
    if "ActionMessage" in (await msg_el.get_attribute("class") or ""):
        return
    # 1) 文件
    if await msg_el.locator(FILE_MESSAGE_SELECTOR).first.count() > 0:
        await process_file_message(page, msg_el, message_id, text_pre, group_dir)
    # 2) 单条视频（非相册）
    elif await msg_el.locator(ALBUM_SELECTOR).first.count() == 0 and await msg_el.locator(VIDEO_MESSAGE_SELECTOR).first.count() > 0:
        await process_video_message(page, msg_el, message_id, text_pre, group_dir)
    # 3) 相册
    elif await msg_el.locator(ALBUM_SELECTOR).first.count() > 0:
        await process_album_message(page, msg_el, message_id, text_pre, group_dir)
    # 4) 文本（含仅有文本的消息）
    if text or await msg_el.locator(FILE_MESSAGE_SELECTOR).first.count() == 0:
        await save_text_message(msg_el, message_id, text_pre, group_dir, text)
    # 5) 评论
    await process_comments(page, msg_el, message_id, text_pre, group_dir)


async def run_once(page: Page, max_groups: Optional[int] = None, scroll_times: int = 5):
    """
    先处理当前可见的消息组（下载完），再滚动加载更多；循环直到滚动次数用完或达到 max_groups。
    """
    scroll_parent = MESSAGE_LIST_ROOT_SELECTOR
    container = page.locator(MESSAGE_LIST_SELECTOR).first
    # 等到至少出现一个 .message-date-group，否则 all() 会得到空（参考 groups查找方案.md）
    try:
        await container.locator(DATE_GROUP_SELECTOR).first.wait_for(state="attached", timeout=15000)
    except PlaywrightTimeoutError:
        log("未等到任何 message-date-group，请确认已打开目标频道且消息已加载")
        return
    # 调试：确认选中的是带 message-date-group 的容器（参考 groups查找方案.md）
    log(f"msglist_root_count={await page.locator(MESSAGE_LIST_ROOT_SELECTOR).count()}")
    log(f"container_count={await page.locator(MESSAGE_LIST_SELECTOR).count()}")
    log(f"date_group_count={await page.locator(MESSAGE_LIST_SELECTOR + ' ' + DATE_GROUP_SELECTOR).count()}")
    processed_group_ids: set[str] = set()
    total_group_index = 0
    for scroll_round in range(scroll_times):
        groups = await container.locator(DATE_GROUP_SELECTOR).all()
        log(f"round={scroll_round} groups={len(groups)}")
        for group_el in groups:
            if max_groups is not None and total_group_index >= max_groups:
                break
            # 用组内第一条消息的 data-message-id 作为组唯一标识，避免滚动后重复处理
            first_msg = group_el.locator("div.message-list-item[data-message-id]").first
            if await first_msg.count() == 0:
                continue
            group_id = await first_msg.get_attribute("data-message-id") or ""
            if group_id in processed_group_ids:
                continue
            processed_group_ids.add(group_id)
            date_text = await get_group_date_text(page, group_el)
            ts = date_to_timestamp(date_text) or "unknown"
            group_dir = OUT_BASE / f"group_{ts}_{total_group_index}"
            group_dir.mkdir(parents=True, exist_ok=True)
            log(f"处理组 [{total_group_index}] {date_text} -> {group_dir.name}")
            messages = await group_el.locator("div.message-list-item[data-message-id]").all()
            for msg_el in messages:
                try:
                    await process_one_message(page, msg_el, group_dir)
                except Exception as e:
                    log(f"消息处理异常: {e}")
            total_group_index += 1
        if max_groups is not None and total_group_index >= max_groups:
            break
        # 当前可见组都处理完后，再滚动加载更多
        await scroll_once_to_load_more(page, scroll_parent)
        await page.wait_for_timeout(1000)
    log(f"共处理 {total_group_index} 个日期组")


def parse_args():
    parser = argparse.ArgumentParser(description="Telegram Web 频道消息与媒体下载")
    parser.add_argument(
        "--max-groups",
        type=int,
        default=None,
        metavar="N",
        help="最多处理的日期组数量，不传则处理全部（例: --max-groups 2）",
    )
    parser.add_argument(
        "--scroll-times",
        type=int,
        default=5,
        metavar="N",
        help="向上滚动加载更多消息的次数（默认 5）",
    )
    return parser.parse_args()




async def main(max_groups: Optional[int], scroll_times: int):
    OUT_BASE.mkdir(parents=True, exist_ok=True)
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    # 使用 edge_util 获取 page（与 create_book_起点.py 一致）
    page, release = await get_page(
        USER_DATA_DIR,
        headless=False,
        channel="msedge",
    )
    try:
        log("打开 Telegram Web...")
        await goto_target_via_entry(page)
        await page.wait_for_timeout(2000)
        await run_once(page, max_groups=max_groups, scroll_times=scroll_times)
    finally:
        await page.wait_for_timeout(2000)
        if page:
            await page.close()
        await release()


if __name__ == "__main__":
    args = parse_args()
    asyncio.run(main(max_groups=args.max_groups, scroll_times=args.scroll_times))
