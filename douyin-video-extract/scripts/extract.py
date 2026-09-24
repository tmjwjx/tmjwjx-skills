#!/usr/bin/env python3
"""全流程编排：抖音链接列表 → 解析 → 下载视频 → 语音转文字（两阶段批量）。

用法:
  python3 extract.py "<分享链接1>" "<分享文本2>" ...          # 单条或多条
  python3 extract.py --list links.txt --output ~/douyin_out   # 从文件批量读链接（每行一条）
  python3 extract.py "<链接>" --no-download                   # 只要文字稿

流程（批量时两阶段，转写不再阻塞下一条解析）:
  阶段1 逐条：去重检查 → TikHub 解析（计费）→ curl 下载视频
  阶段2 批量：待转写视频按 10 个一组提交百炼（一个任务）→ 统一轮询 → 分发 transcript.txt

每条视频产出（目录 <output>/<aweme_id>_<标题slug>/）:
  video.mp4        无水印视频（--no-download 则跳过）
  transcript.txt   语音转写的文字稿
  meta.json        标题/作者/互动数据/直链等元数据

说明:
  - 去重：URL→aweme_id 索引（.index.json），产物完整即跳过，省一次计费解析；--force 全部重跑（会再次计费）
  - 直链有时效：缓存 meta 超 2 小时视为直链过期，重跑会自动重新解析拿新直链；
    skill 不含 OSS 功能（百炼只收 URL），确需绕开时效请自行传 OSS 后跑 aliyun_asr.py --url
  - 转写等待超时不会丢任务，用 task_id 恢复:
    python3 aliyun_asr.py --task-id <id> --json
  - 退出码: 0 全部成功 | 1 无有效链接 | 3 部分失败（详见日志）
"""
import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
MIN_VIDEO_BYTES = 100_000  # 小于此值视为 CDN 错误页而非视频
ASR_BATCH = 10             # 百炼单任务最大 file_urls 数
MAX_LINK_AGE = 2 * 3600    # 直链有效期通常几小时，缓存 meta 超过 2h 视为直链已过期
URL_RE = re.compile(
    r"https?://(?:v\.douyin\.com|www\.douyin\.com|www\.iesdouyin\.com)/[^\s，,；;）)】\"']+",
    re.I,
)


def run(cmd: list, input_text=None) -> tuple:
    p = subprocess.run(cmd, capture_output=True, text=True, input=input_text)
    return p.returncode, p.stdout, p.stderr


def sanitize(name: str, fallback: str) -> str:
    name = re.sub(r'[\\/:*?"<>|#\r\n]+', "", name).strip()
    return (name[:40] or fallback).strip()


def collect_inputs(args) -> list:
    texts = list(args.links)
    if args.list:
        texts.extend(Path(args.list).read_text().splitlines())
    urls = []
    for t in texts:
        m = URL_RE.search(t or "")
        if m:
            urls.append(m.group(0).rstrip(".,。"))
    return urls


def load_index(out_dir: Path) -> dict:
    idx = out_dir / ".index.json"
    if idx.exists():
        try:
            return json.loads(idx.read_text())
        except Exception:
            pass
    return {}


def save_index(out_dir: Path, index: dict) -> None:
    (out_dir / ".index.json").write_text(json.dumps(index, ensure_ascii=False, indent=2))


def parse_and_download(url: str, out_dir: Path, args, index: dict):
    """阶段1：解析+下载。成功返回 {"meta","vdir","url"}，跳过/失败返回 None。"""
    # 去重三级：有文字稿=完成 → 有缓存meta=只补转写（免费） → 否则完整重跑（解析再计费）
    known_id = index.get(url)
    if known_id and not args.force:
        existing = list(out_dir.glob(f"{known_id}_*"))
        if existing:
            vdir = existing[0]
            mpath = vdir / "meta.json"
            if (vdir / "transcript.txt").exists():
                print(f"  ⏭ 已完成（{vdir.name}），跳过")
                return None
            if mpath.exists() and (not args.download or (vdir / "video.mp4").exists()):
                age = time.time() - mpath.stat().st_mtime
                if age > MAX_LINK_AGE:
                    print(f"  ⏳ 缓存直链大概率已过期（meta 已 {int(age / 3600)} 小时），重新解析拿新直链")
                    # 落到下面的完整重跑
                else:
                    try:
                        meta = json.loads(mpath.read_text())
                        print(f"  ↩️ 缺文字稿，用缓存 meta 直接补转写（不重新解析）: {vdir.name}")
                        return {"meta": meta, "vdir": vdir, "url": url}
                    except Exception:
                        pass
            print(f"  ↩️ 产物不完整，重新处理: {vdir.name}")

    rc, stdout, stderr = run(
        ["python3", str(HERE / "tikhub_parse.py"), url, "--key", args.tikhub_key]
    )
    if rc != 0:
        print(f"  ❌ 解析失败: {stderr.strip()[:200]}")
        return None
    meta = json.loads(stdout)
    index[url] = meta["aweme_id"]
    save_index(out_dir, index)

    vdir = out_dir / f"{meta['aweme_id']}_{sanitize(meta['desc'], meta['aweme_id'])}"
    vdir.mkdir(parents=True, exist_ok=True)
    (vdir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2))

    # 下载（直接用解析结果里的直链，不再重复调用计费接口）
    # 抖音 CDN 会按 TLS 指纹拦截 Python urllib，必须用 curl
    if args.download:
        if meta.get("play_addr"):
            done_vid = vdir / "video.mp4"
            r = subprocess.run(
                ["curl", "-sSL", "--max-time", "600", "-A", UA, "-o", str(done_vid), meta["play_addr"]]
            )
            size = done_vid.stat().st_size if done_vid.exists() else 0
            if r.returncode == 0 and size >= MIN_VIDEO_BYTES:
                print(f"  🎬 视频已下载: {done_vid.name} ({size / 1e6:.1f} MB)")
            else:
                if done_vid.exists():
                    done_vid.unlink()
                print(f"  ⚠ 下载失败（curl 退出码 {r.returncode}，{size} 字节，疑似错误页）")
                return None
        else:
            print("  ⚠ 无 play_addr（可能为图集），跳过下载")
    return {"meta": meta, "vdir": vdir, "url": url}


def run_asr_batch(pending: list, out_dir: Path, args) -> list:
    """阶段2：批量提交+统一轮询转写。返回失败的条目列表。"""
    failed = []
    # 提交：每 ASR_BATCH 个一个任务
    tasks = []  # [(task_id, [entry,...])]
    for i in range(0, len(pending), ASR_BATCH):
        chunk = pending[i:i + ASR_BATCH]
        # 图集类没有 play_addr，无可转写音频，跳过且不算失败
        transcribable = [c for c in chunk if (c["meta"]["play_addr"] or c["meta"]["play_addr_265"])]
        for c in chunk:
            if c not in transcribable:
                print(f"  ⏭ 无可转写音频（图集）: {c['vdir'].name}")
        if not transcribable:
            continue
        chunk = transcribable
        urls = [c["meta"]["play_addr"] or c["meta"]["play_addr_265"] for c in chunk]
        print(f"🎙 提交转写任务（{len(chunk)} 条）…")
        rc, stdout, stderr = run(
            ["python3", str(HERE / "aliyun_asr.py"), "--urls", *urls, "--no-wait",
             "--key", args.dashscope_key]
        )
        if rc == 0:
            tasks.append((stdout.strip(), chunk))
        else:
            print(f"  ❌ 提交失败: {stderr.strip()[:200]}")
            failed.extend(chunk)

    # 轮询：逐任务等待并分发结果
    for task_id, chunk in tasks:
        print(f"🎙 等待任务 {task_id}（{len(chunk)} 条）…")
        rc, stdout, stderr = run(
            ["python3", str(HERE / "aliyun_asr.py"), "--task-id", task_id, "--json",
             "--timeout", str(args.asr_timeout), "--key", args.dashscope_key]
        )
        if rc == 5:
            print(f"  ⏳ 等待超时，任务未丢。稍后恢复: python3 aliyun_asr.py --task-id {task_id} --json")
            failed.extend(chunk)
            continue
        if rc != 0:
            print(f"  ❌ 查询失败: {stderr.strip()[:200]}")
            failed.extend(chunk)
            continue
        by_url = {e.get("file_url"): e for e in json.loads(stdout)}
        for c in chunk:
            u = c["meta"]["play_addr"] or c["meta"]["play_addr_265"]
            entry = by_url.get(u)
            if not entry:
                print(f"  ⚠ 结果中未找到该 URL，跳过: {c['vdir'].name}")
                failed.append(c)
                continue
            if entry.get("status") == "SUCCEEDED" and entry.get("text"):
                (c["vdir"] / "transcript.txt").write_text(entry["text"])
                print(f"  📝 文字稿完成（{len(entry['text'])} 字）→ {c['vdir'].name}")
            else:
                print(f"  ❌ 转写未成功（{entry.get('status')}）: {c['vdir'].name}")
                failed.append(c)
    return failed


def main() -> None:
    ap = argparse.ArgumentParser(description="抖音链接 → 视频 + 文字稿")
    ap.add_argument("links", nargs="*", help="分享链接或整段分享文本，可多个")
    ap.add_argument("--list", help="批量链接文件，每行一条（可含分享文本）")
    ap.add_argument("--output", default="./douyin_output", help="输出目录（默认 ./douyin_output）")
    ap.add_argument("--no-download", dest="download", action="store_false", help="不下载视频，只要文字稿")
    ap.add_argument("--no-asr", action="store_true", help="跳过语音转写")
    ap.add_argument("--force", action="store_true", help="忽略去重全部重跑（解析会再次计费）")
    ap.add_argument("--asr-timeout", type=int, default=600)
    ap.add_argument("--tikhub-key", default="")
    ap.add_argument("--dashscope-key", default="")
    args = ap.parse_args()

    urls = collect_inputs(args)
    if not urls:
        print("没有找到有效抖音链接（用位置参数或 --list 文件）", file=sys.stderr)
        sys.exit(1)

    out_dir = Path(args.output).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"共 {len(urls)} 条链接，输出目录: {out_dir}")

    # 阶段1：逐条解析+下载
    index = load_index(out_dir)
    items, failed = [], []
    for i, url in enumerate(urls, 1):
        print(f"[{i}/{len(urls)}] {url}")
        item = parse_and_download(url, out_dir, args, index)
        if item:
            items.append(item)
        else:
            # 区分「已处理跳过」与「真失败」：parse_and_download 返回 None 但无产物说明失败
            known = index.get(url)
            done = known and any(
                (d / "transcript.txt").exists() or (d / "video.mp4").exists()
                for d in out_dir.glob(f"{known}_*")
            )
            if not done:
                failed.append(url)
        if i < len(urls):
            time.sleep(1)  # 轻微限速，避免高频触发风控

    # 阶段2：批量转写（未完成文字稿的条目）
    if args.no_asr:
        print("⏭ --no-asr，跳过转写")
    else:
        pending = [
            it for it in items
            if args.force or not (it["vdir"] / "transcript.txt").exists()
        ]
        if pending:
            failed.extend(run_asr_batch(pending, out_dir, args))
        else:
            print("⏭ 没有待转写的视频")

    ok_ids = {u for u in urls if u not in failed} - {
        d["url"] for d in failed if isinstance(d, dict)
    }
    if failed:
        print(f"\n{len(failed)} 条未完成。重跑同一命令会自动补做（解析已缓存的不会重复计费）:")
        for u in failed:
            print(f"  {u if isinstance(u, str) else u['vdir'].name}")
        sys.exit(3)
    print(f"\n全部完成 ✅（{len(ok_ids)} 条）")


if __name__ == "__main__":
    main()
