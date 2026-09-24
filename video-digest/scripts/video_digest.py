#!/usr/bin/env python3
"""video-digest: 解析视频(抖音/B站/mp4直链), 交给智谱多模态模型提取画面+语音内容.

用法:
  video_digest.py "<链接/分享文本/BV号>"              # 全流程: 解析→(下载)→智谱提取
  video_digest.py "<链接>" --parse-only               # 只解析(抖音出直链, B站连视频一起下载), 不调智谱
  video_digest.py "<链接>" "额外提取要求"              # 追加自定义提取侧重点

Key: .env 或环境变量 TIKHUB_API_KEY / ZHIPU_API_KEY / ZIPHU_MODEL(默认 glm-5.3-flash)
退出码: 0 成功 | 1 输入无效 | 2 缺 Key | 3 TikHub 失败 | 4 下载失败 | 5 智谱失败
"""
import base64
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
TIKHUB_DOUYIN = "https://api.tikhub.io/api/v1/douyin/app/v3/fetch_one_video_by_share_url"
TIKHUB_BILI_META = "https://api.tikhub.io/api/v1/bilibili/web/fetch_one_video"
TIKHUB_BILI_PLAY = "https://api.tikhub.io/api/v1/bilibili/web/fetch_video_playurl"
EXTRACT_PROMPT = (
    "你是视频内容提取助手。请完整观看该视频,输出 markdown(中文,忠实于视频内容,不要编造):\n"
    "## 主题概述\n一句话说明这个视频讲什么\n"
    "## 画面内容\n逐段梳理画面信息:出现的文字/PPT/图表/代码/演示步骤\n"
    "## 口播要点\n按时间顺序完整覆盖讲解内容,不要只挑重点\n"
    "## 关键结论\n可直接抄走的内容:结论/代码/命令/清单/书名\n"
    "## 标签\n3-6 个适合检索的标签"
)
URL_RE = re.compile(
    r"https?://(?:v\.douyin\.com|www\.douyin\.com|www\.iesdouyin\.com"
    r"|www\.bilibili\.com|b23\.tv|www\.xiaohongshu\.com|xhslink\.com)/[^\s，,；;）)】\"']+",
    re.I,
)


def die(code, msg):
    print(msg, file=sys.stderr)
    sys.exit(code)


def load_env():
    env = {}
    path = os.path.join(SKILL_DIR, ".env")
    if os.path.exists(path):
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    return {**env, **{k: v for k, v in os.environ.items() if k in env or k in ("TIKHUB_API_KEY", "ZHIPU_API_KEY", "ZHIPU_MODEL")}}


def find_key(obj, key):
    """深搜 json 第一个非空匹配值; TikHub 响应层级不稳, 统一用它取值."""
    if isinstance(obj, dict):
        if key in obj and obj[key] not in (None, "", [], {}):
            return obj[key]
        for v in obj.values():
            r = find_key(v, key)
            if r is not None:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = find_key(v, key)
            if r is not None:
                return r
    return None


def http_json(url, api_key):
    from urllib.parse import urlencode
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,  # TikHub 有 Cloudflare, 缺 UA 会被 403 error 1010
        "Authorization": f"Bearer {api_key}",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        die(3, f"TikHub HTTP {e.code}: {e.read().decode('utf-8', 'ignore')[:300]}")
    except Exception as e:
        die(3, f"TikHub 请求失败: {e}")


BILI_HOST_FALLBACKS = [
    "upos-hz-mirrorks3.bilivideo.com",
    "upos-sz-mirrorks3.bilivideo.com",
    "upos-sz-mirrorcos.bilivideo.com",
]


def curl_download(urls, path, referer=None):
    """抖音/B站 CDN 按 TLS 指纹拦截 Python urllib, 必须用 curl.

    urls 可以是单个 url 或候选列表(TikHub 常返回海外镜像域名, 国内拉不动时换国内 CDN 域名重试).
    """
    if isinstance(urls, str):
        urls = [urls]
    m = re.match(r"(https?://)([^/]+)(/.*)", urls[0]) if urls else None
    if m and ("bilivideo.com" in urls[0] or "akamaized.net" in urls[0]):
        for h in BILI_HOST_FALLBACKS:
            cand = m.group(1) + h + m.group(3)
            if cand not in urls:
                urls.append(cand)
    last = None
    for u in urls:
        cmd = ["curl", "-sSL", "--max-time", "600", "-A", UA]
        if referer:
            cmd += ["-e", referer]
        cmd += ["-o", path, u]
        r = subprocess.run(cmd)
        size = os.path.getsize(path) if os.path.exists(path) else 0
        if r.returncode == 0 and size >= 100_000:  # <100KB 视为 CDN 错误页
            return
        last = (r.returncode, size, u)
    if os.path.exists(path):
        os.unlink(path)
    die(4, f"下载失败 (最后尝试 curl 退出码 {last[0]}, {last[1]} 字节): {last[2][:120]}")


def resolve_douyin(url, key):
    from urllib.parse import urlencode
    m = URL_RE.search(url)
    if not m:
        die(1, f"输入里没找到抖音链接: {url[:80]}")
    resp = http_json(f"{TIKHUB_DOUYIN}?{urlencode({'share_url': m.group(0).rstrip('.,。')})}", key)
    if resp.get("code") != 200 or not resp.get("data"):
        die(3, f"TikHub 返回异常: {json.dumps(resp, ensure_ascii=False)[:300]}")
    aw = resp["data"]["aweme_detail"]
    video = aw.get("video", {})
    play = (video.get("play_addr") or {}).get("url_list") or []
    play265 = (video.get("play_addr_265") or {}).get("url_list") or []
    return {
        "id": aw.get("aweme_id") or "douyin",
        "title": aw.get("desc") or "douyin_video",
        "author": (aw.get("author") or {}).get("nickname", ""),
        "direct": play[0] if play else (play265[0] if play265 else ""),
    }


def resolve_bilibili(url, key, workdir, download):
    if "b23.tv" in url:  # 短链先跟重定向拿真实地址
        r = subprocess.run(["curl", "-sIL", "-o", "/dev/null", "-w", "%{url_effective}",
                            "--max-time", "30", "-A", UA, url], capture_output=True, text=True)
        if r.returncode == 0 and r.stdout.strip():
            url = r.stdout.strip()
    m = re.search(r"(BV[0-9A-Za-z]{10})", url)
    if not m:
        die(1, f"链接里没找到 BV 号: {url}")
    bv = m.group(1)
    meta = http_json(f"{TIKHUB_BILI_META}?bv_id={bv}", key)
    cid, title = find_key(meta, "cid"), find_key(meta, "title") or bv
    if not cid:
        die(3, f"B站元数据没拿到 cid: {json.dumps(meta, ensure_ascii=False)[:400]}")
    play = http_json(f"{TIKHUB_BILI_PLAY}?bv_id={bv}&cid={cid}&qn=64&platform=html5", key)
    pd = play.get("data", {})
    pd = pd.get("data", pd)  # TikHub 包一层, 里面是 B站 playurl 原始结构
    info = {"id": bv, "title": str(title), "author": find_key(meta, "name") or "", "direct": ""}
    if not download:
        dash = pd.get("dash") or {}
        v = (dash.get("video") or [{}])[0]
        info["direct"] = v.get("baseUrl") or v.get("base_url") or ""
        return info
    out = os.path.join(workdir, f"{bv}.mp4")
    dash, durl = pd.get("dash"), pd.get("durl")
    if isinstance(dash, dict) and dash.get("video"):
        vj = dash["video"][0]
        v_url = vj.get("baseUrl") or vj.get("base_url")
        aj = (dash.get("audio") or [{}])[0]
        a_url = aj.get("baseUrl") or aj.get("base_url")
        if not v_url:
            die(3, f"dash 里没拿到视频流: {json.dumps(dash, ensure_ascii=False)[:300]}")
        print(f"下载 B站视频流 {bv} (画质 id={vj.get('id')}) ...", file=sys.stderr)
        vp, ap = os.path.join(workdir, "v.m4s"), os.path.join(workdir, "a.m4s")
        curl_download(v_url, vp, referer="https://www.bilibili.com/")
        cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", vp]
        if a_url:
            curl_download(a_url, ap, referer="https://www.bilibili.com/")
            cmd += ["-i", ap]
        cmd += ["-c", "copy", out]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0 and a_url:  # 音频拷贝失败时重编码音频
            r = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", vp, "-i", ap,
                                "-c:v", "copy", "-c:a", "aac", out], capture_output=True, text=True)
        if r.returncode != 0:
            die(4, f"ffmpeg 合并失败: {r.stderr[-300:]}")
    elif isinstance(durl, list) and durl:  # 老接口直接给合并 mp4
        mp4 = durl[0].get("url")
        if not mp4:
            die(3, "durl 里没拿到 url")
        print(f"下载 B站视频 {bv} ...", file=sys.stderr)
        curl_download(mp4, out, referer="https://www.bilibili.com/")
    else:
        die(3, f"playurl 里既无 dash 也无 durl: {json.dumps(pd, ensure_ascii=False)[:300]}")
    info["file"] = out
    return info


def call_zhipu(cfg, video_part, prompt):
    body = {
        "model": cfg.get("ZHIPU_MODEL", "glm-5.3-flash"),
        "messages": [{"role": "user", "content": [video_part, {"type": "text", "text": prompt}]}],
    }
    req = urllib.request.Request(
        "https://open.bigmodel.cn/api/paas/v4/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {cfg['ZHIPU_API_KEY']}"})
    try:
        with urllib.request.urlopen(req, timeout=600) as r:
            resp = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        die(5, f"智谱 API HTTP {e.code}: {e.read().decode()[:800]}")
    except Exception as e:
        die(5, f"智谱 API 请求失败: {e}")
    try:
        return resp["choices"][0]["message"]["content"]
    except (KeyError, IndexError):
        die(5, f"智谱响应结构异常: {json.dumps(resp, ensure_ascii=False)[:600]}")


def as_base64_part(path):
    b64 = base64.b64encode(open(path, "rb").read()).decode()
    return {"type": "video_url", "video_url": {"url": f"data:video/mp4;base64,{b64}"}}


def main():
    args = [a for a in sys.argv[1:] if a != "--out"]
    out_path = sys.argv[sys.argv.index("--out") + 1] if "--out" in sys.argv else None
    parse_only = "--parse-only" in args
    args = [a for a in args if a != "--parse-only"]
    if not args:
        die(1, "用法: video_digest.py <链接/分享文本/BV号> [附加要求] [--parse-only] [--out 文件]")
    url, extra = args[0], " ".join(args[1:])
    cfg = load_env()
    if not cfg.get("TIKHUB_API_KEY"):
        die(2, f"缺少 TIKHUB_API_KEY, 写入 {SKILL_DIR}/.env 或设环境变量")
    workdir = tempfile.mkdtemp(prefix="video-digest-")

    local_file, info = None, {}
    low = url.lower()
    if "douyin.com" in low or "iesdouyin" in low:
        info = resolve_douyin(url, cfg["TIKHUB_API_KEY"])
    elif "bilibili.com" in low or "b23.tv" in low or re.match(r"^BV[0-9A-Za-z]{10}$", url.strip()):
        target = url if url.startswith("http") else f"https://www.bilibili.com/video/{url.strip()}"
        info = resolve_bilibili(target, cfg["TIKHUB_API_KEY"], workdir, download=True)
        local_file = info.get("file")
    elif re.search(r"\.(mp4|mov|m4v)(\?|$)", low):
        info = {"id": os.path.basename(url.split("?")[0]), "title": os.path.basename(url.split("?")[0]), "direct": url}
    else:
        die(1, f"暂不支持该链接(支持: 抖音 / B站 / mp4直链): {url[:80]}")

    print(f"标题: {info['title']}\n作者: {info.get('author', '')}\nID: {info['id']}", file=sys.stderr)

    if parse_only:
        print(json.dumps({k: v for k, v in info.items() if k != "file"}, ensure_ascii=False, indent=2))
        if local_file:
            print(f"已下载: {local_file} ({os.path.getsize(local_file) / 1e6:.1f} MB)", file=sys.stderr)
        return
    if info.get("direct") and not local_file and "xiaohongshu" not in low:
        print("直链交给智谱拉取(失败自动回退本机下载)...", file=sys.stderr)
        video_part = {"type": "video_url", "video_url": {"url": info["direct"]}}
    elif local_file:
        size = os.path.getsize(local_file)
        if size > 100 * 1024 * 1024:
            print(f"警告: {size / 1024 / 1024:.0f}MB 偏大, 可能超模型限制", file=sys.stderr)
        print("本地视频 base64 编码后调用智谱 ...", file=sys.stderr)
        video_part = as_base64_part(local_file)
    else:
        die(1, "没有可用的视频源(直链或本地文件)")

    if not cfg.get("ZHIPU_API_KEY"):
        die(2, f"缺少 ZHIPU_API_KEY, 写入 {SKILL_DIR}/.env 或设环境变量")
    prompt = EXTRACT_PROMPT + (f"\n用户额外要求: {extra}" if extra else "")
    try:
        content = call_zhipu(cfg, video_part, prompt)
    except SystemExit:
        if local_file:
            raise
        print("直链方式失败, 回退: curl 下载后 base64 ...", file=sys.stderr)
        dl = os.path.join(workdir, "fallback.mp4")
        curl_download(info["direct"], dl)
        content = call_zhipu(cfg, as_base64_part(dl), prompt)

    result = (f"# {info['title']}\n\n> 来源: {url}\n> 作者: {info.get('author', '')}\n"
              f"> 提取模型: {cfg.get('ZHIPU_MODEL', 'glm-5.3-flash')}\n\n{content}\n")
    if not out_path:
        os.makedirs("/tmp/video-digest", exist_ok=True)
        out_path = "/tmp/video-digest/" + re.sub(r"[^\w-]", "_", str(info["id"]))[:60] + ".md"
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(result)
    print(result)
    print(f"\n---\n已保存: {out_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
