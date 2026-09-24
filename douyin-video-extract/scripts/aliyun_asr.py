#!/usr/bin/env python3
"""阿里云百炼语音转写：公网可访问的音视频 URL → 文字稿（paraformer-v2，异步任务）。

用法:
  python3 aliyun_asr.py --url "https://..."                 # 单条：提交并等待，输出纯文本
  python3 aliyun_asr.py --url "https://..." --timestamps    # 单条：输出带句级时间戳的原始 JSON
  python3 aliyun_asr.py --urls URL1 URL2 [...]              # 批量：一个任务提交多条（≤10），输出 JSON
  python3 aliyun_asr.py --urls URL1 [...] --no-wait         # 批量：只提交，打印 task_id 后退出
  python3 aliyun_asr.py --task-id <id> [--json]             # 查询已有任务（配合 --no-wait / 超时后恢复）

输出:
  单条默认纯文本；批量或加 --json 时输出数组:
  [{"file_url": "...", "status": "SUCCEEDED", "text": "..."}, ...]
  --timestamps 时每项额外带 "detail"（原始转写 JSON，含句级时间戳）

Key 读取优先级: 环境变量 DASHSCOPE_API_KEY > config.json > 命令行 --key
前置条件: 在百炼控制台开通模型服务并创建 API-KEY（https://bailian.console.aliyun.com）
退出码: 0 成功 | 1 参数错误 | 2 缺 Key | 3 提交/查询失败 | 5 等待超时（可稍后 --task-id 恢复）
计费: ¥0.00008/秒，每月免费 36,000 秒（10 小时）。
"""
import argparse
import json
import os
import sys
import time
import urllib.request

SUBMIT_URL = "https://dashscope.aliyuncs.com/api/v1/services/audio/asr/transcription"
TASK_URL = "https://dashscope.aliyuncs.com/api/v1/tasks/{task_id}"  # DashScope 通用异步任务查询
MAX_URLS = 10  # 单个任务最多 file_urls 数（API 限制）


def load_key(cli_key: str) -> str:
    if cli_key:
        return cli_key
    env = os.environ.get("DASHSCOPE_API_KEY")
    if env:
        return env
    cfg = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.json")
    if os.path.exists(cfg):
        try:
            with open(cfg) as f:
                v = json.load(f).get("dashscope_api_key", "")
                if v:
                    return v
        except Exception:
            pass
    print(
        "缺少阿里云百炼 API Key：\n"
        "  1. 到 https://bailian.console.aliyun.com 开通模型服务（免费）\n"
        "  2. 控制台右上角创建 API-KEY\n"
        "  3. 设置环境变量 DASHSCOPE_API_KEY 或填入 config.json 的 dashscope_api_key",
        file=sys.stderr,
    )
    sys.exit(2)


def load_model(cli_model: str) -> str:
    if cli_model:
        return cli_model
    cfg = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.json")
    if os.path.exists(cfg):
        try:
            with open(cfg) as f:
                return json.load(f).get("dashscope_model") or "paraformer-v2"
        except Exception:
            pass
    return "paraformer-v2"


def _request(req: urllib.request.Request) -> dict:
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        print(f"百炼 API HTTP {e.code}: {e.read().decode('utf-8', 'ignore')[:300]}", file=sys.stderr)
        sys.exit(3)
    except Exception as e:
        print(f"百炼 API 请求失败: {e}", file=sys.stderr)
        sys.exit(3)


def get(url: str, key: str) -> dict:
    return _request(urllib.request.Request(url, headers={"Authorization": f"Bearer {key}"}))


def submit(urls: list, key: str, model: str) -> str:
    """提交一个转写任务（可含最多 MAX_URLS 个文件），返回 task_id。"""
    if len(urls) > MAX_URLS:
        print(f"单任务最多 {MAX_URLS} 个 URL，收到 {len(urls)} 个", file=sys.stderr)
        sys.exit(1)
    req = urllib.request.Request(
        SUBMIT_URL,
        data=json.dumps({
            "model": model,
            "input": {"file_urls": urls},
            "parameters": {"disfluency_removal_enabled": True},
        }).encode(),
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "X-DashScope-Async": "enable",  # 不带此头会报 "does not support synchronous calls"
        },
        method="POST",
    )
    resp = _request(req)
    task_id = (resp.get("output") or {}).get("task_id")
    if not task_id:
        print(f"提交失败: {json.dumps(resp, ensure_ascii=False)[:300]}", file=sys.stderr)
        sys.exit(3)
    return task_id


def wait_result(task_id: str, key: str, timeout: int, interval: int) -> dict:
    """轮询任务直到 SUCCEEDED/FAILED。"""
    deadline = time.time() + timeout
    url = TASK_URL.format(task_id=task_id)
    last = ""
    while time.time() < deadline:
        out = get(url, key).get("output") or {}
        status = out.get("task_status", "?")
        if status != last:
            print(f"[asr] 任务 {task_id} 状态: {status}", file=sys.stderr)
            last = status
        if status == "SUCCEEDED":
            return out
        if status in ("FAILED", "CANCELED", "UNKNOWN"):
            print(f"任务失败: {json.dumps(out, ensure_ascii=False)[:300]}", file=sys.stderr)
            sys.exit(3)
        time.sleep(interval)
    print(f"等待超时（{timeout}s）。任务仍在排队，稍后可恢复: "
          f"python3 aliyun_asr.py --task-id {task_id} --json", file=sys.stderr)
    sys.exit(5)


def parse_transcript(data: dict) -> str:
    """转写结果 JSON → 纯文本。结构变化时原样吐 JSON，不丢数据。"""
    transcripts = data.get("transcripts") or []
    if not transcripts:
        return json.dumps(data, ensure_ascii=False)
    parts = []
    for t in transcripts:
        if t.get("text"):
            parts.append(t["text"])
        else:  # 兜底：逐句拼
            parts.append("".join(s.get("text", "") for s in t.get("sentences") or []))
    return "\n".join(parts)


def emit(out: dict, key: str, timestamps: bool, json_mode: bool) -> None:
    """把任务结果整理输出：每项 {file_url, status, text[, detail]}。"""
    entries = []
    for sub in out.get("results") or []:
        status = sub.get("subtask_status")
        entry = {"file_url": sub.get("file_url", ""), "status": status, "text": ""}
        t_url = sub.get("transcription_url")
        if status == "SUCCEEDED" and t_url:
            data = get(t_url, key)
            if timestamps:
                entry["detail"] = data
            entry["text"] = parse_transcript(data)
        entries.append(entry)

    if json_mode or len(entries) != 1:
        print(json.dumps(entries, ensure_ascii=False, indent=2))
    elif timestamps:
        print(json.dumps(entries[0].get("detail", entries[0]), ensure_ascii=False, indent=2))
    else:
        print(entries[0]["text"] or json.dumps(entries[0], ensure_ascii=False))


def main() -> None:
    ap = argparse.ArgumentParser(description="阿里云百炼语音转文字")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--url", help="单个公网可访问的音视频 URL")
    g.add_argument("--urls", nargs="+", metavar="URL", help=f"批量 URL（≤{MAX_URLS}，一个任务提交）")
    g.add_argument("--task-id", dest="task_id", help="查询已提交任务的 task_id")
    ap.add_argument("--key", default="")
    ap.add_argument("--model", default="", help="默认读 config.json 的 dashscope_model（paraformer-v2）")
    ap.add_argument("--timestamps", action="store_true", help="附带句级时间戳原始 JSON")
    ap.add_argument("--json", action="store_true", help="强制 JSON 输出（批量模式下默认 JSON）")
    ap.add_argument("--no-wait", action="store_true", help="只提交任务打印 task_id（不能与 --task-id 同用）")
    ap.add_argument("--timeout", type=int, default=600, help="等待上限秒数（默认 600）")
    ap.add_argument("--interval", type=int, default=5, help="轮询间隔秒数（默认 5）")
    args = ap.parse_args()

    if args.no_wait and args.task_id:
        ap.error("--no-wait 不能与 --task-id 同用")

    key = load_key(args.key)

    if args.task_id:
        out = wait_result(args.task_id, key, args.timeout, args.interval)
        emit(out, key, args.timestamps, args.json)
        return

    urls = [args.url] if args.url else args.urls
    task_id = submit(urls, key, load_model(args.model))
    if args.no_wait:
        print(task_id)
        return
    out = wait_result(task_id, key, args.timeout, args.interval)
    emit(out, key, args.timestamps, args.json)


if __name__ == "__main__":
    main()
