"""
Telegram Web 频道消息与媒体采集（Playwright）

目标：把目标频道里每条消息的全量文本、堆叠的图片/文档/视频、以及评论区里的全部内容
（含评论内的图片/视频/文档）都按"sender-group → 单条消息"两层目录采集下来，
让人和 AI agent 都能直接 ls + read 拿到完整内容。

运行步骤：
  1. pip install -r requirements.txt && playwright install chromium
  2. 用持久化 profile 启动浏览器登录 Telegram Web。
     默认 profile：D:\\UserData\\.playwright_user_data--claude
     该 profile 若已被另一个 Chromium / msedge 占用（playwright MCP 等），运行前需先杀掉。
  3. 运行：python telegram_web_download_pua1.py [--target #-xxx] [--max-groups N]

输出目录布局：
  output/<channel_slug>/
    group_<yyyymmdd>_<sgseq>__<first_msg_id>/
      README.md                  整个 sender-group 的可读摘要：每条消息的文本 + 媒体引用
      msg_<msg_id>__<text_pre>/
        text.md                  消息文本（含链接 / hashtag）
        video.mp4                单视频
        video_thumb.png          单视频封面
        file_<原名>              文档
        photo.png                单图
        album_<sub_id>_video.mp4 相册视频
        album_<sub_id>_photo.png 相册图
        webpage_preview.png      网页预览截图
        _raw.html                （可选）原始消息 outerHTML，默认不写
        comments/
          README.md              评论区可读视图
          cmt_<id>__<text_pre>/
            text.md
            <同上的媒体文件>
    manifest.jsonl               逐条消息（含评论）的处理记录
    _channel_summary.txt         频道基本信息
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from playwright.async_api import (
    Download,
    Locator,
    Page,
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)

SCRIPT_DIR = Path(__file__).resolve().parent

# -------- 默认参数 --------
DEFAULT_USER_DATA_DIR = Path(r"D:\UserData\.playwright_user_data--claude")
DEFAULT_OUT_BASE = SCRIPT_DIR / "output"
DEFAULT_TARGET = "#-1001395144198"

# -------- Telegram Web Selectors --------
MESSAGE_LIST_ROOT = "#MiddleColumn .Transition_slide-active div.Transition.MessageList:has(div.messages-container)"
MESSAGE_LIST = f"{MESSAGE_LIST_ROOT} div.messages-container"
DATE_GROUP = ".message-date-group"
MSG_ITEM = "div.message-list-item[data-message-id]"

FILE_BLOCK = ".File.interactive"
MEDIA_INNER = ".media-inner.interactive"
ALBUM_ITEM = "[id^='album-media-message-']"
TEXT_CONTENT = ".text-content"
WEBPAGE = ".WebPage"
COMMENT_BUTTON = ".CommentButton"

CONTEXT_MENU_ROOT = ".Menu.MessageContextMenu"
CONTEXT_MENU_ITEM = f"{CONTEXT_MENU_ROOT} .MenuItem"


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def safe_filename(s: str, max_len: int = 80) -> str:
    s = (s or "").strip()
    for c in r'\/:*?"<>|':
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
    low = raw.lower()
    if low == "today":
        return datetime.now().strftime("%Y%m%d")
    if low == "yesterday":
        from datetime import timedelta
        return (datetime.now() - timedelta(days=1)).strftime("%Y%m%d")
    # weekday name (Monday..Sunday) → 最近一次该 weekday 的日期（不超过 today）
    wk = {"monday":0,"tuesday":1,"wednesday":2,"thursday":3,"friday":4,"saturday":5,"sunday":6}
    if low in wk:
        from datetime import timedelta
        today = datetime.now()
        delta = (today.weekday() - wk[low]) % 7
        if delta == 0:
            delta = 7  # 上周同一天而不是今天
        return (today - timedelta(days=delta)).strftime("%Y%m%d")
    return safe_filename(raw, 20)


def build_iso_datetime(yyyymmdd: str, time_text: str) -> str:
    """把 '20260219' + '00:02' / 'edited 13:03' 组合成 ISO 字符串。"""
    if not yyyymmdd or len(yyyymmdd) != 8 or not yyyymmdd.isdigit():
        return ""
    t = (time_text or "").strip()
    # strip 'edited ' prefix
    if t.lower().startswith("edited"):
        t = t.split(None, 1)[-1] if " " in t else ""
    # match HH:MM
    m = re.search(r"(\d{1,2}):(\d{2})", t)
    if not m:
        return f"{yyyymmdd[:4]}-{yyyymmdd[4:6]}-{yyyymmdd[6:]}"
    hh, mm = m.group(1).zfill(2), m.group(2)
    return f"{yyyymmdd[:4]}-{yyyymmdd[4:6]}-{yyyymmdd[6:]}T{hh}:{mm}"


def slugify(s: str, max_len: int = 60) -> str:
    s = (s or "channel").strip()
    s = re.sub(r"[\\/:*?\"<>|\n\r\t]+", "_", s)
    s = re.sub(r"\s+", "_", s)
    return s[:max_len] or "channel"


# ---------------- DOM 抽取 ----------------

EXTRACT_MESSAGES_JS = r"""
() => {
  // 工具：提取一个消息节点的所有信息
  const extractMsg = (m) => {
    const id = m.getAttribute('data-message-id');
    const cls = m.className || '';
    const contentClass = m.querySelector('.message-content')?.className || '';
    // ---- 文本：克隆 .text-content，去掉 MessageMeta + WebPage 块，保留文本/链接/hashtag ----
    let text = '';
    const tc = m.querySelector('.text-content');
    if (tc) {
      const clone = tc.cloneNode(true);
      clone.querySelectorAll('.MessageMeta, .WebPage').forEach(n => n.remove());
      // 把 <br> 转成 \n 以保留换行
      clone.querySelectorAll('br').forEach(br => br.replaceWith('\n'));
      text = (clone.innerText || clone.textContent || '').trim();
    }
    // ---- WebPage 预览 ----
    let webPage = null;
    const wp = m.querySelector('.WebPage');
    if (wp) {
      webPage = {
        siteName: wp.querySelector('.site-name')?.innerText || '',
        siteTitle: wp.querySelector('.site-title')?.innerText || '',
        siteDescription: wp.querySelector('.site-description')?.innerText || '',
        hasImage: !!wp.querySelector('img.full-media, img.thumbnail, canvas.thumbnail'),
      };
    }
    // ---- 文件 ----
    let fileName = '';
    let fileSize = '';
    const ft = m.querySelector('.File .file-title');
    if (ft) fileName = (ft.getAttribute('title') || ft.innerText || '').trim();
    const fs = m.querySelector('.File .file-subtitle');
    if (fs) fileSize = (fs.innerText || '').trim();
    // ---- 相册 ----
    const albumItems = [];
    m.querySelectorAll("[id^='album-media-message-']").forEach(it => {
      const subId = (it.getAttribute('id') || '').replace('album-media-message-','');
      const hasVideo = !!it.querySelector('video, .media-inner.interactive');
      const hasImg = !!it.querySelector('img.full-media, img.thumbnail, canvas.thumbnail');
      albumItems.push({ subId, hasVideo, hasImg });
    });
    // ---- 链接、hashtag、mention ----
    const links = [];
    const hashtags = [];
    const mentions = [];
    if (tc) tc.querySelectorAll('a').forEach(a => {
      const h = a.getAttribute('href') || '';
      const t = (a.innerText || '').trim();
      const type = a.getAttribute('data-entity-type') || '';
      if (type === 'MessageEntityHashtag') {
        hashtags.push(t);
      } else if (type === 'MessageEntityMention' || type === 'MessageEntityMentionName') {
        mentions.push(t);
      } else if (h && h.startsWith('http')) {
        links.push({ href: h, text: t || h });
      }
    });
    // ---- 时间、views、shares、edited、pinned、reactions、reply、forwarded ----
    const messageTime = m.querySelector('.message-time')?.innerText?.trim() || '';
    const viewsTitle = m.querySelector('.message-views')?.getAttribute('title') || '';
    let views = null, shares = null;
    if (viewsTitle) {
      const vm = viewsTitle.match(/Views:\s*([\d,]+)/i); if (vm) views = parseInt(vm[1].replace(/,/g,''), 10);
      const sm = viewsTitle.match(/Shares:\s*([\d,]+)/i); if (sm) shares = parseInt(sm[1].replace(/,/g,''), 10);
    }
    const isEdited = cls.includes('was-edited') || messageTime.toLowerCase().startsWith('edited');
    const isPinned = m.getAttribute('data-is-pinned') === 'true' || cls.includes('is-pinned');
    // reply (.EmbeddedMessage 或 .embedded-reply)
    let reply = null;
    const rep = m.querySelector('.EmbeddedMessage, .embedded-reply');
    if (rep) {
      reply = {
        sender: rep.querySelector('.message-title, .embedded-sender, .sender-title')?.innerText?.trim() || '',
        text: rep.querySelector('.message-text, .embedded-text-wrapper, .embedded-message-text')?.innerText?.trim() || rep.innerText?.trim() || '',
      };
    }
    // forwarded (.Message.forwarded 或 .message-title.interactive forward header)
    let forwardedFrom = null;
    if (cls.includes('forwarded') || cls.includes('is-forwarded')) {
      const fwd = m.querySelector('.message-title, .Forwarded, .forward-title');
      if (fwd) forwardedFrom = (fwd.innerText || '').trim().replace(/\s+/g, ' ');
    } else {
      // 有些 forwarded 消息没有 class，看是否有 "Forwarded from" header
      const ft2 = m.querySelector('.message-title.interactive');
      if (ft2 && ft2.innerText && ft2.innerText.includes('Forwarded')) {
        forwardedFrom = ft2.innerText.trim();
      }
    }
    // reactions
    const reactions = [];
    m.querySelectorAll('.Reactions .ReactionButton, .Reactions [class*="Reaction"]').forEach(rb => {
      const emoji = rb.querySelector('.emoji, img')?.getAttribute('alt') || rb.querySelector('.ReactionStaticEmoji')?.innerText || '';
      const count = rb.querySelector('.counter, .count')?.innerText?.trim() || '';
      if (emoji || count) reactions.push({ emoji, count });
    });
    // comments
    let commentCount = 0;
    const cb = m.querySelector('.CommentButton');
    if (cb) {
      const labelEl = cb.querySelector('.label');
      if (labelEl) {
        const mm = (labelEl.innerText || '').match(/\d+/);
        if (mm) commentCount = parseInt(mm[0], 10);
      }
      if (!commentCount) commentCount = -1;
    }
    return {
      id,
      cls,
      contentClass,
      isFirstInGroup: cls.includes('first-in-group'),
      isLastInGroup: cls.includes('last-in-group'),
      isActionMessage: cls.includes('ActionMessage'),
      hasFile: !!m.querySelector('.File.interactive'),
      fileName,
      fileSize,
      hasMediaInner: !!m.querySelector('.media-inner.interactive'),
      hasAlbum: albumItems.length > 0,
      albumItems,
      hasWebPage: !!m.querySelector('.WebPage'),
      webPage,
      hasComments: commentCount !== 0,
      commentCount,
      text,
      links,
      hashtags,
      mentions,
      messageTime,   // "00:02" 或 "edited 13:03"
      views,         // int
      shares,        // int
      isEdited,
      isPinned,
      reply,
      forwardedFrom,
      reactions,
    };
  };

  const out = { containerFound: false, firstVisibleId: null, lastVisibleId: null, dateGroups: [] };
  const dateGroups = document.querySelectorAll('#MiddleColumn .Transition_slide-active .message-date-group');
  if (!dateGroups.length) return out;
  out.containerFound = true;
  for (const dg of dateGroups) {
    const dateText = dg.querySelector('.sticky-date span')?.innerText || '';
    const msgs = dg.querySelectorAll('div.message-list-item[data-message-id]');
    const items = [];
    msgs.forEach(m => items.push(extractMsg(m)));
    if (items.length) out.dateGroups.push({ dateText, items });
  }
  const all = document.querySelectorAll('#MiddleColumn .Transition_slide-active .message-list-item[data-message-id]');
  if (all.length) {
    out.firstVisibleId = all[0].getAttribute('data-message-id');
    out.lastVisibleId = all[all.length-1].getAttribute('data-message-id');
  }
  return out;
}
"""


# ---------------- 工具：右键下载 ----------------


async def _close_any_open_menu(page: Page) -> None:
    try:
        if await page.locator(CONTEXT_MENU_ROOT).count() > 0:
            await page.keyboard.press("Escape")
            await page.wait_for_timeout(150)
    except Exception:
        pass


async def right_click_download(
    page: Page,
    locator: Locator,
    timeout_ms: int = 90000,
    max_attempts: int = 2,
) -> Optional[Download]:
    """右键 → 等菜单 → 点 Download → 抓 download 事件。"""
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
        await page.wait_for_timeout(400)
        try:
            await locator.evaluate(
                """el => {
                    const r = el.getBoundingClientRect();
                    const x = r.left + r.width/2, y = r.top + r.height/2;
                    const opts = { bubbles: true, cancelable: true, view: window,
                                   button: 2, buttons: 2, clientX: x, clientY: y };
                    el.dispatchEvent(new MouseEvent('mousedown', opts));
                    el.dispatchEvent(new MouseEvent('mouseup', opts));
                    el.dispatchEvent(new MouseEvent('contextmenu', opts));
                }"""
            )
        except Exception:
            try:
                await locator.click(button="right", force=True, timeout=5000)
            except Exception as e:
                log(f"  右键失败 (attempt {attempt+1}): {e}")
                continue
        menu_item_any = page.locator(CONTEXT_MENU_ITEM).first
        try:
            await menu_item_any.wait_for(state="visible", timeout=4000)
        except PlaywrightTimeoutError:
            log(f"  右键菜单未出现 (attempt {attempt+1})")
            continue
        download_item = page.locator(f"{CONTEXT_MENU_ITEM}:has-text('Download')").first
        try:
            if not await download_item.is_visible():
                await _close_any_open_menu(page)
                log("  菜单中无 Download 项")
                return None
        except Exception:
            await _close_any_open_menu(page)
            return None
        try:
            async with page.expect_download(timeout=timeout_ms) as di:
                await download_item.click()
            return await di.value
        except Exception as e:
            log(f"  下载等待失败 (attempt {attempt+1}): {e}")
            await _close_any_open_menu(page)
            if "crashed" in str(e).lower():
                raise
            continue
    return None


def _find_ffmpeg_bin() -> str:
    import shutil as _sh
    for c in (r"C:\ProgramData\chocolatey\bin\ffmpeg.exe", "ffmpeg"):
        p = _sh.which(c) if not Path(c).is_absolute() else (c if Path(c).exists() else None)
        if p:
            return p
    return "ffmpeg"


def video_to_mp3(video_path: Path, mp3_path: Optional[Path] = None, mono: bool = True) -> Optional[Path]:
    """用 ffmpeg + libmp3lame 把视频抽成 mp3。失败返回 None。"""
    if not video_path.exists() or video_path.stat().st_size < 1024:
        return None
    if mp3_path is None:
        mp3_path = video_path.with_name("转写_audio.mp3") if video_path.name == "video.mp4" else video_path.with_suffix(".mp3")
    if mp3_path.exists() and mp3_path.stat().st_size > 2000:
        return mp3_path  # 已存在且非 0 字节/header-only
    import subprocess
    cmd = [
        _find_ffmpeg_bin(), "-y", "-i", str(video_path),
        "-vn",
        "-c:a", "libmp3lame",
        "-ac", "1" if mono else "2",
        "-b:a", "64k",
        "-ar", "22050",
        "-f", "mp3",
        str(mp3_path),
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=1800)
        if r.returncode == 0 and mp3_path.exists() and mp3_path.stat().st_size > 0:
            return mp3_path
        log(f"  ⚠ ffmpeg returncode={r.returncode}: {r.stderr.decode('utf-8', 'ignore')[-200:]}")
    except FileNotFoundError:
        log("  ⚠ ffmpeg 未安装，跳过 mp3 抽取")
    except subprocess.TimeoutExpired:
        log("  ⚠ ffmpeg 超时（10 分钟）")
    except Exception as e:
        log(f"  ⚠ ffmpeg 异常: {e}")
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


# ---------------- 真实图片下载（blob fetch / canvas toDataURL） ----------------


async def save_blob_image(page: Page, img_locator: Locator, dest_stem: Path) -> Optional[Path]:
    """
    从 img 元素的 blob: src 拉到原始字节并写到 dest_stem.<ext>。
    返回保存路径。失败返回 None。
    """
    try:
        if await img_locator.count() == 0:
            return None
        await img_locator.scroll_into_view_if_needed(timeout=3000)
    except Exception:
        pass
    try:
        # 等到 img.complete 且有自然宽高
        await img_locator.evaluate(
            """async (el) => {
                if (el.complete && el.naturalWidth) return;
                await new Promise(r => {
                    if (el.complete && el.naturalWidth) return r();
                    el.addEventListener('load', r, { once: true });
                    el.addEventListener('error', r, { once: true });
                    setTimeout(r, 3000);
                });
            }"""
        )
        data = await img_locator.evaluate(
            """async (el) => {
                const src = el.getAttribute('src') || el.src;
                if (!src) return { err: 'no-src' };
                try {
                    const r = await fetch(src);
                    const blob = await r.blob();
                    const dataUrl = await new Promise((resolve, reject) => {
                        const fr = new FileReader();
                        fr.onload = () => resolve(fr.result);
                        fr.onerror = () => reject(fr.error);
                        fr.readAsDataURL(blob);
                    });
                    return { dataUrl, mime: blob.type || r.headers.get('content-type') || '', size: blob.size,
                             nw: el.naturalWidth, nh: el.naturalHeight };
                } catch (e) {
                    return { err: String(e) };
                }
            }"""
        )
    except Exception as e:
        log(f"  blob fetch 失败: {e}")
        return None
    if not data or data.get("err"):
        return None
    data_url = data.get("dataUrl") or ""
    if not data_url.startswith("data:") or "," not in data_url:
        return None
    import base64
    mime = (data.get("mime") or "image/jpeg").lower()
    ext = ".jpg"
    if "png" in mime:
        ext = ".png"
    elif "webp" in mime:
        ext = ".webp"
    elif "gif" in mime:
        ext = ".gif"
    payload = base64.b64decode(data_url.split(",", 1)[1])
    dest = dest_stem.with_suffix(ext)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(payload)
    return dest


async def save_canvas_image(canvas_locator: Locator, dest: Path) -> Optional[Path]:
    """canvas.toDataURL → PNG bytes。低分辨率但是 Telegram 真正渲染的预览。"""
    try:
        if await canvas_locator.count() == 0:
            return None
        await canvas_locator.scroll_into_view_if_needed(timeout=3000)
    except Exception:
        pass
    try:
        b64 = await canvas_locator.evaluate(
            """el => {
                try { return el.toDataURL('image/png').split(',', 1)[1] || ''; }
                catch (e) { return ''; }
            }"""
        )
    except Exception as e:
        log(f"  canvas toDataURL 失败: {e}")
        return None
    if not b64:
        return None
    import base64
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(base64.b64decode(b64))
    return dest


async def save_image_for_element(page: Page, container: Locator, dest_stem: Path) -> Optional[Path]:
    """
    给一个 container（消息或相册子项），优先 img.full-media (blob 原图) → img.thumbnail → canvas.thumbnail。
    """
    # 优先 full-media（高清原图，clicked-to-load 之后会有）
    for sel in ("img.full-media", "img.thumbnail"):
        try:
            cnt = await container.locator(sel).count()
        except Exception:
            continue
        if cnt > 0:
            img = container.locator(sel).first
            src = ""
            try:
                src = await img.get_attribute("src") or ""
            except Exception:
                pass
            if src.startswith("blob:") or src.startswith("data:") or src.startswith("http"):
                p = await save_blob_image(page, img, dest_stem)
                if p:
                    return p
    # 兜底：canvas
    try:
        if await container.locator("canvas.thumbnail").count() > 0:
            return await save_canvas_image(container.locator("canvas.thumbnail").first, dest_stem.with_suffix(".png"))
    except Exception:
        pass
    return None


# ---------------- 单条消息处理 ----------------


async def process_message(
    page: Page,
    msg_data: dict,
    msg_locator: Locator,
    msg_dir: Path,
    *,
    skip_existing: bool,
    only_kinds: Optional[set[str]],
    skip_kinds: Optional[set[str]],
    keep_raw_html: bool,
) -> dict:
    """处理单条消息，全部产物写入 msg_dir。返回 manifest 记录字典。"""
    msg_id = msg_data["id"]
    text = msg_data.get("text", "") or ""
    links = msg_data.get("links") or []
    hashtags = msg_data.get("hashtags") or []
    mentions = msg_data.get("mentions") or []
    record: dict[str, Any] = {
        "msg_id": msg_id,
        "kinds": [],
        "saved": [],
        "errors": [],
        "text_len": len(text),
    }

    if msg_data.get("isActionMessage"):
        record["kinds"].append("action")
        return record

    msg_dir.mkdir(parents=True, exist_ok=True)

    # 检测是否已经下载过媒体（除 text.md / meta.json / _raw.html / comments/ 之外有任何文件）
    SKIP_NAMES = {"text.md", "meta.json", "_raw.html"}
    has_media_files = False
    for p in msg_dir.iterdir():
        if p.is_file() and p.name not in SKIP_NAMES:
            has_media_files = True
            break

    # 写 meta.json（含完整元数据）+ text.md（人/AI 可读）
    meta = {
        "msg_id": msg_id,
        "date": msg_data.get("_date_text", ""),
        "yyyymmdd": msg_data.get("_yyyymmdd", ""),
        "message_time": msg_data.get("messageTime") or "",
        "iso_datetime": msg_data.get("_iso_datetime", ""),
        "views": msg_data.get("views"),
        "shares": msg_data.get("shares"),
        "is_edited": bool(msg_data.get("isEdited")),
        "is_pinned": bool(msg_data.get("isPinned")),
        "is_first_in_group": bool(msg_data.get("isFirstInGroup")),
        "is_last_in_group": bool(msg_data.get("isLastInGroup")),
        "reply": msg_data.get("reply"),
        "forwarded_from": msg_data.get("forwardedFrom"),
        "reactions": msg_data.get("reactions") or [],
        "hashtags": hashtags,
        "mentions": mentions,
        "links": links,
        "file_name": msg_data.get("fileName") or "",
        "file_size": msg_data.get("fileSize") or "",
        "web_page": msg_data.get("webPage"),
        "comment_count": msg_data.get("commentCount") or 0,
        "text": text,
    }
    (msg_dir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    md_lines = [f"# Message {msg_id}", ""]
    head_bits = []
    if meta["iso_datetime"]:
        head_bits.append(f"`{meta['iso_datetime']}`")
    elif meta["message_time"]:
        head_bits.append(f"`{meta['date']} {meta['message_time']}`")
    if meta["views"] is not None:
        head_bits.append(f"views: {meta['views']:,}")
    if meta["shares"] is not None:
        head_bits.append(f"shares: {meta['shares']:,}")
    if meta["is_edited"]:
        head_bits.append("edited")
    if meta["is_pinned"]:
        head_bits.append("pinned")
    if meta["forwarded_from"]:
        head_bits.append(f"forwarded from: {meta['forwarded_from']}")
    if head_bits:
        md_lines.append(" · ".join(head_bits))
        md_lines.append("")
    if meta["reply"]:
        r = meta["reply"]
        md_lines.append(f"> ↪ reply to {r.get('sender') or ''}: {r.get('text') or ''}")
        md_lines.append("")
    if text:
        md_lines.append(text)
    else:
        md_lines.append("_(no text)_")
    if hashtags:
        md_lines.append("")
        md_lines.append("Tags: " + " ".join(hashtags))
    if links:
        md_lines.append("\n## Links\n")
        for ln in links:
            href = ln.get("href") or ""
            t = ln.get("text") or href
            md_lines.append(f"- [{t}]({href})")
    if meta["web_page"]:
        wp = meta["web_page"]
        md_lines.append("\n## Webpage Preview\n")
        if wp.get("siteName"): md_lines.append(f"- site: {wp['siteName']}")
        if wp.get("siteTitle"): md_lines.append(f"- title: {wp['siteTitle']}")
        if wp.get("siteDescription"): md_lines.append(f"- desc: {wp['siteDescription']}")
    if meta["reactions"]:
        md_lines.append("\n## Reactions\n")
        for rx in meta["reactions"]:
            md_lines.append(f"- {rx.get('emoji', '')} {rx.get('count', '')}")
    (msg_dir / "text.md").write_text("\n".join(md_lines) + "\n", encoding="utf-8")

    # 可选 raw.html
    if keep_raw_html and not (msg_dir / "_raw.html").exists():
        try:
            html = await msg_locator.evaluate("el => el.outerHTML")
            (msg_dir / "_raw.html").write_text(html or "", encoding="utf-8")
        except Exception as e:
            record["errors"].append(f"raw.html: {e}")

    if skip_existing and has_media_files:
        record["skipped_media"] = True
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
                dest = msg_dir / safe_filename(f"file_{stem}{ext}", 150)
                log(f"  → file: {original}")
                try:
                    dl = await right_click_download(page, file_block, timeout_ms=120000)
                except Exception as e:
                    record["errors"].append(f"file-crash: {e}")
                    return record
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
                    log(f"  → album video sub={sub_id}")
                    try:
                        dl = await right_click_download(page, item_loc, timeout_ms=1800000, max_attempts=2)
                    except Exception as e:
                        record["errors"].append(f"album-video-{sub_id}-crash: {e}")
                        if "crashed" in str(e).lower():
                            return record
                    else:
                        if dl:
                            suggested = dl.suggested_filename or ""
                            sfx = Path(suggested).suffix or ".mp4"
                            dest = msg_dir / safe_filename(f"album_{sub_id}_video{sfx}")
                            saved = await save_download(dl, dest)
                            if saved:
                                record["saved"].append(saved.name)
                                # mp3 抽取交给独立 batch 进程
                        else:
                            record["errors"].append(f"album-video-{sub_id}-no-download")
                # 真实图（原图 blob）/ canvas 兜底
                try:
                    p = await save_image_for_element(page, item_loc, msg_dir / safe_filename(f"album_{sub_id}_photo"))
                    if p:
                        record["saved"].append(p.name)
                except Exception as e:
                    record["errors"].append(f"album-photo-{sub_id}: {e}")

    # 3) 单视频
    if msg_data.get("hasMediaInner") and not msg_data.get("hasAlbum") and not msg_data.get("hasWebPage"):
        record["kinds"].append("video")
        if kind_enabled("video"):
            media = msg_locator.locator(MEDIA_INNER).first
            if await media.count() > 0:
                log(f"  → video msg={msg_id}（长视频可能需 30 分钟）")
                try:
                    dl = await right_click_download(page, media, timeout_ms=1800000, max_attempts=2)
                except Exception as e:
                    record["errors"].append(f"video-crash: {e}")
                    if "crashed" in str(e).lower():
                        return record
                else:
                    if dl:
                        suggested = dl.suggested_filename or ""
                        sfx = Path(suggested).suffix or ".mp4"
                        dest = msg_dir / safe_filename(f"video{sfx}")
                        saved = await save_download(dl, dest)
                        if saved:
                            record["saved"].append(saved.name)
                            # mp3 抽取交给独立 batch 进程，不阻塞下载主流程
                        else:
                            record["errors"].append("video-save-failed")
                    else:
                        record["errors"].append("video-no-download")
                # 封面：原图 blob → canvas
                try:
                    p = await save_image_for_element(page, media, msg_dir / "video_thumb")
                    if p:
                        record["saved"].append(p.name)
                except Exception as e:
                    record["errors"].append(f"video-thumb: {e}")

    # 4) 网页预览：保存预览原图
    if msg_data.get("hasWebPage"):
        record["kinds"].append("webpage")
        if kind_enabled("webpage"):
            try:
                preview = msg_locator.locator(f"{WEBPAGE} .media-inner").first
                if await preview.count() > 0:
                    p = await save_image_for_element(page, preview, msg_dir / "webpage_preview")
                    if p:
                        record["saved"].append(p.name)
            except Exception as e:
                record["errors"].append(f"webpage: {e}")

    # 5) 单图 / 单贴纸（hasMediaInner 但没有 album、没有 webpage、没有 hasFile，且消息内容是 photo）
    if (msg_data.get("contentClass") or "").find("photo") >= 0 and not msg_data.get("hasAlbum"):
        record["kinds"].append("photo")
        if kind_enabled("photo"):
            try:
                p = await save_image_for_element(page, msg_locator, msg_dir / "photo")
                if p:
                    record["saved"].append(p.name)
            except Exception as e:
                record["errors"].append(f"photo: {e}")

    if not record["kinds"]:
        record["kinds"].append("text-only")

    return record


# ---------------- 评论处理 ----------------


COMMENT_EXTRACT_JS = r"""
() => {
  const containers = document.querySelectorAll('div.Transition.MessageList:has(div.messages-container)');
  if (containers.length === 0) return { items: [], threadTopId: null, dateGroups: [] };
  const activeSlide = containers[containers.length - 1];
  let threadTopId = null;
  const tt = activeSlide.querySelector('.is-thread-top');
  if (tt) threadTopId = tt.getAttribute('data-message-id');
  const items = [];
  // 用 date-group + message 双层遍历以拿到日期
  const dgs = activeSlide.querySelectorAll('.message-date-group');
  dgs.forEach(dg => {
    const dateText = dg.querySelector('.sticky-date span')?.innerText || '';
    dg.querySelectorAll('div.message-list-item[data-message-id]').forEach(m => {
      const id = m.getAttribute('data-message-id');
      if (id === threadTopId) return;
      const cls = m.className || '';
      let text = '';
      const tc = m.querySelector('.text-content');
      if (tc) {
        const clone = tc.cloneNode(true);
        clone.querySelectorAll('.MessageMeta, .WebPage').forEach(n => n.remove());
        clone.querySelectorAll('br').forEach(br => br.replaceWith('\n'));
        text = (clone.innerText || '').trim();
      }
      let sender = '';
      const s = m.querySelector('.message-title .interactive, .sender-title, .message-title-name, .message-title');
      if (s) sender = (s.innerText || '').trim().split('\n')[0];
      let fileName = '';
      const ft = m.querySelector('.File .file-title');
      if (ft) fileName = (ft.getAttribute('title') || ft.innerText || '').trim();
      const albumItems = [];
      m.querySelectorAll("[id^='album-media-message-']").forEach(it => {
        const subId = (it.getAttribute('id') || '').replace('album-media-message-','');
        const hasVideo = !!it.querySelector('video, .media-inner.interactive');
        const hasImg = !!it.querySelector('img.full-media, img.thumbnail, canvas.thumbnail');
        albumItems.push({ subId, hasVideo, hasImg });
      });
      const messageTime = m.querySelector('.message-time')?.innerText?.trim() || '';
      const viewsTitle = m.querySelector('.message-views')?.getAttribute('title') || '';
      let views = null, shares = null;
      if (viewsTitle) {
        const vm = viewsTitle.match(/Views:\s*([\d,]+)/i); if (vm) views = parseInt(vm[1].replace(/,/g,''), 10);
        const sm = viewsTitle.match(/Shares:\s*([\d,]+)/i); if (sm) shares = parseInt(sm[1].replace(/,/g,''), 10);
      }
      const isEdited = cls.includes('was-edited') || messageTime.toLowerCase().startsWith('edited');
      const links = [];
      const hashtags = [];
      const mentions = [];
      if (tc) tc.querySelectorAll('a').forEach(a => {
        const h = a.getAttribute('href') || '';
        const t = (a.innerText || '').trim();
        const type = a.getAttribute('data-entity-type') || '';
        if (type === 'MessageEntityHashtag') hashtags.push(t);
        else if (type === 'MessageEntityMention' || type === 'MessageEntityMentionName') mentions.push(t);
        else if (h && h.startsWith('http')) links.push({ href: h, text: t || h });
      });
      items.push({
        id,
        cls,
        sender,
        text,
        dateText,
        messageTime,
        views,
        shares,
        isEdited,
        links,
        hashtags,
        mentions,
        hasFile: !!m.querySelector('.File.interactive'),
        fileName,
        hasMediaInner: !!m.querySelector('.media-inner.interactive'),
        hasAlbum: albumItems.length > 0,
        albumItems,
        hasWebPage: !!m.querySelector('.WebPage'),
      });
    });
  });
  return { items, threadTopId };
}
"""


async def process_comments(
    page: Page,
    msg_locator: Locator,
    parent_msg_id: str,
    comments_dir: Path,
    *,
    skip_existing: bool,
    only_kinds: Optional[set[str]],
    skip_kinds: Optional[set[str]],
    keep_raw_html: bool,
) -> list[dict]:
    """点开评论 → 抓取 → 退出。返回 list[record]。"""
    cb = msg_locator.locator(COMMENT_BUTTON).first
    if await cb.count() == 0:
        return []
    log(f"  → comments for msg={parent_msg_id}")
    try:
        await cb.scroll_into_view_if_needed()
        await cb.click(timeout=5000)
        await page.wait_for_timeout(2500)
    except Exception as e:
        log(f"    评论按钮点击失败: {e}")
        return []
    # 等讨论区加载（出现 is-thread-top）
    try:
        await page.locator("#MiddleColumn .Transition_slide-active .is-thread-top").first.wait_for(state="attached", timeout=8000)
    except PlaywrightTimeoutError:
        log("    讨论区未加载")
        await page.keyboard.press("Escape")
        await page.wait_for_timeout(500)
        return []
    records: list[dict] = []
    # 向上滚动评论区加载更多
    seen_ids: set[str] = set()
    for round_idx in range(20):
        data = await page.evaluate(COMMENT_EXTRACT_JS)
        items = data.get("items") or []
        new_count = 0
        for ci in items:
            cid = ci["id"]
            if cid in seen_ids:
                continue
            seen_ids.add(cid)
            new_count += 1
            cpre = text_preview(ci.get("text") or "", 24, default_empty="无文字")
            cdir = comments_dir / safe_filename(f"cmt_{cid}__{cpre}")
            cdir.mkdir(parents=True, exist_ok=True)
            # 转成与 message 类似的结构，再调用 process_message 单条处理逻辑
            c_date = ci.get("dateText") or ""
            c_yyyymmdd = date_to_yyyymmdd(c_date)
            c_iso = build_iso_datetime(c_yyyymmdd, ci.get("messageTime") or "")
            faux = {
                "id": cid,
                "text": ci.get("text") or "",
                "links": ci.get("links") or [],
                "hashtags": ci.get("hashtags") or [],
                "mentions": ci.get("mentions") or [],
                "hasFile": ci.get("hasFile"),
                "fileName": ci.get("fileName"),
                "fileSize": ci.get("fileSize"),
                "hasMediaInner": ci.get("hasMediaInner"),
                "hasAlbum": ci.get("hasAlbum"),
                "albumItems": ci.get("albumItems") or [],
                "hasWebPage": ci.get("hasWebPage"),
                "webPage": ci.get("webPage"),
                "isActionMessage": False,
                "isFirstInGroup": False,
                "isLastInGroup": False,
                "messageTime": ci.get("messageTime") or "",
                "views": ci.get("views"),
                "shares": ci.get("shares"),
                "isEdited": bool(ci.get("isEdited")),
                "isPinned": False,
                "reply": None,
                "forwardedFrom": None,
                "reactions": [],
                "commentCount": 0,
                "_date_text": c_date,
                "_yyyymmdd": c_yyyymmdd,
                "_iso_datetime": c_iso,
            }
            # 在讨论区里 message-list-item 用 #message-<id> 定位
            cmt_loc = page.locator(f"#MiddleColumn .Transition_slide-active #message-{cid}").first
            if await cmt_loc.count() == 0:
                continue
            try:
                rec = await process_message(
                    page, faux, cmt_loc, cdir,
                    skip_existing=skip_existing,
                    only_kinds=only_kinds,
                    skip_kinds=skip_kinds,
                    keep_raw_html=keep_raw_html,
                )
            except Exception as e:
                rec = {"msg_id": cid, "kinds": ["error"], "saved": [], "errors": [f"crash: {e}"]}
            rec["sender"] = ci.get("sender") or ""
            rec["is_comment"] = True
            rec["parent_msg_id"] = parent_msg_id
            records.append(rec)
        # 评论区向上滚加载更多
        first_cmt = await page.evaluate(
            "() => document.querySelector('#MiddleColumn .Transition_slide-active .message-list-item[data-message-id]')?.getAttribute('data-message-id') || null"
        )
        await page.evaluate(
            "() => { const ml = document.querySelector('#MiddleColumn .Transition_slide-active div.Transition.MessageList'); if (ml) ml.scrollTop = 0; }"
        )
        await page.wait_for_timeout(800)
        new_first = await page.evaluate(
            "() => document.querySelector('#MiddleColumn .Transition_slide-active .message-list-item[data-message-id]')?.getAttribute('data-message-id') || null"
        )
        if new_count == 0 and new_first == first_cmt:
            break
    # 写评论区 README
    if records:
        await write_comments_readme(comments_dir, records)
    # 退回主列表
    await _close_any_open_menu(page)
    try:
        back_btn = page.locator("#MiddleColumn .Transition_slide-active .Button.back-button, #MiddleColumn .Transition_slide-active button[aria-label='Back']").first
        if await back_btn.count() > 0:
            await back_btn.click(timeout=3000)
        else:
            await page.keyboard.press("Escape")
    except Exception:
        try:
            await page.keyboard.press("Escape")
        except Exception:
            pass
    await page.wait_for_timeout(800)
    return records


async def write_comments_readme(comments_dir: Path, records: list[dict]) -> None:
    lines = ["# Comments", ""]
    for r in records:
        cid = r["msg_id"]
        sender = r.get("sender") or ""
        cdirname = None
        for sub in comments_dir.iterdir():
            if sub.is_dir() and sub.name.startswith(f"cmt_{cid}__"):
                cdirname = sub.name
                break
        cdirname = cdirname or f"cmt_{cid}"
        lines.append(f"## {sender} (msg {cid})\n")
        text_file = comments_dir / cdirname / "text.md"
        if text_file.exists():
            txt = text_file.read_text(encoding="utf-8")
            body_lines = txt.splitlines()[2:]  # skip "# Message ..." header
            lines.extend(body_lines)
        # media file listing
        media_files = []
        cdir = comments_dir / cdirname
        if cdir.exists():
            for p in sorted(cdir.iterdir()):
                if p.is_file() and p.name not in ("text.md", "meta.json", "_raw.html"):
                    media_files.append(p.name)
        if media_files:
            lines.append("\n_Media:_\n")
            for mf in media_files:
                lines.append(f"- [{mf}]({cdirname}/{mf})")
        lines.append("")
    (comments_dir / "README.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------------- sender-group 处理 ----------------


def make_group_dir(out_dir: Path, sg_seq: int, yyyymmdd: str, first_msg_id: str) -> Path:
    return out_dir / safe_filename(f"group_{yyyymmdd}_{sg_seq:04d}__{first_msg_id}")


def make_msg_dir(group_dir: Path, msg_id: str, text_pre: str) -> Path:
    return group_dir / safe_filename(f"msg_{msg_id}__{text_pre}")


async def write_group_readme(group_dir: Path, records: list[dict]) -> None:
    """汇总组内每条消息的文本 + 媒体列表，写成可读 README.md。"""
    lines = [f"# Sender Group", ""]
    for r in records:
        mid = r["msg_id"]
        kinds = "+".join(r.get("kinds") or [])
        # find msg_dir
        mdir = None
        for sub in group_dir.iterdir():
            if sub.is_dir() and sub.name.startswith(f"msg_{mid}__"):
                mdir = sub
                break
        if not mdir:
            continue
        lines.append(f"## msg {mid}  ({kinds})\n")
        text_md = mdir / "text.md"
        if text_md.exists():
            body = text_md.read_text(encoding="utf-8").splitlines()[2:]  # skip header
            lines.extend(body)
        media = []
        for p in sorted(mdir.iterdir()):
            if p.is_file() and p.name not in ("text.md", "meta.json", "_raw.html"):
                media.append(p.name)
        if media:
            lines.append("\n_Media:_\n")
            for m in media:
                lines.append(f"- [{m}]({mdir.name}/{m})")
        cmts = mdir / "comments"
        if cmts.exists() and (cmts / "README.md").exists():
            lines.append(f"\n_Comments: [{mdir.name}/comments/README.md]({mdir.name}/comments/README.md)_\n")
        lines.append("")
    (group_dir / "README.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


async def process_sender_group(
    page: Page,
    sg_msgs: list[dict],
    out_dir: Path,
    manifest_fp,
    sg_seq: int,
    processed_ids: set[str],
    *,
    skip_existing: bool,
    only_kinds: Optional[set[str]],
    skip_kinds: Optional[set[str]],
    keep_raw_html: bool,
    capture_comments: bool,
    dry_run: bool,
) -> None:
    if not sg_msgs:
        return
    first = sg_msgs[0]
    first_msg_id = first["msg"]["id"]
    if first_msg_id in processed_ids:
        return
    yyyymmdd = first["yyyymmdd"]
    group_dir = make_group_dir(out_dir, sg_seq, yyyymmdd, first_msg_id)
    log(f"sender-group seq={sg_seq} date={first['date_text']} first_msg={first_msg_id} -> {group_dir.name}")
    if dry_run:
        for s in sg_msgs:
            processed_ids.add(s["msg"]["id"])
            manifest_fp.write(json.dumps({
                "ts": datetime.now().isoformat(),
                "sg_seq": sg_seq,
                "msg_id": s["msg"]["id"],
                "dry_run": True,
                "text_len": len(s["msg"].get("text") or ""),
                "hasFile": s["msg"].get("hasFile"),
                "hasVideo": s["msg"].get("hasMediaInner") and not s["msg"].get("hasAlbum"),
                "hasAlbum": s["msg"].get("hasAlbum"),
                "hasComments": s["msg"].get("hasComments"),
            }, ensure_ascii=False) + "\n")
        manifest_fp.flush()
        return
    group_dir.mkdir(parents=True, exist_ok=True)
    msg_records: list[dict] = []
    for s in sg_msgs:
        msg = s["msg"]
        msg_id = msg["id"]
        # 注入日期 / iso datetime 给 process_message
        msg["_date_text"] = s["date_text"]
        msg["_yyyymmdd"] = s["yyyymmdd"]
        msg["_iso_datetime"] = build_iso_datetime(s["yyyymmdd"], msg.get("messageTime") or "")
        if msg_id in processed_ids:
            continue
        loc = page.locator(f"{MESSAGE_LIST_ROOT} #message-{msg_id}").first
        if await loc.count() == 0:
            log(f"  消息 {msg_id} 不在 DOM，跳过")
            continue
        text_pre = text_preview(msg.get("text") or "", 30)
        msg_dir = make_msg_dir(group_dir, msg_id, text_pre)
        try:
            record = await process_message(
                page, msg, loc, msg_dir,
                skip_existing=skip_existing,
                only_kinds=only_kinds,
                skip_kinds=skip_kinds,
                keep_raw_html=keep_raw_html,
            )
        except Exception as e:
            log(f"  消息 {msg_id} 处理崩溃: {e}")
            record = {"msg_id": msg_id, "kinds": ["crash"], "saved": [], "errors": [str(e)]}
        record["sg_seq"] = sg_seq
        record["date"] = s["date_text"]
        record["yyyymmdd"] = yyyymmdd
        record["ts"] = datetime.now().isoformat()
        # 评论
        if capture_comments and msg.get("hasComments"):
            try:
                cmt_records = await process_comments(
                    page, loc, msg_id, msg_dir / "comments",
                    skip_existing=skip_existing,
                    only_kinds=only_kinds,
                    skip_kinds=skip_kinds,
                    keep_raw_html=keep_raw_html,
                )
                record["comments_count"] = len(cmt_records)
                for cr in cmt_records:
                    cr["sg_seq"] = sg_seq
                    cr["ts"] = datetime.now().isoformat()
                    manifest_fp.write(json.dumps(cr, ensure_ascii=False) + "\n")
            except Exception as e:
                log(f"  评论处理崩溃: {e}")
                record["errors"] = record.get("errors", []) + [f"comments: {e}"]
        manifest_fp.write(json.dumps(record, ensure_ascii=False) + "\n")
        manifest_fp.flush()
        processed_ids.add(msg_id)
        msg_records.append(record)
    if msg_records:
        try:
            await write_group_readme(group_dir, msg_records)
        except Exception as e:
            log(f"  README 生成失败: {e}")


# ---------------- 滚动 ----------------


async def scroll_to_top_and_wait(page: Page, prev_first_id: Optional[str], max_wait_ms: int = 6000) -> tuple[bool, Optional[str]]:
    """滚到列表顶部加载更老的消息。"""
    await page.evaluate(
        "() => { const ml = document.querySelector('#MiddleColumn .Transition_slide-active div.Transition.MessageList'); if (ml) ml.scrollTop = 0; }"
    )
    deadline = asyncio.get_event_loop().time() + max_wait_ms / 1000
    new_first = prev_first_id
    while asyncio.get_event_loop().time() < deadline:
        await page.wait_for_timeout(400)
        cur = await page.evaluate(
            "() => document.querySelector('#MiddleColumn .Transition_slide-active .message-list-item[data-message-id]')?.getAttribute('data-message-id') || null"
        )
        if cur and cur != prev_first_id:
            return True, cur
    return False, new_first


async def scroll_to_bottom_and_wait(page: Page, prev_last_id: Optional[str], max_wait_ms: int = 6000) -> tuple[bool, Optional[str]]:
    """滚到列表底部加载更新的消息。"""
    await page.evaluate(
        """() => {
            const ml = document.querySelector('#MiddleColumn .Transition_slide-active div.Transition.MessageList');
            if (ml) ml.scrollTop = ml.scrollHeight;
        }"""
    )
    deadline = asyncio.get_event_loop().time() + max_wait_ms / 1000
    new_last = prev_last_id
    while asyncio.get_event_loop().time() < deadline:
        await page.wait_for_timeout(400)
        cur = await page.evaluate(
            """() => {
                const all = document.querySelectorAll('#MiddleColumn .Transition_slide-active .message-list-item[data-message-id]');
                return all.length ? all[all.length-1].getAttribute('data-message-id') : null;
            }"""
        )
        if cur and cur != prev_last_id:
            return True, cur
    return False, new_last


# ---------------- 进度记录 ----------------


def load_progress(out_dir: Path) -> dict:
    p = out_dir / "progress.json"
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_progress(out_dir: Path, data: dict) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    data = dict(data)
    data["updated_at"] = datetime.now().isoformat()
    (out_dir / "progress.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------------- 转发到进度群 ----------------


async def _close_any_modal(page: Page) -> None:
    """关闭所有打开的 modal / menu，确保下一次交互能拿到点击。"""
    for _ in range(4):
        try:
            visible = await page.evaluate(
                """() => {
                    const m = document.querySelector('.Modal.shown.open, .Modal.ChatOrUserPicker.shown.open, .Menu.MessageContextMenu');
                    return !!m;
                }"""
            )
        except Exception:
            return
        if not visible:
            return
        try:
            await page.keyboard.press("Escape")
        except Exception:
            pass
        await page.wait_for_timeout(250)


async def forward_message_to(page: Page, msg_locator: Locator, progress_chat_name: str, timeout_ms: int = 30000) -> bool:
    """
    在消息上点 Forward 按钮 → 在 picker 中搜索并选中 progress 群 → 点 Send。返回是否成功。
    """
    await _close_any_modal(page)
    try:
        await msg_locator.scroll_into_view_if_needed(timeout=5000)
    except Exception:
        pass
    fb = msg_locator.locator('button[aria-label="Forward"]').first
    try:
        if await fb.count() == 0:
            log(f"  ⚠ 无 Forward 按钮，跳过转发")
            return False
        await fb.click(timeout=8000)
    except Exception as e:
        log(f"  ⚠ 点 Forward 按钮失败: {e}")
        await _close_any_modal(page)
        return False
    # 直接等 picker-item 出现（比等 Modal root 稳，root 有 opacity 过渡）
    picker_item = page.locator(f".ChatOrUserPicker-item:has-text('{progress_chat_name}')").first
    try:
        await picker_item.wait_for(state="visible", timeout=8000)
    except PlaywrightTimeoutError:
        log(f"  ⚠ Forward picker 未出现「{progress_chat_name}」项")
        await _close_any_modal(page)
        return False
    # 先确保只选中目标群（清掉之前可能残留的勾选，然后只勾目标）
    try:
        # 反勾任何已勾选的项（不包括目标）
        await page.evaluate(
            """(name) => {
                document.querySelectorAll('.ChatOrUserPicker-item').forEach(it => {
                    const cb = it.querySelector('.picker-checkbox');
                    const selected = cb && cb.className.includes('selected');
                    const isTarget = (it.innerText || '').includes(name);
                    if (selected && !isTarget) {
                        it.click();
                    }
                });
            }""",
            progress_chat_name,
        )
        await page.wait_for_timeout(300)
    except Exception:
        pass
    # 勾选目标项（用 playwright 原生 click，发出 trusted event）
    try:
        await picker_item.click(timeout=5000, force=True)
    except Exception as e:
        log(f"  ⚠ 点 picker item 失败: {e}")
        await _close_any_modal(page)
        return False
    await page.wait_for_timeout(600)
    # 点 FORWARD 进入"待发送"状态（picker-footer-button 文本是 "FORWARD"）
    try:
        await page.locator(".Modal.ChatOrUserPicker .picker-footer-button").first.click(timeout=8000, force=True)
    except Exception as e:
        log(f"  ⚠ 点 FORWARD 失败: {e}")
        await _close_any_modal(page)
        return False
    # 等 modal 关闭（Telegram 会跳到目标聊天，输入框旁显示"Send Message"按钮）
    try:
        await page.locator(".Modal.ChatOrUserPicker.shown.open").wait_for(state="detached", timeout=10000)
    except PlaywrightTimeoutError:
        await _close_any_modal(page)
    await page.wait_for_timeout(800)
    # 等 URL 切到目标聊天
    deadline = asyncio.get_event_loop().time() + 5
    while asyncio.get_event_loop().time() < deadline:
        cur_hash = await page.evaluate("() => location.hash")
        if cur_hash and "1001395144198" not in cur_hash:  # 离开了源频道
            break
        await page.wait_for_timeout(300)
    # 关键的最后一步：点"Send Message"按钮真正发送
    send_btn = page.locator("button[aria-label='Send Message']").first
    try:
        await send_btn.wait_for(state="visible", timeout=8000)
    except PlaywrightTimeoutError:
        log("  ⚠ Send Message 按钮未出现")
        return False
    try:
        await send_btn.click(timeout=5000)
    except Exception as e:
        log(f"  ⚠ 点 Send Message 失败: {e}")
        return False
    await page.wait_for_timeout(1200)
    return True


# ---------------- 主流程 ----------------


async def open_via_progress_anchor(page: Page, progress_chat_name: str) -> tuple[bool, Optional[str], Optional[str]]:
    """
    进入"下载进度"群 → 点击最后一条转发消息的 Focus 箭头 → 跳到目标群。
    返回 (success, anchor_msg_id, target_hash)。
    anchor_msg_id 表示"已处理到的最末消息"，新一轮要从 anchor_msg_id + 1 开始处理。
    """
    log(f"打开 Telegram Web，定位进度群「{progress_chat_name}」...")
    await page.goto("https://web.telegram.org/a/", wait_until="domcontentloaded", timeout=60000)
    # 等 sidebar 出现且加载完
    progress_href = None
    for retry in range(20):
        await page.wait_for_timeout(1000)
        progress_href = await page.evaluate(
            """(name) => {
                const links = Array.from(document.querySelectorAll('a[href^="#"]'));
                for (const a of links) {
                    if ((a.innerText || '').includes(name)) return a.getAttribute('href');
                }
                return null;
            }""",
            progress_chat_name,
        )
        if progress_href:
            break
        # 偶尔需要触发左侧滚动让 chat 出现
        if retry == 5:
            log(f"  等待 sidebar 加载…（已等 {retry+1}s）")
    if not progress_href:
        log(f"未在 sidebar 找到「{progress_chat_name}」聊天，请先建立这个会话并放至少一条转发种子消息")
        return False, None, None
    log(f"  进度群 hash = {progress_href}")
    await page.locator(f"a[href='{progress_href}']").first.click()
    await page.wait_for_timeout(2500)
    # 等消息列表加载
    try:
        await page.locator(MESSAGE_LIST).first.wait_for(state="visible", timeout=15000)
    except PlaywrightTimeoutError:
        log("  进度群消息列表未加载")
        return False, None, None
    # 滚动到底确保看到最新一条
    await page.evaluate(
        """() => {
            const ml = document.querySelector('#MiddleColumn .Transition_slide-active div.Transition.MessageList');
            if (ml) ml.scrollTop = ml.scrollHeight;
        }"""
    )
    await page.wait_for_timeout(1000)
    # 找最后一条非 Action 非 placeholder（id 必须是纯整数）的消息
    last_id = None
    for retry in range(8):
        last_id = await page.evaluate(
            """() => {
                const msgs = Array.from(document.querySelectorAll('#MiddleColumn .Transition_slide-active .message-list-item[data-message-id]'));
                for (let i = msgs.length - 1; i >= 0; i--) {
                    if (msgs[i].className.includes('ActionMessage')) continue;
                    const id = msgs[i].getAttribute('data-message-id');
                    if (/^\\d+$/.test(id)) return id;  // 纯整数，跳过 978.000001 这种 placeholder
                }
                return null;
            }"""
        )
        if last_id:
            break
        await page.wait_for_timeout(500)
    if not last_id:
        log("  进度群里没有可用的转发消息")
        return False, None, None
    log(f"  进度群最后一条 msg id={last_id}（点其 Focus 按钮跳源消息）")
    focus_btn = page.locator(f"#message-{last_id} button[aria-label='Focus message']").first
    try:
        if await focus_btn.count() == 0:
            # 兜底：点 .message-title.interactive（forwarded from header）
            forwarded_header = page.locator(f"#message-{last_id} .message-title").first
            if await forwarded_header.count() > 0:
                await forwarded_header.click(timeout=5000)
            else:
                log("  无 Focus 按钮也无 forwarded header，跳转失败")
                return False, None, None
        else:
            await focus_btn.click(timeout=5000)
    except Exception as e:
        log(f"  点 Focus 按钮失败: {e}")
        return False, None, None
    # 等 URL 变化 + 新消息列表加载
    await page.wait_for_timeout(3000)
    new_hash = await page.evaluate("() => location.hash")
    if new_hash == progress_href:
        log("  Focus 点击后 URL 未变，跳转可能失败")
        return False, None, None
    log(f"  已跳到目标 hash = {new_hash}")
    try:
        await page.locator(f"{MESSAGE_LIST} {DATE_GROUP}").first.wait_for(state="attached", timeout=20000)
    except PlaywrightTimeoutError:
        log("  目标群消息列表未加载")
        return False, None, None
    await page.wait_for_timeout(1500)
    # 拿 viewport center 的 msg id 作为 anchor
    anchor = await page.evaluate(
        """() => {
            const all = document.querySelectorAll('#MiddleColumn .Transition_slide-active .message-list-item[data-message-id]');
            const vh = window.innerHeight;
            for (const m of all) {
                const r = m.getBoundingClientRect();
                if (r.top < vh/2 && r.bottom > vh/2) {
                    const id = m.getAttribute('data-message-id');
                    if (id && /^\\d+$/.test(id)) return id;
                }
            }
            // fallback: any with explicit focused class
            for (const m of all) {
                if (m.className.includes('focused') || m.className.includes('Focused')) {
                    return m.getAttribute('data-message-id');
                }
            }
            return null;
        }"""
    )
    if anchor and anchor.isdigit():
        log(f"  anchor msg_id={anchor}（已下载到这里，下一轮从 {int(anchor)+1} 开始）")
        return True, anchor, new_hash
    log("  未能识别 anchor msg id；使用 DOM 中最大 msg id 作为兜底")
    fallback = await page.evaluate(
        """() => {
            const all = document.querySelectorAll('#MiddleColumn .Transition_slide-active .message-list-item[data-message-id]');
            let best = null;
            for (const m of all) {
                const id = m.getAttribute('data-message-id');
                if (/^\\d+$/.test(id) && (best === null || parseInt(id) > parseInt(best))) best = id;
            }
            return best;
        }"""
    )
    return True, fallback, new_hash


async def run(args) -> None:
    out_base = Path(args.out_base)
    user_data_dir = Path(args.user_data_dir)
    only_kinds = set(s.strip() for s in (args.only_kinds or "").split(",") if s.strip()) or None
    skip_kinds = set(s.strip() for s in (args.skip_kinds or "").split(",") if s.strip()) or None
    progress_chat_name = args.progress_chat_name
    forward_progress = args.forward_progress

    async with async_playwright() as p:
        # playwright 默认把下载临时落在 user_data_dir 旁；指到 out_base 同盘避免占 C/D
        tmp_dl_dir = out_base / "_pw_downloads"
        tmp_dl_dir.mkdir(parents=True, exist_ok=True)
        ctx = await p.chromium.launch_persistent_context(
            user_data_dir=str(user_data_dir),
            headless=args.headless,
            channel=args.browser_channel if args.browser_channel else None,
            accept_downloads=True,
            downloads_path=str(tmp_dl_dir),
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        try:
            ok, anchor, target_hash = await open_via_progress_anchor(page, progress_chat_name)
            if not ok or not target_hash:
                log("入口跳转失败，退出。请确认「下载进度」群里至少有一条种子转发消息。")
                return
            anchor_int = int(anchor) if anchor and anchor.isdigit() else 0
            channel_title = await page.title()
            channel_slug = slugify(channel_title)
            out_dir = out_base / channel_slug
            out_dir.mkdir(parents=True, exist_ok=True)
            manifest_path = out_dir / "manifest.jsonl"
            (out_dir / "_channel_summary.txt").write_text(
                f"channel_title={channel_title}\ntarget_hash={target_hash}\nprogress_chat={progress_chat_name}\nstarted={datetime.now().isoformat()}\n",
                encoding="utf-8",
            )
            log(f"输出目录：{out_dir}")
            log(f"anchor={anchor_int}（已下载到这条，新一轮从 > {anchor_int} 开始）")
            progress = load_progress(out_dir)
            progress.update({
                "channel_title": channel_title,
                "target_hash": target_hash,
                "last_anchor_msg_id": anchor_int,
            })
            save_progress(out_dir, progress)

            processed_ids: set[str] = set()
            global_sg_seq = int(progress.get("last_sg_seq") or 0)
            no_new_rounds = 0

            with manifest_path.open("a", encoding="utf-8") as manifest_fp:
                for round_idx in range(args.scroll_max):
                    # 防护：必须在目标群 hash 才能 extract，否则会误采"下载进度"群消息
                    cur_hash = await page.evaluate("() => location.hash")
                    if cur_hash != target_hash:
                        log(f"当前 hash={cur_hash} 不是 target={target_hash}，重走 entry 重定位")
                        ok2, new_anchor, new_target_hash = await open_via_progress_anchor(page, progress_chat_name)
                        if not ok2:
                            log("  ⚠ entry-flow 重定位失败，退出")
                            break
                        if new_anchor and new_anchor.isdigit():
                            anchor_int = max(anchor_int, int(new_anchor))
                        target_hash = new_target_hash or target_hash
                        continue
                    data = await page.evaluate(EXTRACT_MESSAGES_JS)
                    if not data.get("containerFound"):
                        log("DOM 中没有 date-group，等待 …")
                        await page.wait_for_timeout(1000)
                        continue
                    first_id = data.get("firstVisibleId")
                    last_id = data.get("lastVisibleId")
                    log(f"round={round_idx} groups={len(data['dateGroups'])} first={first_id} last={last_id} anchor={anchor_int}")
                    # 把消息按 msg_id 升序展开成一个 flat list（只保留 id > anchor 的）
                    flat: list[dict] = []
                    for dg in data["dateGroups"]:
                        date_text = dg.get("dateText", "")
                        yyyymmdd = date_to_yyyymmdd(date_text)
                        for msg in dg["items"]:
                            mid = msg["id"]
                            if not mid.isdigit():
                                continue
                            v = int(mid)
                            if v <= anchor_int:
                                continue
                            if mid in processed_ids:
                                continue
                            flat.append({"msg": msg, "yyyymmdd": yyyymmdd, "date_text": date_text, "int_id": v})
                    flat.sort(key=lambda x: x["int_id"])

                    # 拆分 sender-group：按 first-in-group / last-in-group 分段
                    current_sg: list[dict] = []
                    sgs: list[list[dict]] = []
                    for item in flat:
                        if item["msg"].get("isFirstInGroup") and current_sg:
                            sgs.append(current_sg)
                            current_sg = []
                        current_sg.append(item)
                        if item["msg"].get("isLastInGroup"):
                            sgs.append(current_sg)
                            current_sg = []
                    # 余下的零散段（DOM 没看到 last-in-group 的）暂时不处理，等下一轮滚出来
                    leftover = bool(current_sg)

                    if not sgs:
                        no_new_rounds += 1
                        if no_new_rounds >= 3:
                            log("连续多轮无新可处理 sender-group，应已到底。结束。")
                            break
                        # 向下滚加载更多
                        changed, _ = await scroll_to_bottom_and_wait(page, last_id)
                        await page.wait_for_timeout(800)
                        continue
                    no_new_rounds = 0

                    processed_any_this_round = False
                    # 每轮至多处理一个 sg，处理后转发并通过 entry-flow 重定位 anchor，再 re-extract
                    for sg in sgs[:1]:
                        if args.max_groups is not None and global_sg_seq >= args.max_groups:
                            break
                        await process_sender_group(
                            page, sg, out_dir, manifest_fp,
                            global_sg_seq, processed_ids,
                            skip_existing=args.skip_existing,
                            only_kinds=only_kinds,
                            skip_kinds=skip_kinds,
                            keep_raw_html=args.keep_raw_html,
                            capture_comments=args.capture_comments,
                            dry_run=args.dry_run,
                        )
                        # 转发该组最后一条（失败则按倒序兜底其它消息），仅在成功转发后推进 anchor
                        forwarded_id: Optional[str] = None
                        if forward_progress and not args.dry_run:
                            for cand in reversed(sg):
                                cmid = cand["msg"]["id"]
                                if not cmid.isdigit():
                                    continue
                                try:
                                    cloc = page.locator(f"{MESSAGE_LIST_ROOT} #message-{cmid}").first
                                    if await cloc.count() == 0:
                                        continue
                                    ok2 = await forward_message_to(page, cloc, progress_chat_name)
                                    if ok2:
                                        forwarded_id = cmid
                                        log(f"  ✓ 已转发 msg {cmid} 到「{progress_chat_name}」")
                                        break
                                    else:
                                        log(f"  ⚠ 转发 msg {cmid} 失败，尝试组内其它消息")
                                except Exception as e:
                                    log(f"  ⚠ 转发异常 msg {cmid}: {e}")
                            if forwarded_id is None:
                                log(f"  ✗ 全组转发都失败；为避免丢消息，不推进 anchor，下一轮重试")
                        else:
                            # 不转发时，仍用 sg 最后一条作为本地推进点
                            last_id = sg[-1]["msg"]["id"]
                            if last_id.isdigit():
                                forwarded_id = last_id
                        if forwarded_id is not None and forwarded_id.isdigit():
                            anchor_int = max(anchor_int, int(forwarded_id))
                            global_sg_seq += 1
                            processed_any_this_round = True
                            progress.update({
                                "last_anchor_msg_id": anchor_int,
                                "last_sg_seq": global_sg_seq,
                                "last_msg_id": forwarded_id,
                            })
                            save_progress(out_dir, progress)

                    if args.max_groups is not None and global_sg_seq >= args.max_groups:
                        log(f"达到 max-groups={args.max_groups}，停止")
                        break

                    # 处理完后重走 entry flow 重定位 anchor + DOM
                    if processed_any_this_round:
                        ok2, new_anchor, new_target_hash = await open_via_progress_anchor(page, progress_chat_name)
                        if not ok2:
                            log("  ⚠ entry-flow 重定位失败，退出")
                            break
                        if new_anchor and new_anchor.isdigit():
                            anchor_int = max(anchor_int, int(new_anchor))
                        target_hash = new_target_hash or target_hash
                    else:
                        # 没处理任何 sg：向下滚加载更新消息
                        await scroll_to_bottom_and_wait(page, last_id)
                        await page.wait_for_timeout(800)

            log(f"完成。处理至 anchor={anchor_int}，sender-group 累计={global_sg_seq}")
        finally:
            try:
                await ctx.close()
            except Exception:
                pass


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Telegram Web 频道消息与媒体采集（从进度群锚点开始向新方向逐条采集）")
    p.add_argument("--progress-chat-name", default="下载进度", help="进度锚点群名（sidebar 文本匹配）")
    p.add_argument("--out-base", default=str(DEFAULT_OUT_BASE))
    p.add_argument("--user-data-dir", default=str(DEFAULT_USER_DATA_DIR))
    p.add_argument("--browser-channel", default="chrome", help="chrome / msedge / 空（用 playwright chromium）")
    p.add_argument("--max-groups", type=int, default=None, help="本次最多处理 sender-group 数")
    p.add_argument("--scroll-max", type=int, default=500, help="最多滚动轮数")
    p.add_argument("--headless", action="store_true")
    p.add_argument("--skip-existing", action="store_true", default=True)
    p.add_argument("--no-skip-existing", action="store_false", dest="skip_existing")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--only-kinds", default="")
    p.add_argument("--skip-kinds", default="")
    p.add_argument("--keep-raw-html", action="store_true", help="保留每条消息原始 outerHTML 到 _raw.html")
    p.add_argument("--no-capture-comments", dest="capture_comments", action="store_false")
    p.add_argument("--capture-comments", dest="capture_comments", action="store_true", default=True)
    p.add_argument("--no-forward-progress", dest="forward_progress", action="store_false",
                   help="不要把每组最后一条转发到进度群（仅本地 progress.json 记录）")
    p.add_argument("--forward-progress", dest="forward_progress", action="store_true", default=True)
    return p.parse_args()


def main() -> None:
    args = parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
