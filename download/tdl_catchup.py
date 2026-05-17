"""
tdl 高速补漏脚本（方案 C+ 的核心）

逻辑：
  1. 扫 F:\telegram_download\<channel>\ 找出所有 msg_<id>__* 子目录
  2. 对每个 msg_dir，看是否已经有 video.mp4 / file_<原名> 等"主媒体"
  3. 没有主媒体的 msg_id 收集到 pending_msg_ids
  4. 用 tdl 批量并发拉所有 pending（-t 8 -l 4 --takeout）
  5. tdl 下到 F:\telegram_download\_tdl_drop\<msg_id>.<ext>
  6. 按 msg_id 移到对应 msg_dir
  7. 触发 audio batch 抽 mp3

使用前提：
  1. F:\telegram_download\<channel>\ 已经存在（由 Playwright main 创建）
  2. tdl 已 login（看 tdl_pipeline.md）

用法:
  python tdl_catchup.py --channel-id 1395144198 --channel-slug 恋爱心理学_追爱脱单_视频教程
  python tdl_catchup.py --dry-run         # 仅列出 pending msg
  python tdl_catchup.py --threads 16      # 调并发
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

SCRIPT_DIR = Path(__file__).resolve().parent
TDL_BIN = SCRIPT_DIR / "bin" / "tdl.exe"
DEFAULT_OUT_BASE = Path(r"F:\telegram_download")
DEFAULT_CHANNEL_ID = "1395144198"  # 去掉 -100 前缀，源自 -1001395144198
DEFAULT_CHANNEL_SLUG = "恋爱心理学_追爱脱单_视频教程"


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def _is_main_media(name: str) -> bool:
    """与主脚本相同：判定 msg_dir 里的某个文件是不是'主媒体'。"""
    if name.startswith("file_"):
        return True
    if name.startswith("video.") or (name.startswith("video") and not name.startswith("video_thumb") and not name.startswith("video_")):
        return True
    if name == "video.mp4":
        return True
    if name.startswith("album_") and "_video" in name and "_audio" not in name and "_photo" not in name:
        return True
    if name in ("photo.png", "photo.jpg", "photo.jpeg", "photo.webp"):
        return True
    return False


def msg_dir_has_main(msg_dir: Path) -> bool:
    try:
        return any(p.is_file() and _is_main_media(p.name) for p in msg_dir.iterdir())
    except Exception:
        return False


def find_pending(channel_root: Path) -> list[tuple[int, Path]]:
    """返回 [(msg_id, msg_dir), ...]，pending 条件：
       - 存在 _video_pending.json / _video_pending_album_*.json 标记，或
       - 没有主媒体文件（兼容历史 main 漏下情况）
    """
    pending: list[tuple[int, Path]] = []
    if not channel_root.exists():
        return pending
    for group_dir in channel_root.iterdir():
        if not group_dir.is_dir() or not group_dir.name.startswith("group_"):
            continue
        for msg_dir in group_dir.iterdir():
            if not msg_dir.is_dir() or not msg_dir.name.startswith("msg_"):
                continue
            m = re.match(r"^msg_(\d+)__", msg_dir.name)
            if not m:
                continue
            msg_id = int(m.group(1))
            # 优先看 pending 标记（video / file / photo / album-video）
            PENDING_MARKERS = ("_video_pending.json", "_file_pending.json", "_photo_pending.json")
            has_marker = any(
                p.name in PENDING_MARKERS or p.name.startswith("_video_pending_album_")
                for p in msg_dir.iterdir() if p.is_file()
            )
            if has_marker:
                pending.append((msg_id, msg_dir))
                continue
            # 否则兼容：没有主媒体也算
            if not msg_dir_has_main(msg_dir):
                pending.append((msg_id, msg_dir))
    pending.sort(key=lambda x: x[0])
    return pending


def build_msg_urls(channel_id: str, msg_ids: list[int]) -> list[str]:
    """构造 t.me URL：channel-id 形式 https://t.me/c/<id>/<msg_id>。"""
    base = f"https://t.me/c/{channel_id}"
    return [f"{base}/{mid}" for mid in msg_ids]


def run_tdl_download(
    urls: list[str],
    drop_dir: Path,
    session_name: str,
    threads: int,
    concurrent: int,
    takeout: bool,
    batch_size: int,
    proxy: str = "",
) -> int:
    """分批调用 tdl dl。返回失败数。"""
    drop_dir.mkdir(parents=True, exist_ok=True)
    fail_count = 0
    for batch_start in range(0, len(urls), batch_size):
        batch = urls[batch_start:batch_start + batch_size]
        cmd = [
            str(TDL_BIN), "dl",
            "-n", session_name,
            "-t", str(threads),
            "-l", str(concurrent),
            "-d", str(drop_dir),
            "--continue",
            "--skip-same",
        ]
        if proxy:
            cmd.extend(["--proxy", proxy])
        if takeout:
            cmd.append("--takeout")
        for u in batch:
            cmd.extend(["-u", u])
        log(f"批次 {batch_start//batch_size + 1}/{(len(urls)+batch_size-1)//batch_size}：{len(batch)} 个 URL")
        log(f"  CMD: {TDL_BIN.name} dl -n {session_name} -t {threads} -l {concurrent} ...")
        try:
            r = subprocess.run(cmd, timeout=3600 * 6)  # 单批 6 小时上限
            if r.returncode != 0:
                fail_count += 1
                log(f"  ⚠ tdl 返回 {r.returncode}")
        except subprocess.TimeoutExpired:
            log("  ⚠ tdl 单批超时（6h）")
            fail_count += 1
    return fail_count


def _delete_pending_markers(msg_dir: Path) -> int:
    n = 0
    PENDING = ("_video_pending.json", "_file_pending.json", "_photo_pending.json")
    try:
        for p in msg_dir.iterdir():
            if p.is_file() and (p.name in PENDING or p.name.startswith("_video_pending_album_")):
                try:
                    p.unlink()
                    n += 1
                except Exception:
                    pass
    except Exception:
        pass
    return n


def organize_drops_to_msg_dirs(drop_dir: Path, pending_map: dict[int, Path]) -> tuple[int, int]:
    """把 tdl 下到 drop_dir 的文件按 msg_id 移到对应 msg_dir。
    tdl 默认命名是 <message_id>_<file_name>.<ext> 之类，需要解析 msg_id。
    """
    moved = 0
    unmoved = 0
    if not drop_dir.exists():
        return 0, 0
    for f in drop_dir.iterdir():
        if not f.is_file():
            continue
        # tdl 0.20+ 默认命名: {channel_id}_{msg_id}_{file_name}.ext
        # 或老版: {msg_id}_{file_name}.ext
        m = re.match(r"^\d+_(\d+)_", f.name) or re.match(r"^(\d+)[_\.\-]", f.name)
        if not m:
            log(f"  ⚠ 无法识别 msg_id: {f.name}")
            unmoved += 1
            continue
        mid = int(m.group(1))
        msg_dir = pending_map.get(mid)
        if not msg_dir:
            unmoved += 1
            continue
        # 决定目标文件名
        suffix = f.suffix.lower()
        if suffix in (".mp4", ".mov", ".mkv", ".webm"):
            dest = msg_dir / "video.mp4" if suffix == ".mp4" else (msg_dir / f"video{suffix}")
        elif suffix in (".pdf", ".epub", ".mobi", ".rar", ".zip", ".docx", ".xlsx", ".pptx"):
            # 文件保留原名
            dest = msg_dir / f"file_{f.name}"
        else:
            dest = msg_dir / f.name
        try:
            shutil.move(str(f), str(dest))
            moved += 1
            _delete_pending_markers(msg_dir)
            log(f"  ✓ moved {f.name} → {msg_dir.name}/{dest.name}")
        except Exception as e:
            log(f"  ⚠ move 失败 {f.name}: {e}")
            unmoved += 1
    return moved, unmoved


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="用 tdl 给 Playwright 漏下的视频/文件补漏")
    p.add_argument("--out-base", default=str(DEFAULT_OUT_BASE))
    p.add_argument("--channel-id", default=DEFAULT_CHANNEL_ID, help="去掉 -100 前缀的 channel id（用于构造 t.me/c/<id>/ URL）")
    p.add_argument("--channel-slug", default=DEFAULT_CHANNEL_SLUG)
    p.add_argument("--session-name", default="tg-session", help="tdl session name（默认 tg-session）")
    p.add_argument("--threads", type=int, default=8, help="每文件并发连接数")
    p.add_argument("--concurrent", type=int, default=4, help="同时下载文件数")
    p.add_argument("--no-takeout", dest="takeout", action="store_false")
    p.add_argument("--takeout", action="store_true", default=True)
    p.add_argument("--batch-size", type=int, default=200, help="每次 tdl 调用打包多少 URL")
    p.add_argument("--dry-run", action="store_true", help="只列 pending，不下载")
    p.add_argument("--max", type=int, default=None, help="本次最多处理多少条")
    p.add_argument("--proxy", default="socks5://127.0.0.1:10808", help="代理（默认 V2RayN SOCKS5）")
    p.add_argument("--watch", action="store_true", help="watch 模式：一直跑，每隔 --watch-interval 秒扫一遍")
    p.add_argument("--watch-interval", type=int, default=120)
    return p.parse_args()


def run_once(args, channel_root: Path, out_base: Path) -> int:
    """跑一轮 catchup，返回 pending 数。"""
    pending = find_pending(channel_root)
    if args.max is not None:
        pending = pending[:args.max]
    if not pending:
        return 0
    log(f"pending 数量: {len(pending)}")
    pending_map = {mid: mdir for mid, mdir in pending}
    if len(pending) > 16:
        for mid, mdir in pending[:8]:
            log(f"  pending: msg_id={mid} dir={mdir.name}")
        log(f"  ... ({len(pending) - 16} more) ...")
        for mid, mdir in pending[-8:]:
            log(f"  pending: msg_id={mid} dir={mdir.name}")
    else:
        for mid, mdir in pending:
            log(f"  pending: msg_id={mid} dir={mdir.name}")
    if args.dry_run:
        log("dry-run，不实际下载")
        return len(pending)
    urls = build_msg_urls(args.channel_id, [mid for mid, _ in pending])
    drop_dir = out_base / "_tdl_drop"
    log(f"tdl 下载到临时目录: {drop_dir}")
    fail = run_tdl_download(
        urls, drop_dir, args.session_name,
        args.threads, args.concurrent, args.takeout, args.batch_size,
        proxy=args.proxy,
    )
    log(f"tdl 完成，失败批次: {fail}")
    log("移动到 msg_dir...")
    moved, unmoved = organize_drops_to_msg_dirs(drop_dir, pending_map)
    log(f"本轮完成: 移动 {moved}，未移动 {unmoved}")
    return len(pending)


def main() -> None:
    args = parse_args()
    if not TDL_BIN.exists():
        log(f"未找到 tdl: {TDL_BIN}")
        sys.exit(1)
    out_base = Path(args.out_base)
    channel_root = out_base / args.channel_slug
    log(f"扫描 {channel_root}")
    if not args.watch:
        run_once(args, channel_root, out_base)
        return
    # watch 模式：永久轮询
    import time as _t
    total = 0
    while True:
        n = run_once(args, channel_root, out_base)
        total += n
        log(f"watch: 累计处理 {total}，sleep {args.watch_interval}s")
        _t.sleep(args.watch_interval)


if __name__ == "__main__":
    main()
