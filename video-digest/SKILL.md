---
name: video-digest
description: 提取抖音/B站等平台视频的完整内容(画面+语音),交给多模态模型结构化整理后返回。用户发来视频链接(BV号、抖音分享链接、mp4直链等)并要求总结/提取/整理内容时使用。
---

# video-digest 视频内容提取

调 TikHub 解析视频,再把视频(公网直链或本地文件 base64)交给智谱多模态模型(GLM-5.3-Flash)做画面+语音的完整提取,输出结构化 markdown。

## 配置

密钥在 skill 目录 `.env`(`~/.claude/skills/video-digest/.env`),格式 `KEY=VALUE`,环境变量优先:

- `TIKHUB_API_KEY` — tikhub.io 的 API Key
- `ZHIPU_API_KEY` — 智谱开放平台 API Key
- `ZHIPU_MODEL` — 默认 `glm-5.3-flash`

## 用法

```bash
python3 ~/.claude/skills/video-digest/scripts/video_digest.py "<视频链接或BV号>" [--out /tmp/out.md]
python3 ~/.claude/skills/video-digest/scripts/video_digest.py "<链接>" --parse-only   # 只解析不调智谱
```

抖音整段分享文本可直接传入(脚本自动抠链接)。

- 输出:stdout 打印完整 markdown,同时默认存到 `/tmp/video-digest/<视频ID>.md`
- 提取 prompt 已内置(主题概述/画面内容逐段梳理/口播要点/关键结论/标签);要自定义提取侧重点时,把要求追加为第二个参数即可

## 平台支持与内部链路(排查时看)

| 平台 | 链路 |
|---|---|
| 抖音 | TikHub `/douyin/app/v3/fetch_one_video_by_share_url` 解析 → 无水印直链 → 直链传智谱 `video_url`(失败自动回退:curl 下载 → base64) |
| B站(BV/链接) | TikHub `fetch_one_video` + `fetch_video_playurl(platform=html5)` → curl 带 Referer 下载 mp4 → base64 传智谱 |
| mp4 直链 | 不经 TikHub,直接传智谱;超 100MB 或智谱拉取失败则本机下载转 base64 |

## 已知边界

- 下载一律走 curl:抖音/B站 CDN 按 TLS 指纹拦截 Python urllib(实战坑,见 douyin-video-extract)
- TikHub 带 Cloudflare:请求必须带 UA,否则 403 error 1010
- 小红书暂未接(端点未确认),用户问起先说明
- 智谱 video_url 拉公网直链偶尔失败(直链时效/防盗链),脚本会自动回退 base64,再失败把原始报错贴给用户
- 本地文件超 100MB 会警告(可能超模型限制),仍会尝试
- TikHub 返回结构偶有变动,脚本用深搜 key 的方式取值;取不到时打印原始 JSON 片段辅助排查
- 直链(play_addr)带时效签名,拿到后尽快用,不要长期存储复用

## 给 AI 的约定

1. 拿到结果后由你(会话中的模型)向用户转述/总结,不原样甩长文;用户要求入库时按知识库规范整理
2. 脚本报错时:先看报错里的 HTTP 状态和响应体,密钥问题(401/403)提示用户检查 `.env`;平台解析问题贴出关键 JSON 片段
