---
title: Video Parser
emoji: 🎬
colorFrom: blue
colorTo: green
sdk: docker
app_port: 7860
pinned: false
---

# 视频解析下载站

一个可自部署的「链接解析 + 视频下载」网站。后端 FastAPI，前端单页 HTML，下载能力由 yt-dlp 和 ffmpeg 提供。

## 结构

```
video-parser/
├── app.py             # 后端：解析 / m3u8 / 代理 / 下载接口
├── static/index.html  # 前端单页
├── requirements.txt
└── cache/             # 临时文件，下载完成后自动删除
```

## 本机运行（3 步）

```bash
cd video-parser
pip3 install -r requirements.txt
uvicorn app:app --host 0.0.0.0 --port 8000
```

打开 http://127.0.0.1:8000 即可。

如果提示找不到 ffmpeg：

- macOS：`brew install ffmpeg`
- Ubuntu/Debian：`sudo apt install ffmpeg`
- Windows：下载 ffmpeg 解压后把 `bin` 加入系统 PATH

## 支持的输入

| 输入 | 后端分支 | 说明 |
|---|---|---|
| `.m3u8` 直链 | `parse_m3u8` | 展开多清晰度、分片数、直播/加密标记 |
| 站点页面链接 | `yt-dlp -J` | YouTube、B站、抖音、Vimeo 等数万站点 |

## 接口一览

- `POST /api/parse` — body `{"url": "...", "referer": "..."}`，返回标题、封面、格式列表
- `GET /api/m3u8?url=&referer=` — 只解析 m3u8
- `GET /api/proxy?url=&referer=` — 流式代理，支持 Range，可解决防盗链和 CORS
- `GET /api/save?url=&format_id=&referer=&filename=` — 服务端下载并合并为 mp4

前端「下载」按钮就是调 `/api/save`，浏览器直接拿到文件流。

## 防盗链与登录

很多站点的 m3u8 会校验 `Referer` / `User-Agent`，甚至绑定 Cookie：

1. 前端「高级选项」填 Referer，例如 `https://example.com/`（注意末尾斜杠）。
2. 需要登录的站点，用浏览器插件导出 `cookies.txt`（Netscape 格式），然后：

```bash
COOKIES_FILE=/path/cookies.txt uvicorn app:app --host 0.0.0.0 --port 8000
```

## 部署到公网服务器

以 Ubuntu 为例，用 systemd 常驻：

```bash
sudo tee /etc/systemd/system/video-parser.service > /dev/null <<'EOF'
[Unit]
Description=Video Parser
After=network.target

[Service]
WorkingDirectory=/opt/video-parser
ExecStart=/usr/bin/python3 -m uvicorn app:app --host 127.0.0.1 --port 8000
Restart=always
Environment=COOKIES_FILE=/opt/video-parser/cookies.txt

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now video-parser
```

Nginx 反代（顺便开 HTTPS 和大文件转发）：

```nginx
server {
    listen 80;
    server_name your-domain.com;

    client_max_body_size 0;
    proxy_read_timeout 900s;
    proxy_send_timeout 900s;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_buffering off;
    }
}
```

要点：

- 必须 `proxy_buffering off`，否则大文件下载会先全部缓存在 Nginx。
- `proxy_read_timeout` 要放大，ffmpeg 合并长视频可能超过默认 60 秒。
- 用 `certbot --nginx` 一键加证书。

## 生产环境必加的三件事

1. **限制并发**：`/api/save` 是同步重活，建议在应用层加信号量（同时最多 2-3 个任务），否则几个人同时下 4K 就把带宽打满。
2. **加访问控制**：给 `/api/save` 加一个简单 token 校验，或整站加 HTTP Basic，避免被当成公共下载站。
3. **定时更新 yt-dlp**：站点规则常变，每周跑一次 `pip3 install -U yt-dlp`。

## 已知限制

- 有 DRM（Widevine / FairPlay）的视频下不了，这是设计上不支持的。
- 直播流（m3u8 没有 `#EXT-X-ENDLIST`）无法"下载完"，只能录制片段。
- 分片 URL 带时效签名的，需重新抓最新 m3u8，旧链接会 403。
- 本机沙箱访问 YouTube 会超时，这是网络限制，不是代码问题；部署到能访问目标站点的服务器即可。

## 合规

只用于下载你有权保存的内容（自己的作品、已授权的素材、公开许可资源）。不要用它批量抓取受版权保护的内容或绕过付费墙。
