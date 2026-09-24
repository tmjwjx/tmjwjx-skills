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


def call_zhipu(cfg, content_parts):
    """content_parts: 完整的多模态 content 数组."""
    body = {
        "model": cfg.get("ZHIPU_MODEL", "glm-5.3-flash"),
        "messages": [{"role": "user", "content": content_parts}],
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


def fmt_ts(sec):
    return f"{int(sec // 60):02d}:{int(sec % 60):02d}"


def extract_frames(video, outdir, max_frames=10):
    """场景切换抽帧(PPT/画面切换类效果好); 场景太少(口播类)按 45s 间隔兜底.

    返回 [(图片路径, 时间秒)], 超过 max_frames 时均匀截取.
    """
    import glob
    pat = os.path.join(outdir, "frame_%03d.jpg")
    r = subprocess.run(["ffmpeg", "-y", "-loglevel", "info", "-i", video,
                        "-vf", "select='gt(scene,0.2)',showinfo", "-vsync", "vfr",
                        "-frames:v", "60", pat], capture_output=True, text=True)
    times = [float(x) for x in re.findall(r"pts_time:([\d.]+)", r.stderr)]
    frames = [(pat % (i + 1), t) for i, t in enumerate(times) if os.path.exists(pat % (i + 1))]
    if len(frames) < 3:
        for f in glob.glob(pat.replace("%03d", "*")):
            os.unlink(f)
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", video,
                        "-vf", "fps=1/45", "-frames:v", "60", pat],
                       capture_output=True, text=True)
        files = sorted(glob.glob(pat.replace("%03d", "*")))
        frames = [(f, i * 45.0) for i, f in enumerate(files)]
    frames = [(p, t) for p, t in frames if os.path.getsize(p) >= 8_000]  # 过滤转场/黑屏小帧
    if len(frames) > max_frames:
        span = frames[-1][1] - frames[0][1]
        gap = max(30.0, span / max_frames)  # 最小间隔, 避免同一屏连拍
        picked, last = [], -1e9
        for p, t in frames:
            if t - last >= gap:
                picked.append((p, t))
                last = t
            if len(picked) >= max_frames:
                break
        frames = picked if len(picked) >= 3 else frames[:max_frames]
    return frames


def caption_frames(cfg, frames, title):
    """批量(5张/次)让模型给每帧写『【时间】标题|要点』, 解析失败时整批原文兜底."""
    caps, leftover = [], []
    for i in range(0, len(frames), 5):
        batch = frames[i:i + 5]
        content = []
        for p, _ in batch:
            b64 = base64.b64encode(open(p, "rb").read()).decode()
            content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}})
        ts = " ".join(f"{j + 1}){fmt_ts(t)}" for j, (_, t) in enumerate(batch))
        content.append({"type": "text", "text":
            f"这是视频《{title}》按时间顺序的截图, 对应时间点: {ts}。"
            "对每张图输出一行, 格式严格为: 【mm:ss】画面标题|画面内容(出现的文字/PPT/图表要点)。不要输出其他内容。"})
        try:
            resp = call_zhipu(cfg, content)
        except SystemExit:
            resp = ""
        lines = [l.strip() for l in resp.splitlines() if l.strip().startswith("【")]
        used = set()
        for p, t in batch:
            line = next((l for l in lines if fmt_ts(t) in l and lines.index(l) not in used), "")
            if line:
                used.add(lines.index(line))
            caps.append((p, t, line))
    return caps


def main():
    argv = sys.argv[1:]
    out_path = argv[argv.index("--out") + 1] if "--out" in argv else None
    max_frames = int(argv[argv.index("--frames") + 1]) if "--frames" in argv else 10
    parse_only = "--parse-only" in argv
    no_frames = "--no-frames" in argv
    args, skip = [], False
    for a in argv:  # 摘掉带值的选项, 剩下: 链接 + 可选的附加要求
        if skip:
            skip = False
            continue
        if a in ("--out", "--frames"):
            skip = True
            continue
        if a in ("--parse-only", "--no-frames"):
            continue
        args.append(a)
    if not args:
        die(1, "用法: video_digest.py <链接/分享文本/BV号> [附加要求] [--frames 10] [--no-frames] [--parse-only] [--out 文件]")
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

    # 全流程要生成配图笔记, 必须有本地视频(抽帧用), 没有就先下载
    if not local_file and info.get("direct"):
        local_file = os.path.join(workdir, "video.mp4")
        print("下载视频(抽帧配图用) ...", file=sys.stderr)
        curl_download(info["direct"], local_file)
    if not local_file:
        die(1, "没有可用的视频源(直链或本地文件)")
    size = os.path.getsize(local_file)
    if size > 100 * 1024 * 1024:
        print(f"警告: {size / 1024 / 1024:.0f}MB 偏大, 可能超模型限制", file=sys.stderr)
    if not cfg.get("ZHIPU_API_KEY"):
        die(2, f"缺少 ZHIPU_API_KEY, 写入 {SKILL_DIR}/.env 或设环境变量")

    # 1) 整视频画面+语音提取(直链优先, 失败回退本地 base64)
    prompt_text = EXTRACT_PROMPT + (f"\n用户额外要求: {extra}" if extra else "")
    model = cfg.get("ZHIPU_MODEL", "glm-5.3-flash")
    if info.get("direct"):
        print("调用智谱提取画面+语音(直链) ...", file=sys.stderr)
        try:
            content = call_zhipu(cfg, [
                {"type": "video_url", "video_url": {"url": info["direct"]}},
                {"type": "text", "text": prompt_text}])
        except SystemExit:
            print("直链方式失败, 回退本地 base64 ...", file=sys.stderr)
            content = call_zhipu(cfg, [as_base64_part(local_file), {"type": "text", "text": prompt_text}])
    else:
        print("调用智谱提取画面+语音(base64) ...", file=sys.stderr)
        content = call_zhipu(cfg, [as_base64_part(local_file), {"type": "text", "text": prompt_text}])

    # 2) 场景抽帧 + 逐帧配说明
    caps = []
    if not no_frames:
        frames_dir = os.path.join(workdir, "frames")
        os.makedirs(frames_dir, exist_ok=True)
        print(f"场景抽帧(上限 {max_frames} 张) ...", file=sys.stderr)
        frames = extract_frames(local_file, frames_dir, max_frames)
        if frames:
            print(f"抽出 {len(frames)} 帧, 生成配图说明 ...", file=sys.stderr)
            caps = caption_frames(cfg, frames, info["title"])

    # 3) 组装带配图的文档笔记
    if not out_path:
        out_path = "/tmp/video-digest/" + re.sub(r"[^\w-]", "_", str(info["id"]))[:60]
    frames_out = os.path.join(os.path.dirname(os.path.abspath(out_path)), "frames")
    os.makedirs(frames_out, exist_ok=True)
    for p, _, _ in caps:
        subprocess.run(["cp", p, os.path.join(frames_out, os.path.basename(p))])

    head = (f"# {info['title']}\n\n> 来源: {url}\n> 作者: {info.get('author', '')}\n"
            f"> 提取: {model} · 配图 {len(caps)} 张\n\n## 内容提取\n\n{content}\n")
    if caps:
        items = []
        for p, t, line in caps:
            cap = line.strip() or f"【{fmt_ts(t)}】画面截图"
            items.append(f"{cap}\n\n![画面 {fmt_ts(t)}](frames/{os.path.basename(p)})\n")
        head += "\n## 画面截图\n\n" + "\n".join(items)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(head + "\n")
    print(head)
    print(f"\n---\n已保存: {out_path} (配图目录: {frames_out})", file=sys.stderr)


if __name__ == "__main__":
    main()
