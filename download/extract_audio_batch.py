"""
批量扫描已下载的视频，把每个 video.mp4 / album_*_video.* 抽成同目录 mp3。

用法:
  python extract_audio_batch.py                              # 默认扫 F:\telegram_download
  python extract_audio_batch.py --root F:\telegram_download  # 指定根
  python extract_audio_batch.py --workers 4                  # 并发 4 个 ffmpeg
  python extract_audio_batch.py --no-skip-existing           # 强制覆盖已有 mp3
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

DEFAULT_ROOT = Path(r"F:\telegram_download")

# anaconda 自带的 ffmpeg 只有 mp3_mf 编码器（输出 0KB 空 mp3 的 bug），换 chocolatey 的
FFMPEG_CANDIDATES = [
    r"C:\ProgramData\chocolatey\bin\ffmpeg.exe",
    "ffmpeg",
]


def find_ffmpeg() -> str:
    import shutil as _sh
    for c in FFMPEG_CANDIDATES:
        p = _sh.which(c) if not Path(c).is_absolute() else (c if Path(c).exists() else None)
        if p:
            return p
    return "ffmpeg"


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


_FFMPEG_BIN = find_ffmpeg()


def video_to_mp3(video_path: Path, mp3_path: Path, mono: bool = True) -> tuple[bool, str]:
    if not video_path.exists() or video_path.stat().st_size < 1024:
        return False, "video missing or too small"
    cmd = [
        _FFMPEG_BIN, "-y", "-i", str(video_path),
        "-vn",
        "-c:a", "libmp3lame",  # 显式用 libmp3lame，避免回退到 mp3_mf
        "-ac", "1" if mono else "2",
        "-b:a", "64k",
        "-ar", "22050",
        "-f", "mp3",
        str(mp3_path),
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=1800)
        if r.returncode == 0 and mp3_path.exists() and mp3_path.stat().st_size > 0:
            return True, "ok"
        return False, f"rc={r.returncode}: {r.stderr.decode('utf-8','ignore')[-200:]}"
    except FileNotFoundError:
        return False, "ffmpeg not found"
    except subprocess.TimeoutExpired:
        return False, "ffmpeg timeout"
    except Exception as e:
        return False, f"exception: {e}"


def find_video_targets(root: Path, skip_existing: bool) -> list[tuple[Path, Path]]:
    """返回 [(video_path, target_mp3_path), ...]，跳过已存在的 mp3。"""
    targets: list[tuple[Path, Path]] = []
    if not root.exists():
        log(f"root 不存在: {root}")
        return targets
    for video in root.rglob("video.mp4"):
        mp3 = video.with_name("转写_audio.mp3")
        # 兼容旧目录：如果存在不带前缀的 audio.mp3 也算已转
        legacy = video.with_name("audio.mp3")
        if skip_existing and ((mp3.exists() and mp3.stat().st_size > 0) or (legacy.exists() and legacy.stat().st_size > 0)):
            continue
        targets.append((video, mp3))
    # album_<id>_video.<ext>
    for video in root.rglob("album_*_video*"):
        if video.suffix.lower() not in (".mp4", ".mov", ".mkv", ".webm"):
            continue
        stem = video.stem  # album_xxx_video
        if stem.endswith("_video"):
            base = stem[:-len("_video")] + "_audio"
        else:
            base = stem
        mp3 = video.with_name(f"转写_{base}.mp3")
        legacy = video.with_name(f"{base}.mp3")
        if skip_existing and ((mp3.exists() and mp3.stat().st_size > 0) or (legacy.exists() and legacy.stat().st_size > 0)):
            continue
        targets.append((video, mp3))
    return targets


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="批量把 video.mp4 抽成 audio.mp3")
    p.add_argument("--root", default=str(DEFAULT_ROOT))
    p.add_argument("--workers", type=int, default=1)
    p.add_argument("--skip-existing", action="store_true", default=True)
    p.add_argument("--no-skip-existing", action="store_false", dest="skip_existing")
    p.add_argument("--watch", action="store_true",
                   help="一直运行，每隔 --watch-interval 秒扫一遍，新下视频会自动被发现转换")
    p.add_argument("--watch-interval", type=int, default=120, help="watch 模式下的扫描间隔（秒）")
    p.add_argument("--once", dest="watch", action="store_false",
                   help="扫一遍就退出（默认）")
    return p.parse_args()


def process_once(root: Path, workers: int, skip_existing: bool) -> tuple[int, int]:
    targets = find_video_targets(root, skip_existing)
    if not targets:
        return 0, 0
    log(f"待处理: {len(targets)}")
    ok_count = 0
    fail_count = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(video_to_mp3, v, m): (v, m) for v, m in targets}
        for i, fut in enumerate(as_completed(futures), 1):
            v, m = futures[fut]
            ok, msg = fut.result()
            tag = "✓" if ok else "✗"
            log(f"[{i}/{len(targets)}] {tag} {v.parent.parent.name}/{v.parent.name}/{v.name} -> {m.name}: {msg if not ok else ''}")
            if ok:
                ok_count += 1
            else:
                fail_count += 1
    return ok_count, fail_count


def main() -> None:
    args = parse_args()
    root = Path(args.root)
    log(f"scan root: {root}")
    import time as _t
    total_ok = 0
    total_fail = 0
    while True:
        ok, fail = process_once(root, args.workers, args.skip_existing)
        total_ok += ok
        total_fail += fail
        if ok or fail:
            log(f"本轮完成: ok={ok} fail={fail}，累计 ok={total_ok} fail={total_fail}")
        if not args.watch:
            break
        log(f"watch: sleep {args.watch_interval}s 后再扫一遍")
        _t.sleep(args.watch_interval)
    log(f"全部完成: ok={total_ok} fail={total_fail}")


if __name__ == "__main__":
    main()
