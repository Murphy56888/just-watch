# 只看 · just-watch

Murphy 的个人信息流 app「只看」的每日数据仓库。

- `feed/news.enc`：News + 投资（加密）
- `feed/video.enc`：AI视频（小红书剧集，加密）
- `feed/talk.enc`：大佬访谈（加密）
- `covers/`：首批封面图
- `tools/seal.py`：把明文 JSON 加密成 `.enc`（只用公钥 `tools/public.pem`，解密钥匙只在 APK 里）

每天 07:3x / 07:4x 由定时任务更新，app 打开时自动拉取。
