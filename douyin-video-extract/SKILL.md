---
name: douyin-video-extract
description: Use when 用户提供抖音视频链接或分享口令，需要下载视频、提取文案（标题/描述/数据）或语音转文字（口播转写）时。Triggers: 抖音/douyin 链接、v.douyin.com、视频下载、无水印、视频转文字、提取文案、字幕、口播转写、TikHub、阿里云百炼 ASR、paraformer。
---

# douyin-video-extract

抖音链接 → 视频文件 + 文字稿。TikHub 负责解析（拿直链和元数据），阿里云百炼负责语音转文字。

## 快速开始

```bash
# 全流程：解析 + 下载视频 + 语音转文字稿（推荐入口）
python3 scripts/extract.py "<抖音分享链接或整段分享文本>" --output ~/douyin_out

# 批量：文件每行一条链接（可含分享文本）
python3 scripts/extract.py --list links.txt --output ~/douyin_out

# 只解析元数据（含无水印直链），不下载不转写
python3 scripts/tikhub_parse.py "<链接>"

# 手动转写一个公网可访问的音视频 URL
python3 scripts/aliyun_asr.py --url "https://.../xxx.mp4"

# 批量转写（≤10 条一个任务）：先提交拿 task_id，再查结果（等待超时后用这个恢复）
python3 scripts/aliyun_asr.py --urls URL1 URL2 ... --no-wait
python3 scripts/aliyun_asr.py --task-id <task_id> --json
```

extract.py 批量时是**两阶段**：先把全部链接解析+下载完，再按 10 条/任务批量提交转写、统一轮询——转写不会阻塞后续解析。

脚本均零第三方依赖（Python 3.9+，下载用系统 curl）。参数详情跑 `--help`。

## 费用

| 环节 | 单价 |
|---|---|
| TikHub 解析 | ~$0.001/次（按调用次数，与时长无关；下载本身免费） |
| 百炼 paraformer-v2 转写 | ¥0.00008/秒 ≈ ¥0.29/小时，每月免费 10 小时 |

一条 1 分钟视频全流程 ≈ ¥0.012。**重复解析同一条会重复计费**——用 extract.py（自带 URL→aweme_id 索引去重），不要循环裸调 tikhub_parse.py。

## 配置（config.json）

> 本地使用：`cp config.example.json config.json` 后填入自己的 key（config.json 已 gitignore）。

- `tikhub_api_key`：TikHub.io 的 Key（真实 key 放本地 config.json，不入库；若失效去 user.tikhub.io 重置后更新）
- `dashscope_api_key`：阿里云百炼 Key。**未填时转写自动跳过**。开通：https://bailian.console.aliyun.com 开通模型服务（免费）→ 创建 API-KEY
- 环境变量 `TIKHUB_API_KEY` / `DASHSCOPE_API_KEY` 优先于 config.json

## 每条视频的产物

输出目录 `<output>/<aweme_id>_<标题>/`：`video.mp4`（无水印）、`transcript.txt`（文字稿）、`meta.json`（文案/作者/互动数据/直链）。

## 常见问题

| 现象 | 处理 |
|---|---|
| TikHub 返回 HTTP 403 error 1010 | 请求缺 User-Agent 被其 Cloudflare 拦截；脚本已内置 UA，勿删 |
| 下载报 SSL EOF / protocol violation | 抖音 CDN 按 TLS 指纹拦截 Python urllib，**必须用 curl 下载**；脚本已内置 |
| 报 "does not support synchronous calls" | 百炼录音文件识别只支持异步：提交必须带 `X-DashScope-Async: enable` 头（脚本已内置） |
| 报 "Request method 'GET' is not supported" | 轮询要用通用任务接口 `GET /api/v1/tasks/{task_id}`，不能拿提交接口查（脚本已内置） |
| 转写 FAILED 且提示拉取文件失败 | 直链过期（隔天重跑旧任务最常见）。**重跑 extract.py 即可**——缓存 meta 超 2 小时会自动重新解析拿新直链。手动兜底：自建 OSS 传 video.mp4 后跑 `aliyun_asr.py --url <oss链接>`（skill 不含 OSS 配置，需自备） |
| 转写长时间 PENDING / 等待超时 | 正常，任务不会丢。恢复：`aliyun_asr.py --task-id <id> --json`（extract.py 超时时会打印 task_id） |
| statistics.play_count 为 0 | 抖音未公开播放量，正常现象 |
| 输入无链接报 exit 1 | 检查文本里是否真的含 douyin 域名链接（短链/长链/分享口令都支持） |

退出码：0 成功 | 1 输入无效 | 2 缺 Key | 3 API 失败/部分失败 | 4 下载失败 | 5 转写等待超时。

## 注意

- 实测参考：77 秒视频端到端约 40 秒（解析 ~2s、下载 ~10s、转写排队+处理 ~25s），远快于官方"数分钟"的保守说法
- `--force` 忽略去重全部重跑，**解析会再次计费**；产物齐全时不要加 force
- 转写失败/超时的条目，**重跑同一命令会自动只补转写**（用缓存 meta.json，不重新解析、不再收解析费）；只有视频文件本身缺失才走完整重跑
- 单条下载文件 <100KB 判为失败（防 CDN 错误页误报成功）
- 直链（play_addr）带时效签名，拿到后尽快下载，不要长期存储复用
- 图集类作品无 play_addr，只出 meta
- 两条链路本质都是公开数据爬取，商用自担合规风险；控制频率（extract.py 已内置 1s 间隔）
