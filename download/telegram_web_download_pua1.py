"""
Telegram Web 频道消息与媒体采集（Playwright）

目标：把目标频道里每条消息的全量文本、堆叠的图片/文档/视频都采集下来，
按"发送方组"（sender-group: first-in-group / last-in-group 标记的连续消息）划分输出目录。

运行步骤：
  1. pip install -r requirements.txt && playwright install chromium
  2. 先用 Chromium 打开 https://web.telegram.org 登录 Telegram（持久化 profile）。
     默认 profile：D:\\UserData\\.playwright_user_data--claude
     如果该 profile 已被另一个 Chromium 占用（例如 playwright MCP），请先关闭它。
  3. 运行：python telegram_web_download_pua1.py [--target #-1001395144198] [--max-groups N]

输出目录布局：
  output/<channel_slug>/
    group_<yyyymmdd>_<sgseq>__<first_msg_id>/
      <msg_id>__<text_pre>__text.txt
      <msg_id>__<text_pre>__raw.html
      <msg_id>__<text_pre>__video.mp4
      <msg_id>__<text_pre>__video_thumb.png
      <msg_id>__<text_pre>__file_<原名>
      <msg_id>__<text_pre>__photo.png
      <msg_id>__<text_pre>__album_<sub_id>__video.mp4
      <msg_id>__<text_pre>__album_<sub_id>__photo.png
    manifest.jsonl     （逐条消息的处理记录）
    _channel_summary.txt （频道信息）
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from playwright.async_api import (
    Download,
    ElementHandle,
    Locator,
    Page,
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)

SCRIPT_DIR = Path(__file__).resolve().parent

# -------- 默认参数 --------
DEFAULT_USER_DATA_DIR = Path(r"D:\UserData\.playwright_user_data--claude")
DEFAULT_OUT_BASE = SCRIPT_DIR / "output"
DEFAULT_DOWNLOAD_DIR = Path(r"D:\Download")
DEFAULT_TARGET = "#-1001395144198"  # 恋爱心理学 追爱脱单 视频教程

# -------- Telegram Web Selectors --------
MESSAGE_LIST_ROOT = ".Transition_slide-active div.Transition.MessageList:has(div.messages-container)"
MESSAGE_LIST = f"{MESSAGE_LIST_ROOT} div.messages-container"
DATE_GROUP = ".message-date-group"
STICKY_DATE = ".sticky-date span"
MSG_ITEM = "div.message-list-item[data-message-id]"

FILE_BLOCK = ".File.interactive"
FILE_TITLE = ".file-title"
MEDIA_INNER = ".media-inner.interactive"
THUMBNAIL = "canvas.thumbnail, img.thumbnail"
FULL_MEDIA_IMG = "img.full-media"
ALBUM = ".Album"
ALBUM_ITEM = "[id^='album-media-message-']"
TEXT_CONTENT = ".text-content"
WEBPAGE = ".WebPage"
COMMENT_BUTTON = ".CommentButton"

CONTEXT_MENU_ROOT = ".Menu.MessageContextMenu, .Menu.in-portal.MessageContextMenu"
CONTEXT_MENU_ITEM = f"{CONTEXT_MENU_ROOT} .MenuItem"


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")


def safe_filename(s: str, max_len: int = 80) -> str:
    s = (s or "").strip()
    for c in r'\\/:*?"<>|':
        s = s.replace(c, "_")
    s = s.replace("\n", " ").replace("\r", "")
    s = re.sub(r"\s+", " ", s).strip()
    if len(s) > max_len:
        s = s[:max_len].rstrip()
    return s or "unnamed"


def text_preview(text: str, length: int = 30, default_empty: str = "无文本") -> str:
    t = re.sub(r"\s+", " ", (text or "").strip())
    if not t:
        return default_empty
    return safe_filename(t[:length], length)


def date_to_yyyymmdd(date_text: str) -> str:
    raw = (date_text or "").strip()
    if not raw:
        return "unknown"
    for fmt in ("%B %d, %Y", "%b %d, %Y", "%Y-%m-%d", "%d.%m.%Y", "%B %d,%Y"):
        try:
            return datetime.strptime(raw, fmt).strftime("%Y%m%d")
        except ValueError:
            continue
    for fmt in ("%B %d", "%b %d"):
        try:
            dt = datetime.strptime(raw, fmt).replace(year=datetime.now().year)
            return dt.strftime("%Y%m%d")
        except ValueError:
            continue
    # Today / Yesterday / weekday — use current date best-effort
    lower = raw.lower()
    if lower == "today":
        return datetime.now().strftime("%Y%m%d")
    return safe_filename(raw, 20)


def slugify(s: str, max_len: int = 60) -> str:
    s = (s or "channel").strip()
    s = re.sub(r"[\\/:*?\"<>|\n\r\t]+", "_", s)
    s = re.sub(r"\s+", "_", s)
    return s[:max_len] or "channel"


# ---------------- DOM 抽取（评估 JS，一次拿全） ----------------

EXTRACT_MESSAGES_JS = """
() => {
  const out = { containerFound: false, firstVisibleId: null, lastVisibleId: null, dateGroups: [] };
  const dateGroups = document.querySelectorAll('.Transition_slide-active .message-date-group');
  if (!dateGroups.length) return out;
  out.containerFound = true;
  for (const dg of dateGroups) {
    const dateText = dg.querySelector('.sticky-date span')?.innerText || '';
    const msgs = dg.querySelectorAll('div.message-list-item[data-message-id]');
    const items = [];
    msgs.forEach(m => {
      const id = m.getAttribute('data-message-id');
      const cls = m.className || '';
      const contentClass = m.querySelector('.message-content')?.className || '';
      // 提取文本：clone & remove MessageMeta & WebPage description（avoid mixing link-preview meta into the user text）
      let text = '';
      const tc = m.querySelector('.text-content');
      if (tc) {
        const clone = tc.cloneNode(true);
        clone.querySelectorAll('.MessageMeta, .message-time, .message-views').forEach(n => n.remove());
        text = (clone.innerText || '').trim();
      }
      // 文件名
      let fileName = '';
      const ft = m.querySelector('.File .file-title');
      if (ft) fileName = (ft.getAttribute('title') || ft.innerText || '').trim();
      // 相册子项
      const albumItems = [];
      m.querySelectorAll("[id^='album-media-message-']").forEach(it => {
        const subId = (it.getAttribute('id') || '').replace('album-media-message-','');
        const hasVideo = !!it.querySelector('video, .media-inner.interactive');
        const hasImg = !!it.querySelector('img.full-media, img.thumbnail, canvas.thumbnail');
        albumItems.push({ subId, hasVideo, hasImg });
      });
      items.push({
        id,
        cls,
        contentClass,
        isFirstInGroup: cls.includes('first-in-group'),
        isLastInGroup: cls.includes('last-in-group'),
        isActionMessage: cls.includes('ActionMessage'),
        hasFile: !!m.querySelector('.File.interactive'),
        fileName,
        hasMediaInner: !!m.querySelector('.media-inner.interactive'),
        hasAlbum: albumItems.length > 0,
        albumItems,
        hasWebPage: !!m.querySelector('.WebPage'),
        hasComments: !!m.querySelector('.CommentButton'),
        text,
      });
    });
    if (items.length) out.dateGroups.push({ dateText, items });
  }
  // first / last visible message ids
  const all = document.querySelectorAll('.Transition_slide-active .message-list-item[data-message-id]');
  if (all.length) {
    out.firstVisibleId = all[0].getAttribute('data-message-id');
    out.lastVisibleId = all[all.length-1].getAttribute('data-message-id');
  }
  return out;
}
"""


# ---------------- 工具：右键下载 ----------------


async def _close_any_open_menu(page: Page) -> None:
    """关闭任何已开的 context menu。"""
    try:
        cnt = await page.locator(CONTEXT_MENU_ROOT).count()
        if cnt > 0:
            await page.keyboard.press("Escape")
            await page.wait_for_timeout(150)
            await page.mouse.click(5, 5)
            await page.wait_for_timeout(100)
    except Exception:
        pass


async def right_click_download(
    page: Page,
    locator: Locator,
    timeout_ms: int = 90000,
    max_attempts: int = 3,
) -> Optional[Download]:
    """
    在元素上右键，等待菜单出现，找 Download 项，再用 expect_download 包裹点击。
    若菜单没有 Download 项则按 Esc 关闭并返回 None。
    """
    for attempt in range(max_attempts):
        await _close_any_open_menu(page)
        try:
            await page.bring_to_front()
        except Exception:
            pass
        try:
            await locator.scroll_into_view_if_needed(timeout=5000)
        except Exception:
            pass
        await page.wait_for_timeout(500)
        try:
            await locator.hover(timeout=5000)
            await page.wait_for_timeout(150)
        except Exception:
            pass
        try:
            await locator.click(button="right", force=True, timeout=5000)
        except Exception as e:
            log(f"  右键失败 (attempt {attempt+1}): {e}")
            continue
        # 等菜单出现
        menu_root = page.locator(CONTEXT_MENU_ROOT).first
        try:
            await menu_root.wait_for(state="visible", timeout=4000)
        except PlaywrightTimeoutError:
            log(f"  右键菜单未出现 (attempt {attempt+1})")
            # 尝试用 JS dispatch contextmenu event 兜底
            try:
                await locator.evaluate(
                    """el => {
                        const r = el.getBoundingClientRect();
                        const ev = new MouseEvent('contextmenu', {
                            bubbles: true, cancelable: true, view: window,
                            button: 2, buttons: 2,
                            clientX: r.left + r.width/2, clientY: r.top + r.height/2
                        });
                        el.dispatchEvent(ev);
                    }"""
                )
                await menu_root.wait_for(state="visible", timeout=3000)
            except Exception:
                continue
        # 找 Download 项
        download_item = page.locator(f"{CONTEXT_MENU_ITEM}:has-text('Download')").first
        try:
            if not await download_item.is_visible():
                await _close_any_open_menu(page)
                log("  菜单中无 Download 项")
                return None
        except Exception:
            await _close_any_open_menu(page)
            return None
        # 用 expect_download 包裹点击
        try:
            async with page.expect_download(timeout=timeout_ms) as di:
                await download_item.click()
            return await di.value
        except Exception as e:
            log(f"  下载等待失败 (attempt {attempt+1}): {e}")
            await _close_any_open_menu(page)
            continue
    return None


async def save_download(download: Download, dest: Path) -> Optional[Path]:
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        await download.save_as(str(dest))
        return dest if dest.exists() else None
    except Exception as e:
        log(f"  save_as 失败: {e}")
        try:
            src = await download.path()
            if src and Path(src).exists():
                shutil.copy2(str(src), str(dest))
                return dest
        except Exception:
            pass
        return None


# ---------------- 单条消息处理 ----------------


async def process_message(
    page: Page,
    msg_data: dict,
    msg_locator: Locator,
    group_dir: Path,
    manifest_fp,
    skip_existing: bool,
    only_kinds: Optional[set[str]] = None,
    skip_kinds: Optional[set[str]] = None,
) -> dict:
    """
    msg_data 是 JS 抽取出来的字典，msg_locator 是页面定位器。返回处理结果记录。
    """
    msg_id = msg_data["id"]
    text = msg_data.get("text", "") or ""
    text_pre = text_preview(text, 30)
    base = f"{msg_id}__{text_pre}"
    record: dict[str, Any] = {
        "msg_id": msg_id,
        "text_pre": text_pre,
        "kinds": [],
        "saved": [],
        "errors": [],
        "skipped": False,
    }

    if msg_data.get("isActionMessage"):
        record["kinds"].append("action")
        return record

    # 检查跳过：组目录里已经有 msg_id__ 开头的非 text/html 文件 → 媒体已下载，仍重写文本
    existing_media = list(group_dir.glob(f"{msg_id}__*")) if group_dir.exists() else []
    has_media_files = any(
        not (p.name.endswith("__text.txt") or p.name.endswith("__raw.html"))
        for p in existing_media
    )

    # 保存原始 HTML（重型证据）
    raw_html_path = group_dir / safe_filename(f"{base}__raw.html")
    if not raw_html_path.exists():
        try:
            html = await msg_locator.evaluate("el => el.outerHTML")
            group_dir.mkdir(parents=True, exist_ok=True)
            raw_html_path.write_text(html or "", encoding="utf-8")
        except Exception as e:
            record["errors"].append(f"raw.html: {e}")

    # 保存文本
    text_path = group_dir / safe_filename(f"{base}__text.txt")
    try:
        group_dir.mkdir(parents=True, exist_ok=True)
        text_path.write_text(text, encoding="utf-8")
    except Exception as e:
        record["errors"].append(f"text.txt: {e}")

    # 媒体已存且 skip_existing → 跳过媒体下载
    if skip_existing and has_media_files:
        record["skipped"] = True
        record["kinds"].append("media-skipped")
        return record

    def kind_enabled(k: str) -> bool:
        if only_kinds is not None and k not in only_kinds:
            return False
        if skip_kinds is not None and k in skip_kinds:
            return False
        return True

    # 1) 文件
    if msg_data.get("hasFile"):
        record["kinds"].append("file")
        if kind_enabled("file"):
            file_block = msg_locator.locator(FILE_BLOCK).first
            if await file_block.count() > 0:
                original = msg_data.get("fileName") or "file"
                ext = Path(original).suffix
                stem = Path(original).stem
                dest = group_dir / safe_filename(f"{base}__file_{stem}{ext}", 120)
                log(f"  → file: {original}")
                dl = await right_click_download(page, file_block, timeout_ms=120000)
                if dl:
                    saved = await save_download(dl, dest)
                    if saved:
                        record["saved"].append(saved.name)
                    else:
                        record["errors"].append("file-save-failed")
                else:
                    record["errors"].append("file-no-download")

    # 2) 相册
    if msg_data.get("hasAlbum"):
        record["kinds"].append("album")
        if kind_enabled("album"):
            items = msg_data.get("albumItems") or []
            for ai in items:
                sub_id = ai.get("subId") or "x"
                item_loc = msg_locator.locator(f"#album-media-message-{sub_id}")
                if await item_loc.count() == 0:
                    continue
                try:
                    await item_loc.scroll_into_view_if_needed()
                except Exception:
                    pass
                if ai.get("hasVideo"):
                    dest = group_dir / safe_filename(f"{base}__album_{sub_id}__video.mp4")
                    log(f"  → album video sub={sub_id}")
                    dl = await right_click_download(page, item_loc, timeout_ms=120000)
                    if dl:
                        suggested = dl.suggested_filename or ""
                        sfx = Path(suggested).suffix or ".mp4"
                        dest = group_dir / safe_filename(f"{base}__album_{sub_id}__video{sfx}")
                        saved = await save_download(dl, dest)
                        if saved:
                            record["saved"].append(saved.name)
                    else:
                        record["errors"].append(f"album-video-{sub_id}-no-download")
                # 截图缩略图作为图片
                try:
                    thumb = item_loc.locator("img.full-media, img.thumbnail, canvas.thumbnail").first
                    if await thumb.count() > 0 and await thumb.is_visible():
                        img_dest = group_dir / safe_filename(f"{base}__album_{sub_id}__photo.png")
                        if not img_dest.exists():
                            group_dir.mkdir(parents=True, exist_ok=True)
                            await thumb.screenshot(path=str(img_dest))
                            record["saved"].append(img_dest.name)
                except Exception as e:
                    record["errors"].append(f"album-photo-{sub_id}: {e}")

    # 3) 单条视频 / media（非相册）
    if msg_data.get("hasMediaInner") and not msg_data.get("hasAlbum") and not msg_data.get("hasWebPage"):
        record["kinds"].append("video")
        if kind_enabled("video"):
            media = msg_locator.locator(MEDIA_INNER).first
            if await media.count() > 0:
                log(f"  → video msg={msg_id}")
                dl = await right_click_download(page, media, timeout_ms=180000)
                if dl:
                    suggested = dl.suggested_filename or ""
                    sfx = Path(suggested).suffix or ".mp4"
                    dest = group_dir / safe_filename(f"{base}__video{sfx}")
                    saved = await save_download(dl, dest)
                    if saved:
                        record["saved"].append(saved.name)
                    else:
                        record["errors"].append("video-save-failed")
                else:
                    record["errors"].append("video-no-download")
            # 视频封面截图
            try:
                thumb = media.locator("canvas.thumbnail, img.thumbnail").first
                if await thumb.count() > 0 and await thumb.is_visible():
                    img_dest = group_dir / safe_filename(f"{base}__video_thumb.png")
                    if not img_dest.exists():
                        await thumb.screenshot(path=str(img_dest))
                        record["saved"].append(img_dest.name)
            except Exception as e:
                record["errors"].append(f"video-thumb: {e}")

    # 4) 网页预览的图片：截图保存
    if msg_data.get("hasWebPage"):
        record["kinds"].append("webpage")
        try:
            preview = msg_locator.locator(f"{WEBPAGE} .media-inner").first
            if await preview.count() > 0 and await preview.is_visible():
                img_dest = group_dir / safe_filename(f"{base}__webpage_preview.png")
                if not img_dest.exists():
                    await preview.screenshot(path=str(img_dest))
                    record["saved"].append(img_dest.name)
        except Exception as e:
            record["errors"].append(f"webpage: {e}")

    # 标记纯文本
    if not record["kinds"]:
        record["kinds"].append("text-only")

    return record


# ---------------- 滚动加载历史 ----------------

async def scroll_up_and_wait(page: Page, prev_first_id: Optional[str], max_wait_ms: int = 6000) -> tuple[bool, Optional[str]]:
    """
    向上滚动一次，等待 first visible msg id 变化。返回 (是否加载到新消息, 新 first id)。
    """
    js = """
    () => {
      const ml = document.querySelector('.Transition_slide-active div.Transition.MessageList');
      if (!ml) return null;
      ml.scrollTop = 0;
      return { st: ml.scrollTop, sh: ml.scrollHeight };
    }
    """
    await page.evaluate(js)
    # 轮询直到 first id 变化或超时
    deadline = asyncio.get_event_loop().time() + max_wait_ms / 1000
    new_first = prev_first_id
    while asyncio.get_event_loop().time() < deadline:
        await page.wait_for_timeout(400)
        cur = await page.evaluate(
            "() => document.querySelector('.Transition_slide-active .message-list-item[data-message-id]')?.getAttribute('data-message-id') || null"
        )
        if cur and cur != prev_first_id:
            new_first = cur
            return True, new_first
    return False, new_first


# ---------------- 主流程 ----------------

async def open_target_channel(page: Page, target_hash: str) -> bool:
    """
    打开 https://web.telegram.org/a/ ，在 sidebar 中点击匹配 target_hash 的会话。
    """
    log(f"打开 Telegram Web，定位频道 {target_hash} ...")
    await page.goto("https://web.telegram.org/a/", wait_until="domcontentloaded", timeout=60000)
    await page.wait_for_timeout(3000)
    # 等待 chat-list 出现
    try:
        await page.wait_for_selector(f"a[href='{target_hash}']", timeout=15000)
    except PlaywrightTimeoutError:
        log("sidebar 中未找到目标会话链接，尝试直接 hash 跳转")
    # 优先 sidebar 点击
    link = page.locator(f"a[href='{target_hash}']").first
    if await link.count() > 0:
        try:
            await link.click()
            await page.wait_for_timeout(2000)
        except Exception as e:
            log(f"sidebar 点击失败: {e}")
    # 兜底：location.hash
    if target_hash not in (await page.evaluate("() => location.hash")):
        await page.evaluate(f"() => location.hash = '{target_hash}'")
        await page.wait_for_timeout(2000)
    # 等待 message list 出现
    try:
        await page.locator(MESSAGE_LIST).first.wait_for(state="visible", timeout=20000)
    except PlaywrightTimeoutError:
        log("消息列表未加载")
        return False
    # 等至少 1 个 date group
    try:
        await page.locator(f"{MESSAGE_LIST} {DATE_GROUP}").first.wait_for(state="attached", timeout=20000)
    except PlaywrightTimeoutError:
        log("未等到 date group")
        return False
    log(f"频道打开成功：{await page.title()}")
    return True


async def run(args) -> None:
    target_hash = args.target
    out_base = Path(args.out_base)
    user_data_dir = Path(args.user_data_dir)
    only_kinds = set(s.strip() for s in (args.only_kinds or "").split(",") if s.strip()) or None
    skip_kinds = set(s.strip() for s in (args.skip_kinds or "").split(",") if s.strip()) or None
    start_msg_id = args.start_msg_id

    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(
            user_data_dir=str(user_data_dir),
            headless=args.headless,
            channel=args.browser_channel if args.browser_channel else None,
            accept_downloads=True,
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        try:
            ok = await open_target_channel(page, target_hash)
            if not ok:
                log("打开目标频道失败，退出")
                return

            channel_title = await page.title()
            channel_slug = slugify(channel_title)
            out_dir = out_base / channel_slug
            out_dir.mkdir(parents=True, exist_ok=True)
            manifest_path = out_dir / "manifest.jsonl"
            summary_path = out_dir / "_channel_summary.txt"
            summary_path.write_text(
                f"channel_title={channel_title}\ntarget_hash={target_hash}\nstarted={datetime.now().isoformat()}\n",
                encoding="utf-8",
            )
            log(f"输出目录：{out_dir}")

            processed_ids: set[str] = set()
            global_sg_seq = 0  # 全局 sender-group 序号
            prev_first_id: Optional[str] = None
            no_change_rounds = 0

            with manifest_path.open("a", encoding="utf-8") as manifest_fp:
                for round_idx in range(args.scroll_max):
                    # 抽取当前 DOM
                    data = await page.evaluate(EXTRACT_MESSAGES_JS)
                    if not data.get("containerFound"):
                        log("DOM 中没有 date-group，等待 …")
                        await page.wait_for_timeout(1000)
                        continue

                    first_id = data.get("firstVisibleId")
                    last_id = data.get("lastVisibleId")
                    log(
                        f"round={round_idx} groups={len(data['dateGroups'])} "
                        f"first={first_id} last={last_id}"
                    )

                    # 遍历 date group → sender-group → 消息
                    current_sg: list[dict] = []  # 累积 sender-group 内的消息

                    for dg in data["dateGroups"]:
                        date_text = dg.get("dateText", "")
                        yyyymmdd = date_to_yyyymmdd(date_text)
                        for msg in dg["items"]:
                            msg_id = msg["id"]
                            # 新组开始
                            if msg.get("isFirstInGroup"):
                                current_sg = []
                            current_sg.append({"msg": msg, "yyyymmdd": yyyymmdd, "date_text": date_text})
                            # 组结束 → 处理
                            if msg.get("isLastInGroup"):
                                # start_msg_id 过滤：如果整组的所有 msg_id 都 > start_msg_id 则跳过
                                sg_min_id = min(int(s["msg"]["id"]) for s in current_sg if s["msg"]["id"].isdigit())
                                if start_msg_id is None or sg_min_id <= start_msg_id:
                                    await process_sender_group(
                                        page, current_sg, out_dir, manifest_fp,
                                        global_sg_seq, processed_ids, args.skip_existing, args.dry_run,
                                        only_kinds, skip_kinds,
                                    )
                                global_sg_seq += 1
                                current_sg = []

                            if args.max_groups is not None and global_sg_seq >= args.max_groups:
                                break
                        if args.max_groups is not None and global_sg_seq >= args.max_groups:
                            break

                    if args.max_groups is not None and global_sg_seq >= args.max_groups:
                        log(f"达到 max-groups={args.max_groups}，停止")
                        break

                    # 滚动加载更多
                    changed, new_first = await scroll_up_and_wait(page, first_id)
                    if not changed:
                        no_change_rounds += 1
                        if no_change_rounds >= 3:
                            log("连续多轮无新消息加载，应该已到顶")
                            break
                    else:
                        no_change_rounds = 0
                    prev_first_id = new_first

            log(f"完成。共处理 sender-group={global_sg_seq}，msg={len(processed_ids)}")
        finally:
            try:
                await ctx.close()
            except Exception:
                pass


async def process_sender_group(
    page: Page,
    sg_msgs: list[dict],
    out_dir: Path,
    manifest_fp,
    sg_seq: int,
    processed_ids: set[str],
    skip_existing: bool,
    dry_run: bool,
    only_kinds: Optional[set[str]] = None,
    skip_kinds: Optional[set[str]] = None,
) -> None:
    if not sg_msgs:
        return
    first = sg_msgs[0]
    first_msg_id = first["msg"]["id"]
    if first_msg_id in processed_ids:
        return
    yyyymmdd = first["yyyymmdd"]
    group_dir = out_dir / safe_filename(f"group_{yyyymmdd}_{sg_seq:04d}__{first_msg_id}")
    log(f"sender-group seq={sg_seq} date={first['date_text']} first_msg={first_msg_id} -> {group_dir.name}")
    if dry_run:
        for s in sg_msgs:
            processed_ids.add(s["msg"]["id"])
            kinds = []
            if s["msg"].get("isActionMessage"): kinds.append("action")
            if s["msg"].get("hasFile"): kinds.append("file")
            if s["msg"].get("hasAlbum"): kinds.append("album")
            if s["msg"].get("hasMediaInner") and not s["msg"].get("hasAlbum"): kinds.append("video")
            if s["msg"].get("hasWebPage"): kinds.append("webpage")
            if not kinds: kinds.append("text-only")
            manifest_fp.write(json.dumps({
                "ts": datetime.now().isoformat(),
                "sg_seq": sg_seq,
                "msg_id": s["msg"]["id"],
                "dry_run": True,
                "kinds": kinds,
                "text_len": len(s["msg"].get("text") or ""),
            }, ensure_ascii=False) + "\n")
        manifest_fp.flush()
        return
    group_dir.mkdir(parents=True, exist_ok=True)
    # 组合 _combined.txt 记录所有消息的纯文本顺序
    combined_lines: list[str] = []
    for s in sg_msgs:
        msg = s["msg"]
        msg_id = msg["id"]
        if msg_id in processed_ids:
            continue
        loc = page.locator(f"{MESSAGE_LIST_ROOT} #message-{msg_id}").first
        if await loc.count() == 0:
            log(f"  消息 {msg_id} 不在 DOM 中，跳过")
            continue
        record = await process_message(page, msg, loc, group_dir, manifest_fp, skip_existing, only_kinds, skip_kinds)
        record["sg_seq"] = sg_seq
        record["date"] = s["date_text"]
        record["yyyymmdd"] = yyyymmdd
        record["ts"] = datetime.now().isoformat()
        manifest_fp.write(json.dumps(record, ensure_ascii=False) + "\n")
        manifest_fp.flush()
        processed_ids.add(msg_id)
        text = msg.get("text") or ""
        combined_lines.append(f"## msg {msg_id}  ({'+'.join(record['kinds'])})\n{text}\n")
    if combined_lines:
        (group_dir / "_combined.md").write_text("\n".join(combined_lines), encoding="utf-8")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Telegram Web 频道消息与媒体采集（按 sender-group 输出）")
    p.add_argument("--target", default=DEFAULT_TARGET, help="目标频道 hash，例如 #-1001395144198")
    p.add_argument("--out-base", default=str(DEFAULT_OUT_BASE), help="输出根目录，默认 download/output")
    p.add_argument("--user-data-dir", default=str(DEFAULT_USER_DATA_DIR), help="持久化 profile 目录")
    p.add_argument("--browser-channel", default="", help="浏览器 channel，留空用 Chromium，可填 msedge/chrome")
    p.add_argument("--max-groups", type=int, default=None, help="最多处理的 sender-group 数量")
    p.add_argument("--scroll-max", type=int, default=200, help="最多向上滚动加载次数")
    p.add_argument("--headless", action="store_true", help="无头运行")
    p.add_argument("--skip-existing", action="store_true", default=True, help="组目录已有同 msg_id 媒体则跳过下载")
    p.add_argument("--no-skip-existing", action="store_false", dest="skip_existing", help="强制重新下载")
    p.add_argument("--dry-run", action="store_true", help="仅识别和记录，不下载")
    p.add_argument("--only-kinds", default="", help="逗号分隔，只处理指定类型，例 file,video,album,webpage")
    p.add_argument("--skip-kinds", default="", help="逗号分隔，跳过指定类型")
    p.add_argument("--start-msg-id", type=int, default=None, help="只处理 msg_id <= 该值的消息（即从历史更早的消息开始；不传则处理可见到的全部）")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
