#!/usr/bin/env python3
"""TikHub 抖音解析：分享链接/分享文本 → 视频元数据 + 无水印直链，可选下载。

用法:
  python3 tikhub_parse.py "<分享链接或含链接的分享文本>"
  python3 tikhub_parse.py "<链接>" --download --output ~/Downloads

Key 读取优先级: 环境变量 TIKHUB_API_KEY > config.json > 命令行 --key
退出码: 0 成功 | 2 缺 Key/配置错误 | 3 API 调用失败 | 4 下载失败
计费: 每次解析约 $0.001，重复解析同一视频会重复计费，调用方自行按 aweme_id 去重。
"""
import argparse
import json
import os
import re
import subprocess
import sys
import urllib.parse
import urllib.request
from pathlib import Path

API = "https://api.tikhub.io/api/v1/douyin/app/v3/fetch_one_video_by_share_url"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"

# 从任意分享文本里抠出抖音链接（短链/网页长链/分享口令）
URL_RE = re.compile(
    r"https?://(?:v\.douyin\.com|www\.douyin\.com|www\.iesdouyin\.com)/[^\s，,；;）)】\"']+",
    re.I,
)


def load_key(cli_key: str) -> str:
    if cli_key:
        return cli_key
    env = os.environ.get("TIKHUB_API_KEY")
    if env:
        return env
    cfg = Path(__file__).resolve().parent.parent / "config.json"
    if cfg.exists():
        try:
            v = json.loads(cfg.read_text()).get("tikhub_api_key", "")
            if v:
                return v
        except Exception:
            pass
    print("缺少 TikHub API Key：设置环境变量 TIKHUB_API_KEY，或填入 config.json 的 tikhub_api_key", file=sys.stderr)
    sys.exit(2)


def extract_url(text: str) -> str:
    m = URL_RE.search(text)
    if not m:
        print(f"输入里没找到抖音链接：{text[:80]}", file=sys.stderr)
        sys.exit(1)
    return m.group(0).rstrip(".,。")


def http_json(url: str, key: str) -> dict:
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}", "User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "ignore")[:300]
        print(f"TikHub API HTTP {e.code}: {body}", file=sys.stderr)
        sys.exit(3)
    except Exception as e:
        print(f"TikHub API 请求失败: {e}", file=sys.stderr)
        sys.exit(3)


def parse(link: str, key: str) -> dict:
    """解析一条链接，返回精简后的元数据。"""
    q = urllib.parse.urlencode({"share_url": link})
    resp = http_json(f"{API}?{q}", key)
    if resp.get("code") != 200 or not resp.get("data"):
        print(f"TikHub 返回异常: {json.dumps(resp, ensure_ascii=False)[:300]}", file=sys.stderr)
        sys.exit(3)
    aw = resp["data"]["aweme_detail"]
    video = aw.get("video", {})
    play = (video.get("play_addr") or {}).get("url_list") or []
    return {
        "aweme_id": aw.get("aweme_id"),
        "desc": aw.get("desc", ""),
        "author": (aw.get("author") or {}).get("nickname", ""),
        "create_time": aw.get("create_time"),
        "duration_ms": video.get("duration"),
        "statistics": aw.get("statistics", {}),
        "play_addr": play[0] if play else "",
        "play_addr_265": ((video.get("play_addr_265") or {}).get("url_list") or [""])[0],
    }


def sanitize(name: str, fallback: str) -> str:
    name = re.sub(r'[\\/:*?"<>|#\r\n]+', "", name).strip()
    return (name[:40] or fallback).strip()


def download(url: str, path: Path) -> None:
    # 抖音 CDN 会按 TLS 指纹拦截 Python urllib，必须用 curl 下载
    r = subprocess.run(["curl", "-sSL", "--max-time", "600", "-A", UA, "-o", str(path), url])
    size = path.stat().st_size if path.exists() else 0
    # 小于 100KB 视为 CDN 错误页而非视频（抖音视频均为 MB 级）
    if r.returncode != 0 or size < 100_000:
        if path.exists():
            path.unlink()
        print(f"下载失败（curl 退出码 {r.returncode}，{size} 字节）", file=sys.stderr)
        sys.exit(4)


def main() -> None:
    ap = argparse.ArgumentParser(description="TikHub 抖音解析")
    ap.add_argument("input", help="抖音分享链接，或整段含链接的分享文本")
    ap.add_argument("--download", action="store_true", help="同时下载无水印 mp4")
    ap.add_argument("--output", default=".", help="下载目录（默认当前目录）")
    ap.add_argument("--key", default="", help="TikHub API Key（默认读环境变量/config.json）")
    args = ap.parse_args()

    key = load_key(args.key)
    meta = parse(extract_url(args.input), key)
    print(json.dumps(meta, ensure_ascii=False, indent=2))

    if args.download:
        if not meta["play_addr"]:
            print("无 play_addr，无法下载（可能是图集或已删除）", file=sys.stderr)
            sys.exit(4)
        out = Path(args.output).expanduser()
        out.mkdir(parents=True, exist_ok=True)
        name = sanitize(meta["desc"], meta["aweme_id"])
        path = out / f"{name}.mp4"
        print(f"下载中 → {path}", file=sys.stderr)
        download(meta["play_addr"], path)
        print(f"已保存: {path} ({path.stat().st_size / 1e6:.1f} MB)", file=sys.stderr)


if __name__ == "__main__":
    main()
