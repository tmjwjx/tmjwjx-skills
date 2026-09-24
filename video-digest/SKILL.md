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
python3 ~/.claude/skills/video-digest/scripts/video_digest.py "<视频链接或BV号>"
python3 ~/.claude/skills/video-digest/scripts/video_digest.py "<链接>" --frames 15      # 配图上限(默认 10)
python3 ~/.claude/skills/video-digest/scripts/video_digest.py "<链接>" --no-frames      # 不要配图
python3 ~/.claude/skills/video-digest/scripts/video_digest.py "<链接>" --parse-only     # 只解析不调智谱
python3 ~/.claude/skills/video-digest/scripts/video_digest.py "<链接>" "额外提取要求" --out /tmp/note.md
```

抖音整段分享文本可直接传入(脚本自动抠链接)。

## 产物(带配图的文档笔记)

- `<输出>/note.md`:内容提取(主题/画面/口播/结论/标签)+ 画面截图区(每张带时间戳与说明)
- `<输出>/frames/`:场景切换抽帧(过滤黑屏小帧、按最小间隔去连拍)
- 图片用相对路径 `frames/xxx.jpg` 引用;入库知识库时把 frames 语义化改名挪进 `Attachments/` 并修链接

- 输出:stdout 打印完整 markdown,同时默认存到 `/tmp/video-digest/<视频ID>.md`
- 提取 prompt 已内置(主题概述/画面内容逐段梳理/口播要点/关键结论/标签);要自定义提取侧重点时,把要求追加为第二个参数即可

## 平台支持与内部链路(排查时看)

| 平台 | 链路 |
|---|---|
| 抖音 | TikHub `/douyin/app/v3/fetch_one_video_by_share_url` 解析 → 无水印直链 → 直链传智谱 `video_url`(失败自动回退:curl 下载 → base64) |
| B站(BV/链接) | TikHub `fetch_one_video` + `fetch_video_playurl` 拿 DASH 双流 → curl 带 Referer 下载 → ffmpeg 合成 → base64 传智谱 |
| mp4 直链 | 不经 TikHub,直接传智谱;超 100MB 或智谱拉取失败则本机下载转 base64 |

**长视频(>5 分钟)自动分块**:实测整片 base64 不可行(46min≈336MB),自动按 5 分钟/段切片(480p/900k,硬件编码器)→ 逐段提取 → 合成最终笔记。实测 5min/29MB(base64 39MB)可通过,处理约 40s/段。

**模型**:必须用视觉模型(`glm-5.3-flash` 默认);旗舰 `glm-5.3` 不支持视频输入(只收 text),实测确认。

## 已知边界

- 下载一律走 curl:抖音/B站 CDN 按 TLS 指纹拦截 Python urllib(实战坑,见 douyin-video-extract)
- TikHub 带 Cloudflare:请求必须带 UA,否则 403 error 1010
- 小红书暂未接(端点未确认),用户问起先说明
- 智谱 video_url 拉公网直链偶尔失败(直链时效/防盗链),脚本会自动回退 base64,再失败把原始报错贴给用户
- 容器敏感:m4s 直接拷贝的流智谱报 1210「视频输入格式/解析错误」,脚本遇到 1210 自动重编码(480p 硬件加速)重试
- 本地文件超 100MB 会警告(可能超模型限制),仍会尝试
- TikHub 返回结构偶有变动,脚本用深搜 key 的方式取值;取不到时打印原始 JSON 片段辅助排查
- 直链(play_addr)带时效签名,拿到后尽快用,不要长期存储复用
- 长视频(30 分钟+)本地文件较大,base64 传智谱可能超限——超 100MB 会警告;不行就让用户换短视频或等 OSS 直链方案

## 给 AI 的约定

1. **异步流程**:用户发来视频链接,先秒回「收到,提取中」,立刻用 Bash 后台任务(run_in_background)跑脚本,日志重定向到文件(别接管道,进程会话中断会丢输出);任务完成通知到达后再主动汇报:总结要点 + 笔记/配图路径,不等用户来问
2. 长视频跑一次十几分钟属正常,期间可正常对答其他事
3. **笔记形态(2026-09-24 用户纠偏,重要)**:脚本的分段提取(时间戳画面/口播流水账)只是**原始素材**,绝不能直接当笔记交付。必须由你重新创作成**学习者视角的知识笔记**——按「概念/原理 → 完整实操步骤(代码块用视频里最终正确版)→ 踩坑与修正 → 总结」组织,像真人看完视频写的笔记;配图只挑有信息量的(代码/文档/关键界面),插在对应知识点旁,时间戳流水账和过渡帧全部丢弃
4. **交付方式**:知识笔记默认放进用户工作区的收件箱(知识库为 `00 Inbox/`,命名 `MM-DD 标题.md`),配图语义化命名挪 `Attachments/` 并把链接改为库内路径;**不主动发文件给用户**,用户要求才发。口头汇报时只给要点总结 + 文件位置
4. 脚本报错时:先看日志里的 HTTP 状态和响应体,密钥问题(401/403)提示用户检查 `.env`;平台解析问题贴出关键 JSON 片段
