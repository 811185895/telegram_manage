"""
从 tdl chat export 的 JSON 文件导入 channel 元数据，为每条 msg 创建 msg_dir 结构：
  - text.md
  - meta.json
  - _video_pending.json (视频/相册视频，待 tdl_catchup 补下载)
  - _photo_pending.json (单图，待补下载)

sender-group 按"连续 msg 时间差 < 30 分钟"启发式划分。

用法：
  python tdl_import_metadata.py --json full_export_raw.json --skip-msg-id 471
  python tdl_import_metadata.py --json full_export_raw.json --dry-run
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_OUT_BASE = Path(r"F:\telegram_download")
DEFAULT_CHANNEL_SLUG = "恋爱心理学_追爱脱单_视频教程"
SG_GAP_SECONDS = 30 * 60  # 30 min: 同 sender-group


def log(m: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {m}", flush=True)


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


def classify(raw_msg: dict) -> str:
    """返回 video/document/photo/text-only/action。"""
    if raw_msg.get("Action"):
        return "action"
    media = raw_msg.get("Media") or {}
    if media.get("Document"):
        if media.get("Video"):
            return "video"
        return "document"
    if media.get("Photo"):
        return "photo"
    if media.get("WebPage") or media.get("Webpage"):
        return "webpage"
    return "text-only"


def extract_hashtags(text: str) -> list[str]:
    if not text:
        return []
    return re.findall(r"#[\w一-鿿]+", text)


def build_iso(unix_date: int) -> str:
    try:
        return datetime.fromtimestamp(unix_date, tz=timezone(timedelta(hours=8))).isoformat()
    except Exception:
        return ""


def msg_iso_date_part(unix_date: int) -> tuple[str, str]:
    """返回 (yyyymmdd, 'YYYY-MM-DD HH:MM' 北京时间)。"""
    try:
        dt = datetime.fromtimestamp(unix_date, tz=timezone(timedelta(hours=8)))
        return dt.strftime("%Y%m%d"), dt.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return "unknown", ""


def group_by_album_and_sender(msgs: list[dict]) -> list[list[dict]]:
    """
    1) 同 GroupedID 的 msg 合并为一个"逻辑消息"（album）
    2) 然后按时间差 < 30 分钟把多个逻辑消息聚成一个 sender-group
    返回 [sender_group, sender_group, ...]，每个 sender_group 是 [msg_dict, msg_dict, ...]
    （album 内的多 msg 仍然展开成单 msg，但它们 grouped_id 相同会被 process 时合并）
    """
    # 按 id 升序
    sorted_msgs = sorted(msgs, key=lambda m: m["id"])
    groups: list[list[dict]] = []
    current: list[dict] = []
    last_date: Optional[int] = None
    for m in sorted_msgs:
        d = m.get("date", 0)
        if last_date is None or (d - last_date) <= SG_GAP_SECONDS:
            current.append(m)
        else:
            if current:
                groups.append(current)
            current = [m]
        last_date = d
    if current:
        groups.append(current)
    return groups


def import_message(msg: dict, msg_dir: Path, channel_id: int) -> dict:
    """为单条 msg 写 text.md + meta.json + 可能的 pending 标记。返回 stats。"""
    msg_dir.mkdir(parents=True, exist_ok=True)
    raw = msg.get("raw") or {}
    mid = msg["id"]
    text = msg.get("text") or ""
    kind = classify(raw)
    unix = msg.get("date", 0)
    yyyymmdd, iso = msg_iso_date_part(unix)
    media = raw.get("Media") or {}
    doc = media.get("Document") or {}
    photo = media.get("Photo") or {}
    grouped_id = raw.get("GroupedID") or 0
    fwd = raw.get("FwdFrom") or {}
    fwd_from = ""
    if fwd:
        fwd_from = fwd.get("FromName") or fwd.get("PostAuthor") or ""
        if isinstance(fwd.get("FromID"), dict):
            cid = fwd["FromID"].get("ChannelID") or fwd["FromID"].get("UserID")
            if cid and not fwd_from:
                fwd_from = f"id_{cid}"
    is_pinned = bool(raw.get("Pinned"))
    is_edited = bool(raw.get("EditDate") or raw.get("Edit"))
    reply_to = raw.get("ReplyTo")
    reply = None
    if isinstance(reply_to, dict) and reply_to.get("ReplyToMsgID"):
        reply = {"reply_to_msg_id": reply_to.get("ReplyToMsgID")}

    meta = {
        "msg_id": mid,
        "channel_id": channel_id,
        "kind": kind,
        "date_unix": unix,
        "date_iso_bj": iso,
        "yyyymmdd": yyyymmdd,
        "text": text,
        "hashtags": extract_hashtags(text),
        "file_name": msg.get("file") or "",
        "file_size": doc.get("Size") or 0,
        "mime_type": doc.get("MimeType") or "",
        "video_duration": None,
        "grouped_id": grouped_id,
        "is_pinned": is_pinned,
        "is_edited": is_edited,
        "forwarded_from": fwd_from,
        "reply": reply,
        "no_forwards": bool(raw.get("Noforwards")),
    }
    # 提取 duration if video
    for attr in (doc.get("Attributes") or []):
        if isinstance(attr, dict) and "Duration" in attr:
            meta["video_duration"] = attr.get("Duration")

    # text.md
    md_lines = [f"# Message {mid}", ""]
    head = [f"`{iso}` (BJ)"]
    head.append(f"kind: {kind}")
    if meta["file_name"]:
        head.append(f"file: {meta['file_name']}")
    if meta["file_size"]:
        head.append(f"size: {meta['file_size']:,}")
    if meta["video_duration"]:
        head.append(f"duration: {meta['video_duration']}s")
    if is_pinned: head.append("pinned")
    if fwd_from: head.append(f"forwarded from: {fwd_from}")
    md_lines.append(" · ".join(head))
    md_lines.append("")
    if text:
        md_lines.append(text)
    else:
        md_lines.append("_(no text)_")
    if meta["hashtags"]:
        md_lines.append("")
        md_lines.append("Tags: " + " ".join(meta["hashtags"]))
    (msg_dir / "text.md").write_text("\n".join(md_lines) + "\n", encoding="utf-8")
    (msg_dir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    # pending markers for media that needs tdl download
    if kind == "video":
        # 已下载的 video.mp4 存在则跳过
        if not (msg_dir / "video.mp4").exists():
            (msg_dir / "_video_pending.json").write_text(json.dumps({
                "msg_id": mid,
                "channel_id": channel_id,
                "kind": "video",
                "target_path": "video.mp4",
                "file_name": meta["file_name"],
                "file_size": meta["file_size"],
            }, ensure_ascii=False, indent=2), encoding="utf-8")
    elif kind == "document":
        # 已下载的 file_* 存在则跳过
        already = any(p.name.startswith("file_") for p in msg_dir.iterdir() if p.is_file())
        if not already:
            (msg_dir / "_file_pending.json").write_text(json.dumps({
                "msg_id": mid,
                "channel_id": channel_id,
                "kind": "document",
                "file_name": meta["file_name"],
                "file_size": meta["file_size"],
            }, ensure_ascii=False, indent=2), encoding="utf-8")
    elif kind == "photo":
        already = any(p.name.startswith("photo") for p in msg_dir.iterdir() if p.is_file())
        if not already:
            (msg_dir / "_photo_pending.json").write_text(json.dumps({
                "msg_id": mid,
                "channel_id": channel_id,
                "kind": "photo",
            }, ensure_ascii=False, indent=2), encoding="utf-8")

    return {"kind": kind, "has_pending": (msg_dir / "_video_pending.json").exists() or (msg_dir / "_file_pending.json").exists() or (msg_dir / "_photo_pending.json").exists()}


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="把 tdl chat export JSON 导入为 msg_dir 结构")
    p.add_argument("--json", required=True, help="tdl chat export 输出的 JSON 文件路径")
    p.add_argument("--out-base", default=str(DEFAULT_OUT_BASE))
    p.add_argument("--channel-slug", default=DEFAULT_CHANNEL_SLUG)
    p.add_argument("--skip-msg-id", type=int, default=0, help="只处理 msg_id > 该值的（用于增量导入）")
    p.add_argument("--max", type=int, default=None, help="最多处理多少 msg")
    p.add_argument("--dry-run", action="store_true")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    with open(args.json, "r", encoding="utf-8") as f:
        data = json.load(f)
    channel_id = data.get("id") or 0
    msgs = data.get("messages") or []
    log(f"加载 {len(msgs)} 条 msg，channel_id={channel_id}")
    # filter by skip-msg-id
    msgs = [m for m in msgs if m["id"] > args.skip_msg_id]
    if args.max is not None:
        msgs = msgs[:args.max]
    log(f"过滤后待处理: {len(msgs)} 条（msg_id > {args.skip_msg_id}）")
    if not msgs:
        return
    # 跳过 action 消息（用户加入/离开 / channel 创建等）
    msgs = [m for m in msgs if classify(m.get("raw") or {}) != "action"]
    log(f"剔除 action 后: {len(msgs)} 条")
    # 分组 sender-group
    sgs = group_by_album_and_sender(msgs)
    log(f"切分为 {len(sgs)} 个 sender-group（30 分钟间隔）")

    if args.dry_run:
        for i, sg in enumerate(sgs[:5]):
            log(f"  sg[{i}]: {len(sg)} msgs, first id={sg[0]['id']} date={msg_iso_date_part(sg[0]['date'])[1]}")
        log("dry-run 退出")
        return

    out_base = Path(args.out_base)
    channel_root = out_base / args.channel_slug
    channel_root.mkdir(parents=True, exist_ok=True)

    sg_seq = 0
    total_written = 0
    total_pending = 0
    # 找现有最大 sg_seq
    for existing in channel_root.iterdir():
        if existing.is_dir() and existing.name.startswith("group_"):
            m = re.search(r"_(\d{4})__", existing.name)
            if m:
                sg_seq = max(sg_seq, int(m.group(1)) + 1)
    log(f"现有最大 sg_seq+1 = {sg_seq}（从这里继续）")

    for sg in sgs:
        first = sg[0]
        first_id = first["id"]
        yyyymmdd, _ = msg_iso_date_part(first["date"])
        group_dir = channel_root / safe_filename(f"group_{yyyymmdd}_{sg_seq:04d}__{first_id}")
        for msg in sg:
            mid = msg["id"]
            tp = text_preview(msg.get("text") or "", 30)
            msg_dir = group_dir / safe_filename(f"msg_{mid}__{tp}")
            try:
                stats = import_message(msg, msg_dir, channel_id)
                total_written += 1
                if stats["has_pending"]:
                    total_pending += 1
            except Exception as e:
                log(f"  ⚠ 处理 msg {mid} 失败: {e}")
        sg_seq += 1
        if sg_seq % 50 == 0:
            log(f"  ... sg_seq={sg_seq} written={total_written} pending={total_pending}")

    log(f"完成：写入 {total_written} msg，{total_pending} 个 pending（待 tdl_catchup 下载）")


if __name__ == "__main__":
    main()
