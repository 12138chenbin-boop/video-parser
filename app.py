"""
视频解析 / 下载站 —— 最小可用后端
================================================
能力：
  POST /api/parse        解析任意 yt-dlp 支持的链接，返回标题/封面/格式列表
  GET  /api/m3u8         解析 m3u8（多清晰度、分片数、是否加密/直播）
  GET  /api/proxy        流式代理媒体（解决防盗链 / CORS / Range 拖动）
  GET  /api/save         后端下载并合并成 mp4，返回文件
  GET  /                 前端单页

依赖：fastapi uvicorn httpx yt-dlp ffmpeg
启动：uvicorn app:app --host 0.0.0.0 --port 8000
"""

import asyncio
import ipaddress
import os
import re
import shutil
import subprocess
import tempfile
import uuid
from typing import Optional
from urllib.parse import urljoin, urlparse

import httpx
from fastapi import FastAPI, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(BASE_DIR, "cache")
os.makedirs(CACHE_DIR, exist_ok=True)

# 可选：浏览器导出的 cookies.txt，供需要登录的站点使用
COOKIES_FILE = os.environ.get("COOKIES_FILE", "")


def ytdlp_base() -> list:
    cmd = ["yt-dlp", "--no-update", "--no-progress"]
    if COOKIES_FILE and os.path.exists(COOKIES_FILE):
        cmd += ["--cookies", COOKIES_FILE]
    return cmd

app = FastAPI(title="Video Parser")


# ----------------------------------------------------------------------------- 工具
def check_url(url: str) -> str:
    """只允许 http/https，且禁止访问内网地址（防 SSRF）。"""
    url = (url or "").strip()
    if not url:
        raise ValueError("链接为空")
    p = urlparse(url)
    if p.scheme not in ("http", "https"):
        raise ValueError("只支持 http/https 链接")
    host = (p.hostname or "").lower()
    if not host:
        raise ValueError("链接格式不正确")
    if host in ("localhost", "metadata.google.internal"):
        raise ValueError("不允许访问内网地址")
    try:
        ip = ipaddress.ip_address(host)
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            raise ValueError("不允许访问内网地址")
    except ValueError as e:
        if "不允许" in str(e):
            raise
    return url


def headers_for(referer: Optional[str], extra: Optional[dict] = None) -> dict:
    h = {
        "User-Agent": UA,
        "Accept": "*/*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Referer": referer or "https://www.google.com/",
    }
    if extra:
        h.update({k: v for k, v in extra.items() if v})
    return h


def human_size(n) -> str:
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "-"
    if n <= 0:
        return "-"
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f}{u}"
        n /= 1024
    return f"{n:.1f}PB"


def fmt_duration(s) -> str:
    try:
        s = int(float(s))
    except (TypeError, ValueError):
        return "-"
    h, r = divmod(s, 3600)
    m, sec = divmod(r, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m:02d}:{sec:02d}"


async def run(cmd: list, timeout: int = 120) -> tuple[int, bytes, bytes]:
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise TimeoutError("处理超时")
    return proc.returncode, out, err


# ----------------------------------------------------------------------------- 解析
class ParseReq(BaseModel):
    url: str
    referer: Optional[str] = None


@app.post("/api/parse")
async def api_parse(req: ParseReq):
    try:
        url = check_url(req.url)
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)

    # 直链 m3u8 走专用解析
    if ".m3u8" in url.lower():
        return await parse_m3u8(url, req.referer)

    cmd = ytdlp_base() + [
        "-J", "--no-playlist", "--no-warnings",
        "--socket-timeout", "15",
        "--user-agent", UA,
        "--referer", req.referer or url,
        url,
    ]
    try:
        code, out, err = await run(cmd, timeout=90)
    except TimeoutError:
        return JSONResponse({"ok": False, "error": "解析超时，站点可能限速或需要登录"}, status_code=504)
    if code != 0:
        msg = err.decode("utf-8", "ignore")[-800:]
        return JSONResponse({"ok": False, "error": f"解析失败：{msg}"}, status_code=502)

    try:
        info = __import__("json").loads(out)
    except Exception:
        return JSONResponse({"ok": False, "error": "返回数据解析失败"}, status_code=502)

    formats = []
    for f in info.get("formats") or []:
        if f.get("vcodec") == "none" and f.get("acodec") == "none":
            continue
        formats.append({
            "format_id": f.get("format_id"),
            "ext": f.get("ext"),
            "resolution": f.get("resolution") or (f"{f.get('width') or ''}x{f.get('height') or ''}"),
            "height": f.get("height") or 0,
            "vcodec": f.get("vcodec"),
            "acodec": f.get("acodec"),
            "filesize": f.get("filesize") or f.get("filesize_approx"),
            "filesize_str": human_size(f.get("filesize") or f.get("filesize_approx")),
            "tbr": f.get("tbr"),
            "note": f.get("format_note") or "",
        })
    # 高清在前，纯音频放最后
    formats.sort(key=lambda x: (-(x["height"] or 0), -(x["tbr"] or 0)))

    return {
        "ok": True,
        "type": "site",
        "title": info.get("title"),
        "thumbnail": info.get("thumbnail"),
        "duration": fmt_duration(info.get("duration")),
        "uploader": info.get("uploader") or info.get("channel"),
        "webpage_url": info.get("webpage_url"),
        "formats": formats[:40],
        "best": {"format_id": "best", "ext": "mp4", "resolution": "自动最佳", "filesize_str": "-"},
    }


@app.get("/api/m3u8")
async def api_m3u8(url: str = Query(...), referer: Optional[str] = None):
    try:
        return await parse_m3u8(check_url(url), referer)
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)


async def parse_m3u8(url: str, referer: Optional[str]) -> dict:
    """取主播放列表，展开所有清晰度。"""
    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as cli:
        r = await cli.get(url, headers=headers_for(referer))
        if r.status_code != 200:
            return JSONResponse(
                {"ok": False, "error": f"m3u8 拉取失败 HTTP {r.status_code}，多半是 Referer/UA 校验"},
                status_code=502,
            )
        text = r.text

    variants = []
    seen = set()
    lines = [l.strip() for l in text.splitlines()]
    for i, line in enumerate(lines):
        if line.startswith("#EXT-X-STREAM-INF"):
            meta = line
            nxt = ""
            for j in range(i + 1, len(lines)):
                if lines[j] and not lines[j].startswith("#"):
                    nxt = lines[j]
                    break
            if not nxt:
                continue
            res = re.search(r"RESOLUTION=(\d+)x(\d+)", meta)
            bw = re.search(r"BANDWIDTH=(\d+)", meta)
            vurl = nxt if nxt.startswith("http") else urljoin(url, nxt)
            key = (res.group(0) if res else "", bw.group(1) if bw else "")
            if key in seen:
                continue
            seen.add(key)
            variants.append({
                "url": vurl,
                "resolution": f"{res.group(1)}x{res.group(2)}" if res else "未知",
                "height": int(res.group(2)) if res else 0,
                "bandwidth": int(bw.group(1)) if bw else 0,
            })

    # master 列表本身不含 EXTINF：抽最低码率子列表统计分片、判断直播
    child_text = text
    if variants:
        child_url = min(variants, key=lambda v: v["bandwidth"] or 10**12)["url"]
        try:
            async with httpx.AsyncClient(timeout=15, follow_redirects=True) as c2:
                r2 = await c2.get(child_url, headers=headers_for(referer))
                if r2.status_code == 200:
                    child_text = r2.text
        except Exception:
            pass

    is_live = "#EXT-X-ENDLIST" not in child_text
    encrypted = "#EXT-X-KEY" in text and "METHOD=NONE" not in text
    seg_count = child_text.count("#EXTINF")

    if variants:
        variants.sort(key=lambda x: (-x["height"], -x["bandwidth"]))
        formats = [{
            "format_id": f"v{i}",
            "ext": "mp4",
            "resolution": v["resolution"],
            "height": v["height"],
            "filesize_str": f"码率 {round((v['bandwidth'] or 0) / 1000)}kbps" if v["bandwidth"] else "-",
            "direct": v["url"],
        } for i, v in enumerate(variants)]
        formats = formats[:12]  # 同分辨率码率很多，前端只展示前 12 条
    else:
        formats = [{
            "format_id": "source",
            "ext": "mp4",
            "resolution": "原始流",
            "height": 0,
            "filesize_str": f"{seg_count} 个分片",
            "direct": url,
        }]

    return {
        "ok": True,
        "type": "m3u8",
        "title": url.rstrip("/").split("/")[-1].split("?")[0],
        "thumbnail": None,
        "duration": "-",
        "uploader": None,
        "webpage_url": url,
        "is_live": is_live,
        "encrypted": encrypted,
        "segments": seg_count,
        "formats": formats,
        "referer": referer,
    }


# ----------------------------------------------------------------------------- 代理
@app.get("/api/proxy")
async def api_proxy(url: str = Query(...), referer: Optional[str] = None):
    """流式转发，保留 Range 以支持播放器拖动。"""
    try:
        check_url(url)
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)

    req_headers = headers_for(referer)
    cli = httpx.AsyncClient(timeout=httpx.Timeout(30, read=60), follow_redirects=True)
    r = await cli.get(url, headers=req_headers)
    if r.status_code >= 400:
        await cli.aclose()
        return JSONResponse({"ok": False, "error": f"上游 HTTP {r.status_code}"}, status_code=502)

    async def stream():
        try:
            async for chunk in r.aiter_raw():
                yield chunk
        finally:
            await r.aclose()
            await cli.aclose()

    skip = {"content-encoding", "transfer-encoding", "connection", "content-length"}
    out_headers = {k: v for k, v in r.headers.items() if k.lower() not in skip}
    return StreamingResponse(stream(), media_type=r.headers.get("content-type", "application/octet-stream"),
                             headers=out_headers)


# ----------------------------------------------------------------------------- 保存
@app.get("/api/save")
async def api_save(
    url: str = Query(...),
    format_id: str = "best",
    referer: Optional[str] = None,
    filename: Optional[str] = None,
):
    try:
        check_url(url)
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)

    token = uuid.uuid4().hex[:10]
    safe_name = (filename or f"video_{token}").replace("/", "_").replace("\\", "_")

    if ".m3u8" in url.lower():
        # ffmpeg 直接合并 m3u8
        out_path = os.path.join(CACHE_DIR, f"{token}.mp4")
        cmd = ["ffmpeg", "-y", "-loglevel", "error"]
        hdr_lines = f"Referer: {referer or url}\r\nUser-Agent: {UA}\r\n"
        cmd += ["-headers", hdr_lines, "-i", url, "-c", "copy", "-bsf:a", "aac_adtstoasc", out_path]
        try:
            code, _, err = await run(cmd, timeout=900)
        except TimeoutError:
            return JSONResponse({"ok": False, "error": "下载超时，源可能过慢"}, status_code=504)
        if code != 0 or not os.path.exists(out_path):
            return JSONResponse({"ok": False, "error": f"ffmpeg 失败：{err.decode('utf-8','ignore')[-500:]}"}, status_code=500)
    else:
        # yt-dlp 下载 + 合并
        tpl = os.path.join(CACHE_DIR, f"{token}.%(ext)s")
        fmt = "bestvideo*+bestaudio/best" if format_id == "best" else format_id
        cmd = ytdlp_base() + [
            "--no-playlist", "--no-warnings",
            "--user-agent", UA, "--referer", referer or url,
            "-f", fmt, "--merge-output-format", "mp4",
            "-o", tpl, url,
        ]
        try:
            code, _, err = await run(cmd, timeout=900)
        except TimeoutError:
            return JSONResponse({"ok": False, "error": "下载超时"}, status_code=504)
        if code != 0:
            return JSONResponse({"ok": False, "error": f"yt-dlp 失败：{err.decode('utf-8','ignore')[-500:]}"}, status_code=500)
        files = [os.path.join(CACHE_DIR, f) for f in os.listdir(CACHE_DIR) if f.startswith(token + ".")]
        files = [f for f in files if os.path.getsize(f) > 1024]
        if not files:
            return JSONResponse({"ok": False, "error": "未生成有效文件"}, status_code=500)
        out_path = max(files, key=os.path.getsize)
        for f in files:
            if f != out_path:
                try:
                    os.remove(f)
                except OSError:
                    pass

    ext = os.path.splitext(out_path)[1] or ".mp4"
    return FileResponse(
        out_path,
        media_type="video/mp4" if ext == ".mp4" else "application/octet-stream",
        filename=f"{safe_name}{ext}",
        background=_cleanup_later(out_path),
    )


class _cleanup_later:
    """FileResponse 背景任务：响应结束后删临时文件。"""
    def __init__(self, path):
        self.path = path

    async def __call__(self):
        await asyncio.sleep(2)
        try:
            os.remove(self.path)
        except OSError:
            pass


# ----------------------------------------------------------------------------- 前端
@app.get("/", response_class=HTMLResponse)
async def index():
    with open(os.path.join(BASE_DIR, "static", "index.html"), "r", encoding="utf-8") as f:
        return HTMLResponse(f.read())


@app.get("/healthz")
async def health():
    return {"ok": True}
