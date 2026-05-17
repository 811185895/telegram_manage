"""
扫所有 group_<yyyymmdd>_<sgseq>__<first_msg_id> 目录，
取其中第一条 msg 的 text 作为后缀（前 N 字符）追加到目录名。

最终格式: group_<yyyymmdd>_<sgseq>__<first_msg_id>__<标题摘要>

用法:
  python rename_groups_with_title.py --dry-run     # 只列改名计划，不动
  python rename_groups_with_title.py               # 实际改名
"""
from __future__ import annotations

import argparse
import json
import re
from datetime import datetime
from pathlib import Path

DEFAULT_OUT_BASE = Path(r"F:\telegram_download")
DEFAULT_CHANNEL_SLUG = "恋爱心理学_追爱脱单_视频教程"
TITLE_LEN = 40


def log(m: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {m}", flush=True)


def safe(s: str, max_len: int = 80) -> str:
    s = (s or "").strip()
    for c in r'\/:*?"<>|':
        s = s.replace(c, "_")
    s = s.replace("\n", " ").replace("\r", "")
    s = re.sub(r"\s+", " ", s).strip()
    if len(s) > max_len:
        s = s[:max_len].rstrip()
    return s or "unnamed"


def extract_title(group_dir: Path) -> str:
    """从 group_dir 内第一个 msg_dir 抽出 text 标题（前 40 字符）。"""
    # 找 msg_dir 按 msg_id 升序，取最小那个
    msg_dirs = []
    for sub in group_dir.iterdir():
        if not sub.is_dir():
            continue
        m = re.match(r"^msg_(\d+)__", sub.name)
        if m:
            msg_dirs.append((int(m.group(1)), sub))
    if not msg_dirs:
        return ""
    msg_dirs.sort(key=lambda x: x[0])
    first_msg_dir = msg_dirs[0][1]
    # 优先看 meta.json 的 text 字段
    meta_path = first_msg_dir / "meta.json"
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            text = (meta.get("text") or "").strip()
            if text:
                return text
        except Exception:
            pass
    # 兜底：从 msg_dir 名字解析 text_pre
    m = re.match(r"^msg_\d+__(.*)$", first_msg_dir.name)
    if m:
        return m.group(1)
    return ""


def needs_rename(group_dir: Path) -> tuple[bool, str]:
    """如果目录名已经有 4 个 `_` 段后还有非空内容，认为已带标题。"""
    name = group_dir.name
    # 当前格式 group_<date>_<seq>__<msgid> 共 4 段（含 group prefix）
    # 加了标题后多一段 __<title>
    parts = name.split("__")
    # group_xxx_xxx, msgid, [title]
    if len(parts) >= 3 and parts[2]:
        return False, parts[2]  # 已有标题
    title = extract_title(group_dir)
    return bool(title), title


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--out-base", default=str(DEFAULT_OUT_BASE))
    p.add_argument("--channel-slug", default=DEFAULT_CHANNEL_SLUG)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--title-len", type=int, default=TITLE_LEN)
    return p.parse_args()


def main() -> None:
    args = parse_args()
    channel_root = Path(args.out_base) / args.channel_slug
    log(f"scan {channel_root}")
    if not channel_root.exists():
        log("不存在，退出")
        return
    plan: list[tuple[Path, Path]] = []
    skipped_no_title = 0
    skipped_already = 0
    for sub in channel_root.iterdir():
        if not sub.is_dir() or not sub.name.startswith("group_"):
            continue
        # 当前格式：group_<date>_<seq>__<msgid>  (split by '__' → 2 parts)
        # 已重命名：group_<date>_<seq>__<msgid>__<title>  (3 parts)
        parts = sub.name.split("__")
        if len(parts) >= 3 and parts[2]:
            skipped_already += 1
            continue
        title = extract_title(sub)
        if not title:
            skipped_no_title += 1
            continue
        title = safe(title, args.title_len)
        new_name = f"{sub.name}__{title}"
        new_path = sub.parent / new_name
        if new_path.exists() and new_path != sub:
            log(f"  ⚠ 冲突，跳过 {sub.name} (目标 {new_name} 已存在)")
            continue
        plan.append((sub, new_path))
    log(f"待重命名: {len(plan)}; 已有标题: {skipped_already}; 无标题: {skipped_no_title}")
    if args.dry_run:
        for src, dst in plan[:8]:
            log(f"  {src.name} → {dst.name}")
        if len(plan) > 16:
            log(f"  ...(共 {len(plan)} 个)")
        for src, dst in plan[-8:]:
            if len(plan) > 8:
                log(f"  {src.name} → {dst.name}")
        log("dry-run 退出")
        return
    ok = 0
    fail = 0
    for src, dst in plan:
        try:
            src.rename(dst)
            ok += 1
        except Exception as e:
            log(f"  ⚠ rename 失败 {src.name}: {e}")
            fail += 1
    log(f"完成: ok={ok} fail={fail}")


if __name__ == "__main__":
    main()
