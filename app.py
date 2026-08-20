"""image-grabber —— 抓取网页图片、预览、并转存到 Lychee 相册。

架构：
  浏览器页面 → 本服务(Flask) → 目标网站(提取/下载图片)
                         └→ Lychee API(转存到指定相册)

安全提示：
  这是一个自托管工具，默认阻止请求内网 IP，避免被当作 SSRF 跳板。
  如需抓取内网/局域网资源，可设置环境变量 BLOCK_PRIVATE_NETWORKS=false。
"""

from __future__ import annotations

import io
import ipaddress
import json
import os
import re
import secrets
import socket
import threading
import time as _time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import urllib.parse
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup
from flask import Flask, Response, jsonify, render_template, request
from PIL import Image


# ---------------------------------------------------------------- 配置
def _env_bool(name: str, default: bool) -> bool:
    return os.environ.get(name, "true" if default else "false").lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, ""))
    except ValueError:
        return default


LYCHEE_URL = os.environ.get("LYCHEE_URL", "").rstrip("/")
LYCHEE_TOKEN = os.environ.get("LYCHEE_TOKEN", "").strip()
UPLOAD_METHOD = os.environ.get("UPLOAD_METHOD", "multipart")  # multipart | import
MAX_IMAGES = _env_int("MAX_IMAGES", 200)
MAX_PAGE_BYTES = _env_int("MAX_PAGE_BYTES", 8 * 1024 * 1024)
MAX_IMAGE_BYTES = _env_int("MAX_IMAGE_BYTES", 20 * 1024 * 1024)
MAX_VIDEO_BYTES = _env_int("MAX_VIDEO_BYTES", 500 * 1024 * 1024)
BLOCK_PRIVATE_NETWORKS = _env_bool("BLOCK_PRIVATE_NETWORKS", True)
DEEP_EXTRACT_ENABLED = _env_bool("DEEP_EXTRACT_ENABLED", True)
DEEP_EXTRACT_SCROLLS = int(os.environ.get("DEEP_EXTRACT_SCROLLS", "3"))
PORT = _env_int("PORT", 8000)
# 书签存储(服务器共享, 跨设备可见可编辑)
BOOKMARK_FILE = os.environ.get("BOOKMARK_FILE", "data/bookmarks.json")
_BOOKMARK_MAX = 500
_bookmarks_lock = threading.Lock()
# 转存时把图片原图转为 WebP 再上传(更省空间;缩略图仍是 Lychee 生成的 JPEG)
WEBP_CONVERT_DEFAULT = _env_bool("WEBP_CONVERT_DEFAULT", True)
WEBP_QUALITY = _env_int("WEBP_QUALITY", 80)

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# 图片代理的进程内缓存
_CACHE_TTL = 30 * 60
_CACHE_MAX_BYTES = 64 * 1024 * 1024
_cache: dict[str, tuple[float, bytes, str]] = {}
_cache_lock = threading.Lock()


class FetchError(Exception):
    """抓取/上传类业务错误，message 直接展示给用户。"""


def _cache_get(key: str):
    now = _time.time()
    with _cache_lock:
        item = _cache.get(key)
        if item and now - item[0] < _CACHE_TTL:
            return item[1], item[2]
        if item:
            _cache.pop(key, None)
    return None


def _cache_put(key: str, data: bytes, content_type: str) -> None:
    with _cache_lock:
        if len(data) > _CACHE_MAX_BYTES:
            return
        total = sum(len(value[1]) for value in _cache.values())
        if total + len(data) > _CACHE_MAX_BYTES:
            _cache.clear()
        _cache[key] = (_time.time(), data, content_type)


# ---------------------------------------------------------------- 网络工具
def _download(
    url: str,
    *,
    referer: str | None = None,
    max_bytes: int = MAX_IMAGE_BYTES,
    allow_private: bool = False,
) -> tuple[bytes, str, str]:
    """下载远程资源，返回 (bytes, content_type, 最终URL)。

    allow_private=True 时跳过内网拦截(用于转存自己 Lychee 相册里的媒体)。
    """
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise FetchError("仅支持 http/https 链接")
    if BLOCK_PRIVATE_NETWORKS and not allow_private:
        _assert_public(url)
    headers = {"User-Agent": UA, "Accept": "*/*", "Accept-Language": "zh-CN,zh;q=0.9"}
    if referer:
        headers["Referer"] = referer
    try:
        resp = requests.get(
            url,
            headers=headers,
            stream=True,
            timeout=(8, 30),
            allow_redirects=True,
        )
    except requests.RequestException as exc:
        raise FetchError(f"网络请求失败: {exc.__class__.__name__}") from exc
    with resp:
        if resp.status_code != 200:
            raise FetchError(f"目标返回 HTTP {resp.status_code}")
        if BLOCK_PRIVATE_NETWORKS and not allow_private:
            _assert_public(resp.url)
        content_type = (
            resp.headers.get("Content-Type", "").split(";")[0].strip().lower()
        )
        buf = io.BytesIO()
        total = 0
        for chunk in resp.iter_content(64 * 1024):
            total += len(chunk)
            if total > max_bytes:
                raise FetchError(f"文件超过 {max_bytes // (1024 * 1024)}MB 限制")
            buf.write(chunk)
        return buf.getvalue(), content_type, resp.url


# 真正的内网/本机网段(不含 198.18.0.0/15 等代理工具虚拟地址段,避免误伤 Clash/Surge fake-ip)
_BLOCKED_NETWORKS = [
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
    ipaddress.ip_network("ff00::/8"),
]


def _is_blocked_ip(ip) -> bool:
    return any(ip in net for net in _BLOCKED_NETWORKS)


def _assert_public(url: str) -> None:
    host = urlsplit(url).hostname
    if not host:
        raise FetchError("无效 URL")
    if host == "localhost":
        raise FetchError("已阻止内网/本机地址(可在环境变量 BLOCK_PRIVATE_NETWORKS 关闭)")
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise FetchError(f"无法解析域名: {host}") from exc
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if _is_blocked_ip(ip):
            raise FetchError(
                f"已阻止内网地址 {ip}(可在环境变量 BLOCK_PRIVATE_NETWORKS 关闭)"
            )


def _probe(data: bytes) -> tuple | None:
    """用 Pillow 探测图片尺寸与格式，失败返回 None。"""
    try:
        with Image.open(io.BytesIO(data)) as im:
            return im.size, (im.format or "").lower()
    except Exception:
        return None


def _to_webp(data: bytes, fmt: str) -> bytes | None:
    """把图片字节转成 WebP，保留透明通道/EXIF/ICC；失败返回 None 由调用方回退原图。

    已是 WebP 或 GIF(可能多帧)直接跳过，避免无意义重编码或丢失动画。
    """
    fmt = (fmt or "").lower()
    if fmt in ("webp", "gif"):
        return None
    try:
        with Image.open(io.BytesIO(data)) as im:
            if getattr(im, "is_animated", False):
                return None
            if im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info):
                im = im.convert("RGBA")
            elif im.mode != "RGB":
                im = im.convert("RGB")
            buf = io.BytesIO()
            kwargs: dict = {"format": "WEBP", "quality": WEBP_QUALITY}
            if "exif" in im.info:
                kwargs["exif"] = im.info["exif"]
            if "icc_profile" in im.info:
                kwargs["icc_profile"] = im.info["icc_profile"]
            im.save(buf, **kwargs)
            return buf.getvalue()
    except Exception:
        return None


# ---------------------------------------------------------------- 图片提取
@dataclass
class ImageItem:
    url: str
    alt: str = ""
    source: str = "img"
    ref: str = ""  # 来源页面，用作 Referer


_SRC_ATTRS = (
    "src",
    "data-src",
    "data-original",
    "data-lazy-src",
    "data-lazy",
    "data-echo",
    "data-url",
    "data-image",
    "data-img",
    "data-thumb",
    "data-bg",
    "data-background-image",
)
# 常见占位图特征: src 是这类地址时,优先使用懒加载真实地址
_PLACEHOLDER_RE = re.compile(
    r"(?:placeholder|loading|spinner|blank|pixel|transparent|1x1|gray|grey|loading|lazy)",
    re.IGNORECASE,
)
_BAD_URL_PATH_RE = re.compile(r"[()\[\]{};,*|<>\"'\\\s]")
_VIDEO_EXT = (".mp4", ".webm", ".mov", ".m4v", ".ogv", ".avi", ".mkv")

# ---------------------------------------------------------------- 后台转存任务
# 任务保存在内存中，完成后保留一段时间自动清理（不持久化，重启即失效）。
_BATCH_TTL = 3600          # 任务完成/过期后保留秒数
_BATCH_MAX_ITEMS = 500     # 单次最多提交条数
# 后台转存并发：图片/视频分池。2核4G 建议 图片4 视频1（视频文件大, 防内存爆）
BATCH_CONCURRENCY_IMAGE = _env_int("BATCH_CONCURRENCY_IMAGE", 4)
BATCH_CONCURRENCY_VIDEO = _env_int("BATCH_CONCURRENCY_VIDEO", 1)
_batches: dict[str, dict] = {}
_batch_lock = threading.Lock()
_BATCH_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")

_IMAGE_EXT = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif", ".bmp", ".svg")


def _parse_srcset(value: str) -> list[tuple[str, int | None]]:
    parts: list[tuple[str, int | None]] = []
    for chunk in re.split(r",(?![^(]*\))", value or ""):
        chunk = chunk.strip()
        if not chunk:
            continue
        bits = chunk.split()
        if not bits:
            continue
        width = None
        for bit in bits[1:]:
            if bit.endswith("w"):
                try:
                    width = int(bit[:-1])
                except ValueError:
                    pass
        parts.append((bits[0], width))
    return parts


def _abs(base: str, value: str) -> str | None:
    value = value.strip().strip('"').strip("'")
    if not value or value.lower().startswith(
        ("data:", "blob:", "javascript:", "mailto:")
    ):
        return None
    return urljoin(base, value)


def _clean(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, ""))


def _best_srcset(value: str) -> str | None:
    candidates = _parse_srcset(value)
    if not candidates:
        return None
    candidates = [c for c in candidates if c[1] is not None] or candidates
    return max(candidates, key=lambda c: c[1] or 0)[0]


def extract_images(
    page_url: str, html: bytes, max_images: int = MAX_IMAGES
) -> list[ImageItem]:
    """从 HTML 提取图片：img/srcset/data-*、picture source、og:image、css background。"""
    soup = BeautifulSoup(html, "html.parser")
    base = page_url
    base_tag = soup.find("base", href=True)
    if base_tag:
        base = urljoin(page_url, base_tag["href"])

    found: list[ImageItem] = []
    seen: set[str] = set()

    def add(url_value: str, alt: str = "", source: str = "img") -> None:
        abs_url = _abs(base, url_value)
        if not abs_url:
            return
        abs_url = _clean(abs_url)
        if abs_url in seen or urlsplit(abs_url).scheme not in ("http", "https"):
            return
        seen.add(abs_url)
        found.append(ImageItem(url=abs_url, alt=alt or "", source=source, ref=page_url))

    # OG / Twitter 卡片图
    for prop in ("og:image", "og:image:url", "twitter:image"):
        tag = soup.find("meta", attrs={"property": prop}) or soup.find(
            "meta", attrs={"name": prop}
        )
        if tag and tag.get("content"):
            add(tag["content"], source=f"meta:{prop}")

    # img 标签(含懒加载属性与 srcset)
    for img in soup.find_all("img"):
        attrs = img.attrs
        url_value = None
        src_value = attrs.get("src") or ""
        if not src_value or _PLACEHOLDER_RE.search(src_value):
            # src 缺失或为占位图时,尝试懒加载属性里的真实地址
            for key in ("data-src", "data-original", "data-lazy-src", "data-lazy",
                        "data-echo", "data-url", "data-image", "data-img", "data-thumb"):
                if attrs.get(key):
                    url_value = attrs[key]
                    break
        else:
            url_value = src_value
        srcset_value = attrs.get("srcset") or attrs.get("data-srcset")
        if srcset_value:
            best = _best_srcset(srcset_value)
            if best and not _PLACEHOLDER_RE.search(best):
                url_value = best
        if url_value:
            add(url_value, alt=str(img.get("alt") or ""), source="img")

    # <picture> 里的 <source srcset/src>
    for src in soup.find_all("source"):
        if src.find_parent("video"):
            continue  # 视频源由 extract_videos 处理
        if src.get("srcset"):
            best = _best_srcset(src["srcset"])
            if best:
                add(best, source="picture")
        elif src.get("src"):
            add(src["src"], source="picture")

    # 内联样式里的 background-image
    css_re = re.compile(r"background(?:-image)?\s*:\s*url\(\s*['\"]?([^'\")\s]+)")
    for el in soup.find_all(style=True):
        for match in css_re.finditer(el["style"]):
            add(match.group(1), source="css")

    # 兜底: 全页任意位置出现图片/视频扩展名的 URL(借鉴 gallery-dl 通用提取器,
    # 覆盖内嵌 JSON/JS/__NEXT_DATA__/文本里的资源, 含相对路径)
    if len(found) < max_images:
        html_text = html.decode("utf-8", "ignore")
        # 排除 srcset 属性(已由 img/source 逻辑选最高清收录, 避免重复低清候选)
        fallback_text = re.sub(
            r"(?i)(?:srcset|data-srcset)=[\"'][^\"']*[\"']", "", html_text
        )
        for match in re.finditer(
            r"(?i)(?:[^?&#\"'\'>\s]+)\.(?:jpe?g|jpe|png|gif|web[mp]|avif|bmp)(?:[^\"'<>\s]*)?",
            fallback_text,
        ):
            add(match.group(0), source="json")
            if len(found) >= max_images:
                break

    return found[:max_images]


def extract_videos(
    page_url: str, html: bytes, max_videos: int = MAX_IMAGES
) -> list[ImageItem]:
    """从 HTML 提取视频链接：video/source、og:video、指向视频文件的链接。"""
    soup = BeautifulSoup(html, "html.parser")
    base = page_url
    base_tag = soup.find("base", href=True)
    if base_tag:
        base = urljoin(page_url, base_tag["href"])

    found: list[ImageItem] = []
    seen: set[str] = set()

    def add(url_value: str, alt: str = "", source: str = "video") -> None:
        abs_url = _abs(base, url_value)
        if not abs_url:
            return
        abs_url = _clean(abs_url)
        if abs_url in seen or urlsplit(abs_url).scheme not in ("http", "https"):
            return
        if not abs_url.lower().endswith(_VIDEO_EXT):
            return  # 排除 embed/播放页/直播流，只收直接可下载的视频文件
        seen.add(abs_url)
        found.append(ImageItem(url=abs_url, alt=alt or "", source=source, ref=page_url))

    # og:video / twitter:player
    for prop in ("og:video", "og:video:url", "og:video:secure_url", "twitter:player"):
        tag = soup.find("meta", attrs={"property": prop}) or soup.find(
            "meta", attrs={"name": prop}
        )
        if tag and tag.get("content"):
            add(tag["content"], source=f"meta:{prop}")

    # <video> 标签(含懒加载写法)
    for video in soup.find_all("video"):
        url_value = next(
            (video.get(k) for k in _SRC_ATTRS if video.get(k)), None
        )
        if url_value:
            add(url_value, alt=str(video.get("title") or ""), source="video")
        for src in video.find_all("source"):
            if src.get("src"):
                add(src["src"], source="video-source")

    # <a href> 直接指向视频文件(常见于"下载"按钮)
    for a in soup.find_all("a", href=True):
        if a["href"].lower().endswith(_VIDEO_EXT):
            add(a["href"], alt=str(a.get_text(strip=True) or ""), source="link")

    # 兜底: 全页任意位置出现视频扩展名的 URL(覆盖 JSON/JS 里的视频资源)
    if len(found) < max_videos:
        html_text = html.decode("utf-8", "ignore")
        ext_pattern = "|".join(e[1:] for e in _VIDEO_EXT)
        for match in re.finditer(
            rf"(?i)(?:[^?&#\"'\'>\s]+)\.(?:{ext_pattern})(?:[^\"'<>\s]*)?",
            html_text,
        ):
            add(match.group(0), source="video")
            if len(found) >= max_videos:
                break

    return found[:max_videos]


# ---------------------------------------------------------------- Lychee API
def _lychee_headers(token: str) -> dict:
    # Lychee v4 认证：Authorization 头直接放原始 token(服务端对其做 SHA512 比对)，不带 Bearer
    return {
        "Authorization": token,
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": UA,
    }


_SMART_ALBUM_IDS = {
    # Lychee 7 内置智能相册(不可作为上传目标)
    "unsorted", "highlighted", "recent", "on_this_day", "untagged",
    "unrated", "one_star", "two_stars", "three_stars", "four_stars",
    "five_stars", "best_pictures", "my_rated_pictures", "my_best_pictures",
    # 其他版本/结构中的内置相册
    "starred", "public", "trash", "featured", "search", "shared", "tag",
}


def _album_briefs(data) -> list[dict]:
    """递归提取相册节点(id/title/子相册标记)，兼容 Lychee 不同版本。

    数组/对象/嵌套树、数字 id 与随机字符串 id 均可；排除 smart albums 等
    内置相册。保留 num_subalbums / has_subalbum / has_albums 供上层判断
    是否需要继续拉取子相册。
    """
    out: list[dict] = []
    seen: set[str] = set()

    def walk(node) -> None:
        if isinstance(node, dict):
            album_id = node.get("id")
            title = node.get("title")
            if album_id is not None and title is not None:
                aid = str(album_id)
                # 排除 smart albums 等内置相册,保留真实相册(数字或随机字符串 id)
                if aid not in _SMART_ALBUM_IDS and aid not in seen:
                    seen.add(aid)
                    num_subalbums = node.get("num_subalbums") or 0
                    has_subalbum = bool(
                        node.get("has_subalbum")
                        or node.get("has_albums")
                        or num_subalbums
                    )
                    out.append(
                        {
                            "id": aid,
                            "title": str(title),
                            "num_subalbums": int(num_subalbums),
                            "has_subalbum": has_subalbum,
                        }
                    )
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(data)
    return out


def flatten_albums(data) -> list[dict]:
    """递归提取相册，兼容 Lychee 不同版本(数组/对象/嵌套树,数字 id 与随机字符串 id)。"""
    return [{"id": a["id"], "title": a["title"]} for a in _album_briefs(data)]


def _lychee_check(resp: requests.Response) -> None:
    """统一检查 Lychee 响应：区分 token 无效 / 非 JSON(未认证或地址不对) / 其他错误。"""
    if resp.status_code in (401, 406):
        raise FetchError("API Token 无效(HTTP %d),请到 Lychee 个人设置 → 修改登录信息 → API Token 新建后重试" % resp.status_code)
    if resp.status_code == 400 and "token" in resp.text.lower():
        raise FetchError("API Token 无效(HTTP 400),请到 Lychee 个人设置 → 修改登录信息 → API Token 新建后重试")
    ctype = resp.headers.get("Content-Type", "")
    if resp.status_code == 200 and "json" not in ctype:
        raise FetchError("API Token 无效或未创建,请到 Lychee 个人设置 → 修改登录信息 → API Token 新建后重试")
    if resp.status_code < 200 or resp.status_code >= 300:
        if "json" not in ctype:
            raise FetchError(f"Lychee 返回 HTTP {resp.status_code}({ctype or '无 Content-Type'}): {resp.text[:160]}")
        raise FetchError(f"Lychee 返回 HTTP {resp.status_code}: {resp.text[:200]}")


def lychee_albums(base: str, token: str) -> list[dict]:
    """拉取 Lychee v4 相册列表(含嵌套子相册)，返回扁平数组供前端下拉选择。

    v4 接口: POST /api/Albums::get, 返回嵌套 albums 树(含 shared_albums)。
    子相册的 title/path 形如「父相册 / 子相册」，便于下拉框展示层级。
    """
    headers = _lychee_headers(token)
    resp = requests.post(f"{base}/api/Albums::get", json={}, headers=headers, timeout=20)
    _lychee_check(resp)
    data = resp.json()

    out: list[dict] = []
    seen: set[str] = set()

    def walk(node: dict, prefix: str = "", depth: int = 0) -> None:
        if depth > 20:
            return
        aid = str(node.get("id") or "")
        title = str(node.get("title") or "")
        if not aid or not title:
            return
        if aid in seen:
            return
        seen.add(aid)
        label = f"{prefix} / {title}" if prefix else title
        out.append({"id": aid, "title": title, "path": label})
        for child in node.get("albums") or []:
            if isinstance(child, dict):
                walk(child, label, depth + 1)

    if isinstance(data, dict):
        for collection in ("albums", "shared_albums"):
            for node in data.get(collection) or []:
                if isinstance(node, dict):
                    walk(node)
    elif isinstance(data, list):
        for node in data:
            if isinstance(node, dict):
                walk(node)
    return out


def lychee_upload(
    base: str,
    token: str,
    album_id: str,
    data: bytes,
    filename: str,
    content_type: str,
    title: str = "",
) -> str:
    """multipart 方式上传图片字节到指定相册(Lychee v4: POST /api/Photo::add)。"""
    form = {"albumID": str(album_id)} if album_id else {}
    if title:
        form["title"] = title
    upload_headers = {k: v for k, v in _lychee_headers(token).items() if k != "Content-Type"}
    resp = requests.post(
        f"{base}/api/Photo::add",
        data=form,
        files={"file": (filename, data, content_type or "application/octet-stream")},
        headers=upload_headers,
        timeout=180,
    )
    _lychee_check(resp)
    try:
        return str(resp.json().get("id") or "")
    except Exception:
        return ""


def lychee_import_url(base: str, token: str, album_id: str, url: str) -> None:
    """让 Lychee v4 服务端直接从 URL 导入(需 Lychee 能访问该图片地址)。"""
    body: dict = {"urls": [url]}
    if album_id:
        body["albumID"] = str(album_id)
    resp = requests.post(
        f"{base}/api/Import::url",
        json=body,
        headers=_lychee_headers(token),
        timeout=60,
    )
    if resp.status_code == 404:
        raise FetchError("Lychee 不支持该导入接口,请改用 UPLOAD_METHOD=multipart")
    _lychee_check(resp)


def lychee_album_photos(base: str, token: str, album_id: str) -> list[str]:
    """返回 Lychee v4 相册内全部照片 ID(POST /api/Album::get)。"""
    resp = requests.post(
        f"{base}/api/Album::get",
        json={"albumID": str(album_id)},
        headers=_lychee_headers(token),
        timeout=30,
    )
    _lychee_check(resp)
    data = resp.json()
    return [str(p.get("id")) for p in _lychee_photo_list(data) if p.get("id")]


def lychee_delete_photos(base: str, token: str, photo_ids: list[str]) -> None:
    """批量删除照片(Lychee v4: POST /api/Photo::delete)。"""
    if not photo_ids:
        return
    resp = requests.post(
        f"{base}/api/Photo::delete",
        json={"photoIDs": photo_ids},
        headers=_lychee_headers(token),
        timeout=60,
    )
    _lychee_check(resp)


def _transfer_one(
    item: dict,
    album_id: str,
    base: str,
    token: str,
    allow_private: bool = False,
    convert_webp: bool = False,
) -> dict:
    """转存单个媒体(url/title/ref/type)到 Lychee，返回结果 dict。

    与 /api/upload 共用同一套校验与上传逻辑，供后台批量任务复用。
    """
    url = (item.get("url") or "").strip()
    title = (item.get("title") or "").strip()
    ref = item.get("ref") or ""
    kind = (item.get("type") or "image").strip().lower()
    if not url:
        return {"ok": False, "error": "缺少 url"}
    if kind not in ("image", "video"):
        return {"ok": False, "error": "type 仅支持 image 或 video"}

    if UPLOAD_METHOD == "import":
        try:
            lychee_import_url(base, token, album_id, url)
            return {"ok": True, "method": "import", "type": kind}
        except (FetchError, requests.RequestException) as exc:
            return {"ok": False, "error": str(exc)}

    # multipart：先在本服务下载，再以字节流上传，不依赖 Lychee 到外网的连通性
    try:
        data, content_type, _ = _download(
            url,
            referer=ref or None,
            max_bytes=MAX_VIDEO_BYTES if kind == "video" else MAX_IMAGE_BYTES,
            allow_private=allow_private,
        )
    except FetchError as exc:
        return {"ok": False, "error": f"下载失败: {exc}"}

    ext = os.path.splitext(urlsplit(url).path)[1].lower()
    probe = None
    if kind == "video":
        if "video" not in content_type and ext not in _VIDEO_EXT:
            return {"ok": False, "error": "下载内容不是有效视频"}
        fmt = content_type.split("/")[-1] if "/" in content_type else ""
        if fmt == "quicktime":
            fmt = "mov"
    else:
        probe = _probe(data)
        if probe is None:
            return {"ok": False, "error": "下载内容不是有效图片"}
        (width, height), fmt = probe

    converted = False
    if convert_webp and kind == "image":
        webp_data = _to_webp(data, fmt)
        if webp_data is not None:
            data = webp_data
            content_type = "image/webp"
            fmt = "webp"
            converted = True

    filename = os.path.basename(urlsplit(url).path) or "image"
    if converted:
        filename = os.path.splitext(filename)[0] + ".webp"
    elif not os.path.splitext(filename)[1]:
        filename += "." + (fmt or ("mp4" if kind == "video" else "jpg")).lower()
    try:
        photo_id = lychee_upload(
            base,
            token,
            album_id,
            data,
            filename,
            content_type,
            title or filename,
        )
    except (FetchError, requests.RequestException) as exc:
        return {"ok": False, "error": str(exc)}
    result: dict = {"ok": True, "photo_id": photo_id, "type": kind}
    if converted:
        result["webp"] = True
    if probe:
        result["width"] = width
        result["height"] = height
    return result


# ---------------------------------------------------------------- Lychee 相册提取
_LYCHEE_GALLERY_RE = re.compile(r"^/gallery/([A-Za-z0-9_-]{8,})$")
_LYCHEE_ALBUM_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,}$")


def _lychee_album_id(url: str) -> str | None:
    """从 Lychee 相册页 URL 提取相册 ID, 兼容两代路由。

    - v7+:  /gallery/{albumID}
    - v4:   /#{albumID} 或 /#{albumID}/{photoID}(相册 ID 在 hash 第一段)
    非 Lychee 相册 URL 返回 None。
    """
    parts = urlsplit(url)
    m = _LYCHEE_GALLERY_RE.match(parts.path)
    if m:
        return m.group(1)
    frag = parts.fragment.strip("/")
    if not frag:
        return None
    for token in frag.split("/"):
        # map/search/login 等内置页名都短于 8 位, 天然被长度过滤排除
        if _LYCHEE_ALBUM_ID_RE.match(token):
            return token
    return None


def _lychee_photo_list(data) -> list[dict]:
    """从 Lychee API 返回中取出照片数组(v4 photos[] / v7 data/items)。"""
    photos: list = []
    if isinstance(data, dict):
        for key in ("photos", "data", "items"):
            if isinstance(data.get(key), list):
                photos = data[key]
                break
    elif isinstance(data, list):
        photos = data
    return [p for p in photos if isinstance(p, dict)]


def _lychee_album_children(data) -> list[dict]:
    """从 Lychee 相册响应中取出子相册数组(嵌套相册)。"""
    if isinstance(data, dict):
        for key in ("albums", "children", "subalbums"):
            if isinstance(data.get(key), list):
                return [a for a in data[key] if isinstance(a, dict)]
    return []


def _lychee_photo_url(photo: dict) -> str | None:
    """兼容不同 Lychee 版本/魔改版的字段风格, 取媒体 URL。

    - v7:        size_variants.original.url
    - v4:        同 v7(小写变体)
    - 魔改版:     顶层 url / 驼峰 sizeVariants
    """
    # 顶层 url(部分魔改版把原图/原视频直接放顶层)
    for key in ("url", "original_url", "big_url", "full_url"):
        v = photo.get(key)
        if isinstance(v, str) and v:
            return v
    # size_variants / sizeVariants / sizes 变体
    for vk in ("size_variants", "sizeVariants", "sizes"):
        variants = photo.get(vk)
        if not isinstance(variants, dict):
            continue
        for key in ("original", "full", "big", "medium", "small", "thumb"):
            v = variants.get(key)
            if isinstance(v, dict) and isinstance(v.get("url"), str) and v["url"]:
                return v["url"]
            if isinstance(v, str) and v:
                return v
    return None


def _extract_lychee_album(page_url: str, token: str | None = None) -> tuple[list[ImageItem], list[ImageItem]] | None:
    """Lychee 相册页的照片由 JS 异步加载,这里直接调其 API 取原图/视频。

    v4 专用: POST /api/Album::get(JSON body), 递归提取嵌套子相册。

    优先带 API Token(Authorization 头)请求——v4 对匿名请求会返回空相册或 419,
    私有相册/子相册必须带 Token 才能读到; 带 Token 失败时回退到匿名(XSRF cookie)请求,
    以兼容公开相册与其他 Lychee 实例。
    非 Lychee 相册页或调用失败时返回 None,上层回退到普通 HTML 提取。
    """
    parts = urlsplit(page_url)
    album_id = _lychee_album_id(page_url)
    if not album_id:
        return None
    base = f"{parts.scheme}://{parts.netloc}"
    session = requests.Session()
    session.headers["User-Agent"] = UA
    images: list[ImageItem] = []
    videos: list[ImageItem] = []
    seen: set[str] = set()
    try:
        page_resp = session.get(page_url, timeout=15)
        if page_resp.status_code != 200:
            return None
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        for cookie in session.cookies:
            if cookie.name == "XSRF-TOKEN":
                headers["X-XSRF-TOKEN"] = urllib.parse.unquote(cookie.value)
                break
    except Exception:
        return None

    def fetch_album(aid: str, depth: int = 0, hdrs: dict | None = None) -> bool:
        """拉取单个相册(照片+子相册), 返回是否取到任何媒体。"""
        if depth > 12:
            return False
        try:
            api_resp = session.post(
                f"{base}/api/Album::get",
                json={"albumID": aid},
                headers=hdrs or headers,
                timeout=20,
            )
            if api_resp.status_code != 200:
                return False
            try:
                data = api_resp.json()
            except Exception:
                return False
        except Exception:
            return False

        found = False
        for photo in _lychee_photo_list(data):
            title = str(photo.get("title") or "")
            ptype = str(photo.get("type") or "").lower()
            url = _lychee_photo_url(photo)
            if not url:
                continue
            # v4/魔改版返回相对路径, 需要用站点地址补全
            abs_url = _clean(urljoin(base, url))
            if urlsplit(abs_url).scheme not in ("http", "https") or abs_url in seen:
                continue
            seen.add(abs_url)
            is_video = ptype.startswith("video") or abs_url.lower().endswith(_VIDEO_EXT)
            item = ImageItem(url=abs_url, alt=title, source="lychee", ref=page_url)
            (videos if is_video else images).append(item)
            found = True

        for sub in _lychee_album_children(data):
            sub_id = str(sub.get("id") or "")
            if _LYCHEE_ALBUM_ID_RE.match(sub_id):
                found = fetch_album(sub_id, depth + 1, hdrs) or found
        return found

    def attempt(hdrs: dict | None) -> tuple[list[ImageItem], list[ImageItem]] | None:
        try:
            if not fetch_album(album_id, hdrs=hdrs):
                return None
        except Exception:
            return None
        return images, videos

    # 优先带 token(自己的实例/私有相册); 取不到媒体时清空结果回退匿名(公开相册/其他实例)
    if token:
        res = attempt({"Authorization": token, **headers})
        if res is not None and (res[0] or res[1]):
            return res
        images.clear()
        videos.clear()
        seen.clear()
    return attempt(None)


def _media_urls_from_text(text: str, page_url: str, max_items: int = MAX_IMAGES) -> list[ImageItem]:
    """通用媒体嗅探: 从任意文本(JSON/JS/HTML)里找出图片/视频直链。

    不依赖站点类型, 是深度提取的兜底网络——只要页面加载过含媒体直链的
    数据(相册 JSON、接口返回等), 即使 DOM 里没有也能抓到。
    """
    exts = "|".join(
        sorted({e[1:] for e in (_VIDEO_EXT + _IMAGE_EXT)}, key=len, reverse=True)
    )
    sep = chr(34) + chr(39) + "<>"  # 双引号/单引号/尖括号不参与 URL
    pattern = re.compile(
        rf"(?i)([^{sep}\s]{{1,500}}\.(?:{exts})(?:\?[^{sep}\s]*)?)"
    )
    found: list[ImageItem] = []
    seen: set[str] = set()
    for match in pattern.finditer(text):
        raw = match.group(1).strip("\\" + chr(34) + chr(39)).rstrip(
            ".,;:!?)]}>" + chr(34) + chr(39) + "\\"
        )
        # JSON 字符串里 \/ 是转义, 还原为正斜杠, 否则下载时路径带反斜杠
        raw = raw.replace("\\/", "/")
        if not raw or _PLACEHOLDER_RE.search(raw):
            continue
        abs_url = _abs(page_url, raw)
        if not abs_url:
            continue
        abs_url = _clean(abs_url)
        if abs_url in seen or urlsplit(abs_url).scheme not in ("http", "https"):
            continue
        # 过滤 JS/代码碎片: 真实媒体 URL 的路径里不应出现括号/分号/逗号/星号/反斜杠等
        if _BAD_URL_PATH_RE.search(urlsplit(abs_url).path):
            continue
        seen.add(abs_url)
        found.append(ImageItem(url=abs_url, alt="", source="sniff", ref=page_url))
        if len(found) >= max_items:
            break
    return found


# ---------------------------------------------------------------- 深度提取(无头浏览器渲染)
def render_extract(
    url: str, max_images: int = MAX_IMAGES, scroll_times: int | None = None
) -> tuple[list[ImageItem], list[ImageItem]] | None:
    """用无头 Chromium 渲染页面, 触发 JS 与懒加载后提取图片/视频。

    返回 (images, videos); 未安装/禁用/失败时返回 None, 由上层回退到静态提取。
    """
    if not DEEP_EXTRACT_ENABLED:
        return None
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        return None
    scroll_times = DEEP_EXTRACT_SCROLLS if scroll_times is None else scroll_times
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
            )
            try:
                context = browser.new_context(
                    user_agent=UA,
                    locale="zh-CN",
                    viewport={"width": 1366, "height": 900},
                )
                page = context.new_page()
                sniffed_bodies: list[str] = []

                def on_response(resp):
                    # 收集 JSON/JS/HTML 响应体, 供媒体 URL 嗅探
                    try:
                        ctype = resp.headers.get("content-type", "").lower()
                        if not any(
                            k in ctype
                            for k in ("json", "javascript", "html", "text", "x-www-form-urlencoded")
                        ):
                            return
                        body = resp.text()
                        if body and len(body) <= 4 * 1024 * 1024:
                            sniffed_bodies.append(body)
                    except Exception:
                        pass

                page.on("response", on_response)
                page.goto(url, wait_until="domcontentloaded", timeout=30000)
                page.wait_for_timeout(1500)
                for _ in range(scroll_times):
                    page.mouse.wheel(0, 2500)
                    page.wait_for_timeout(1500)
                try:
                    page.wait_for_load_state("networkidle", timeout=8000)
                except Exception:
                    pass
                page.wait_for_timeout(1200)
                html = page.content()
                final_url = page.url or url
            finally:
                browser.close()
    except Exception:
        return None
    images = extract_images(final_url, html.encode("utf-8", "ignore"), max_images)
    videos = extract_videos(final_url, html.encode("utf-8", "ignore"), max_images)
    if sniffed_bodies:
        # 网络嗅探结果补缺: DOM 已找到的优先, 不重复
        known = {item.url for item in images} | {item.url for item in videos}
        for body in sniffed_bodies:
            for item in _media_urls_from_text(body, final_url, max_images):
                if item.url in known:
                    continue
                known.add(item.url)
                if item.url.lower().endswith(_VIDEO_EXT):
                    videos.append(item)
                else:
                    images.append(item)
        images = images[:max_images]
        videos = videos[:max_images]
    return images, videos


# ---------------------------------------------------------------- Flask 路由
app = Flask(__name__)
app.json.ensure_ascii = False


@app.get("/")
def index():
    return render_template(
        "index.html",
        default_lychee_url=LYCHEE_URL,
        default_lychee_token=LYCHEE_TOKEN,
        upload_method=UPLOAD_METHOD,
        max_images=MAX_IMAGES,
        default_convert_webp=WEBP_CONVERT_DEFAULT,
    )


@app.post("/api/extract")
def api_extract():
    body = request.get_json(silent=True) or {}
    url = (body.get("url") or "").strip()
    if not url:
        return jsonify(ok=False, error="请输入要抓取的网址"), 400
    deep = bool(body.get("deep")) or request.args.get("deep") == "1"
    mode = "html"
    lychee_token = (body.get("lychee_token") or "").strip() or LYCHEE_TOKEN or None
    lychee_result = _extract_lychee_album(url, token=lychee_token)
    if lychee_result is not None:
        items, videos = lychee_result
        final_url = url
        mode = "lychee"
    elif deep:
        deep_result = render_extract(url)
        if deep_result is not None:
            items, videos = deep_result
            final_url = url
            mode = "deep"
        else:
            try:
                html, _, final_url = _download(url, max_bytes=MAX_PAGE_BYTES)
            except FetchError as exc:
                return jsonify(ok=False, error=str(exc)), 502
            items = extract_images(final_url, html)
            videos = extract_videos(final_url, html)
    else:
        try:
            html, _, final_url = _download(url, max_bytes=MAX_PAGE_BYTES)
        except FetchError as exc:
            return jsonify(ok=False, error=str(exc)), 502
        items = extract_images(final_url, html)
        videos = extract_videos(final_url, html)
    return jsonify(
        ok=True,
        source=final_url,
        mode=mode,
        count=len(items) + len(videos),
        images=[
            {
                "url": item.url,
                "alt": item.alt,
                "source": item.source,
                "ref": item.ref,
            }
            for item in items
        ],
        videos=[
            {
                "url": item.url,
                "alt": item.alt,
                "source": item.source,
                "ref": item.ref,
            }
            for item in videos
        ],
    )


@app.post("/api/extract_html")
def api_extract_html():
    """直接解析前端提交的已渲染 HTML(预览 iframe 中已加载的内容), 不再重新抓取。"""
    body = request.get_json(silent=True) or {}
    html = body.get("html") or ""
    url = (body.get("url") or "").strip()
    if not html or not url:
        return jsonify(ok=False, error="缺少参数(html/url)"), 400
    items = extract_images(url, html.encode("utf-8", "ignore"))
    videos = extract_videos(url, html.encode("utf-8", "ignore"))
    return jsonify(
        ok=True,
        source=url,
        mode="preview",
        count=len(items) + len(videos),
        images=[
            {"url": item.url, "alt": item.alt, "source": item.source, "ref": item.ref}
            for item in items
        ],
        videos=[
            {"url": item.url, "alt": item.alt, "source": item.source, "ref": item.ref}
            for item in videos
        ],
    )


def _lychee_page_html(url: str, html: str) -> str:
    """把 Lychee 相册页改写成可在本服务 iframe 中同源运行的版本。

    所有指向 Lychee 自身的地址改写成同源 /lp/<主机>/... 代理路径,
    这样主 bundle 与动态 import 的分包、以及 SPA 的 API 请求都不受跨域限制。
    """
    parts = urlsplit(url)
    netloc = parts.netloc
    target = f"{parts.scheme}://{netloc}"
    html = html.replace(target, f"/lp/{netloc}")
    for attr in ('"/dist/', "'/dist/", '"/build/', "'/build/", '"/img/', "'/img/", '"/uploads/', "'/uploads/"):
        html = html.replace(attr, f"/lp/{netloc}" + attr[1:])
    api_js = (
        "<script>(function(){"
        "var O=location.origin,B=document.baseURI||location.href,T='" + target + "';"
        "function pw(u){var a;try{a=new URL(u,B).href;}catch(e){return null;}"
        "var p;try{p=new URL(a).pathname;}catch(e){return null;}"
        "if(p.indexOf('/api/')===0&&(a.indexOf(T)===0||a.indexOf(O)===0)){"
        "var s=new URL(a).search;"
        "return O+'/api/passthrough?url='+encodeURIComponent(T+p+s);}return null;}"
        "var f=window.fetch;if(f){window.fetch=function(i,o){"
        "var u=typeof i==='string'?i:(i&&i.url);var r=u?pw(u):null;"
        "if(r){var ni=(i instanceof Request&&!o)?new Request(r,i):(o||{});"
        "return f(r,ni);}return f.apply(this,arguments);};}"
        "var ox=XMLHttpRequest.prototype.open;"
        "XMLHttpRequest.prototype.open=function(m,u){var r=pw(u);"
        "return ox.call(this,m,r||u,arguments[2],arguments[3],arguments[4]);};"
        "})();</script>"
    )
    if "</body>" in html:
        html = html.replace("</body>", api_js + "</body>", 1)
    else:
        html += api_js
    return html


def _lychee_page_response(url: str, html: str) -> Response:
    """包装 Lychee 相册预览页: 去掉禁止嵌入头, 并授权 /lp/ 代理访问。"""
    out = Response(html, content_type="text/html; charset=utf-8")
    out.set_cookie("lp_host", urlsplit(url).netloc, max_age=3600, samesite="Lax")
    for header in ("X-Frame-Options", "x-frame-options", "Content-Security-Policy", "content-security-policy"):
        out.headers.pop(header, None)
    return out


@app.get("/api/frame")
def api_frame():
    """网页预览代理: 拉取目标页面, 移除禁止嵌入的响应头, 供 iframe 显示。

    同时注入 <base>, 让页面里的相对资源能正确加载。
    自己的 Lychee 相册页(局域网地址)自动放行, 并改写成同源 /lp/... 版本,
    让其 SPA 的静态资源与 API 请求都由本服务同源代理, 避开跨域限制。
    """
    url = request.args.get("url", "")
    if not url or urlsplit(url).scheme not in ("http", "https"):
        return jsonify(ok=False, error="无效 URL"), 400
    is_lychee = _lychee_album_id(url) is not None
    try:
        if BLOCK_PRIVATE_NETWORKS and not is_lychee:
            _assert_public(url)
        headers = {"User-Agent": UA, "Accept": "text/html,application/xhtml+xml,*/*;q=0.8"}
        resp = requests.get(url, headers=headers, timeout=15, allow_redirects=True)
        if resp.status_code != 200:
            return jsonify(ok=False, error=f"目标返回 HTTP {resp.status_code}"), 502
        ctype = resp.headers.get("Content-Type", "").lower()
        if "html" not in ctype:
            return Response(resp.content, content_type=ctype or "application/octet-stream")
        if is_lychee:
            return _lychee_page_response(url, _lychee_page_html(url, resp.text))
        html = resp.text
        if re.search(r"<base\s", html, re.IGNORECASE) is None:
            html = re.sub(
                r"(<head[^>]*>)",
                lambda m: m.group(1) + "<base href='" + resp.url + "'>",
                html,
                count=1,
                flags=re.IGNORECASE,
            )
        # 注入拦截脚本: iframe 内的链接继续走代理(保持同源可读取), 目标页面相对链接用 base 解析
        inject_js = (
            "<script>(function(){"
            "var targetBase=document.baseURI||location.href;"
            "document.addEventListener('click',function(e){"
            "var a=e.target&&e.target.closest?e.target.closest('a[href]'):null;"
            "if(!a)return;var href=a.getAttribute('href');"
            "if(!href||href.indexOf('javascript:')===0)return;"
            "var abs;try{abs=new URL(href,targetBase).href;}catch(err){return;}"
            "if(abs.indexOf('http')!==0)return;"
            "e.preventDefault();"
            "location.href=location.origin+'/api/frame?url='+encodeURIComponent(abs);"
            "});})();</script>"
        )
        if "</body>" in html:
            html = html.replace("</body>", inject_js + "</body>", 1)
        else:
            html += inject_js
        out = Response(html, content_type="text/html; charset=utf-8")
        for header in ("X-Frame-Options", "x-frame-options", "Content-Security-Policy", "content-security-policy"):
            out.headers.pop(header, None)
        return out
    except FetchError as exc:
        return jsonify(ok=False, error=str(exc)), 502
    except requests.RequestException as exc:
        return jsonify(ok=False, error=f"预览失败: {exc.__class__.__name__}"), 502


def _lychee_api_forward(
    method: str, url: str, body: bytes | None, content_type: str | None
) -> requests.Response:
    """带匿名会话(XSRF cookie)转发 Lychee API 请求。"""
    parts = urlsplit(url)
    base = f"{parts.scheme}://{parts.netloc}"
    session = requests.Session()
    session.headers["User-Agent"] = UA
    try:
        session.get(base + "/", timeout=10)
    except requests.RequestException:
        pass
    headers = {
        "Accept": "application/json",
        "Content-Type": content_type or "application/json",
    }
    xsrf = next(
        (urllib.parse.unquote(c.value) for c in session.cookies if c.name == "XSRF-TOKEN"),
        None,
    )
    if xsrf:
        headers["X-XSRF-TOKEN"] = xsrf
    return session.request(
        method, url, headers=headers, data=body, timeout=25, allow_redirects=True
    )


@app.route("/api/passthrough", methods=["GET", "POST", "PUT", "DELETE"])
def api_passthrough():
    """供预览 iframe 内的 Lychee SPA 同源调用其 API(绕过 CORS)。

    仅转发 /api/ 路径; 其他内网地址仍被拦截。
    """
    url = request.args.get("url", "")
    if not url or urlsplit(url).scheme not in ("http", "https"):
        return jsonify(ok=False, error="缺少或无效的 url"), 400
    parts = urlsplit(url)
    if BLOCK_PRIVATE_NETWORKS and not parts.path.startswith("/api/"):
        try:
            _assert_public(url)
        except FetchError as exc:
            return jsonify(ok=False, error=str(exc)), 502
    try:
        resp = _lychee_api_forward(
            request.method,
            url,
            request.get_data() or None,
            (request.headers.get("Content-Type") or "application/json").split(";")[0],
        )
    except requests.RequestException as exc:
        return jsonify(ok=False, error=f"转发失败: {exc.__class__.__name__}"), 502
    ctype = resp.headers.get("Content-Type", "application/json").split(";")[0]
    out = Response(resp.content, status=resp.status_code, mimetype=ctype or "application/json")
    out.headers["Cache-Control"] = "no-store"
    return out


@app.route("/lp/<path:path>", methods=["GET", "POST", "PUT", "DELETE"])
def api_lychee_asset(path):
    """Lychee 相册预览的同源代理: 静态资源直接转发, API 带匿名会话转发。

    仅允许本服务 /api/frame 预览过 Lychee 相册页的浏览器(lp_host cookie)
    访问对应主机; 其余请求一律拒绝, 避免变成内网开放代理。
    """
    netloc, _, rest = path.partition("/")
    if not re.match(r"^[A-Za-z0-9.\-]+(:\d+)?$", netloc):
        return jsonify(ok=False, error="无效的资源地址"), 400
    # 相册页面(首次进入, 响应里会种下 lp_host cookie)不要求 cookie;
    # 资源与 API 必须是已授权页面里发起的请求
    is_html_page = rest == "" or rest.startswith("gallery/")
    if not is_html_page and request.cookies.get("lp_host") != netloc:
        return jsonify(ok=False, error="未授权的 Lychee 资源"), 403
    url = f"http://{netloc}/" + rest
    if request.query_string:
        url += ("&" if "?" in url else "?") + request.query_string.decode("utf-8", "replace")
    if rest.startswith("api/") or rest.startswith("/api/"):
        try:
            resp = _lychee_api_forward(
                request.method,
                url,
                request.get_data() or None,
                (request.headers.get("Content-Type") or "application/json").split(";")[0],
            )
        except requests.RequestException as exc:
            return jsonify(ok=False, error=f"转发失败: {exc.__class__.__name__}"), 502
        ctype = resp.headers.get("Content-Type", "application/json").split(";")[0]
        out = Response(resp.content, status=resp.status_code, mimetype=ctype or "application/json")
        out.headers["Cache-Control"] = "no-store"
        return out
    try:
        data, content_type, final_url = _download(url, allow_private=True)
    except FetchError as exc:
        return jsonify(ok=False, error=str(exc)), 502
    ctype = (content_type or "").split(";")[0].strip().lower()
    if ctype == "text/html":
        # 相册页/子页面: 按预览页处理(改写同源地址 + 注入拦截脚本)
        return _lychee_page_response(
            final_url, _lychee_page_html(final_url, data.decode("utf-8", errors="replace"))
        )
    return Response(data, mimetype=content_type or "application/octet-stream")


@app.get("/api/proxy")
def api_proxy():
    """代理图片字节，解决防盗链/CORS/页面混用导致的预览失败。"""
    url = request.args.get("url", "")
    ref = request.args.get("ref", "")
    if not url or urlsplit(url).scheme not in ("http", "https"):
        return jsonify(ok=False, error="缺少或无效的 url"), 400
    allow_private = request.args.get("allow_private") in ("1", "true", "True")
    cached = _cache_get(url)
    if cached:
        data, content_type = cached
    else:
        try:
            data, content_type, _ = _download(url, referer=ref or None, allow_private=allow_private)
        except FetchError as exc:
            return jsonify(ok=False, error=str(exc)), 502
        _cache_put(url, data, content_type)
    resp = Response(data, mimetype=content_type or "application/octet-stream")
    resp.headers["Cache-Control"] = "private, max-age=3600"
    return resp


@app.get("/api/proxy_stream")
def api_proxy_stream():
    """流式代理视频字节，支持 Range，供 <video> 预览(不缓存、不整包进内存)。"""
    url = request.args.get("url", "")
    ref = request.args.get("ref", "")
    if not url or urlsplit(url).scheme not in ("http", "https"):
        return jsonify(ok=False, error="缺少或无效的 url"), 400
    allow_private = request.args.get("allow_private") in ("1", "true", "True")
    headers = {"User-Agent": UA, "Accept": "*/*", "Accept-Language": "zh-CN,zh;q=0.9"}
    if ref:
        headers["Referer"] = ref
    range_header = request.headers.get("Range")
    if range_header:
        headers["Range"] = range_header
    try:
        if BLOCK_PRIVATE_NETWORKS and not allow_private:
            _assert_public(url)
        upstream = requests.get(
            url, headers=headers, stream=True, timeout=(8, None), allow_redirects=True
        )
    except FetchError as exc:
        return jsonify(ok=False, error=str(exc)), 502
    except requests.RequestException as exc:
        return jsonify(ok=False, error=f"网络请求失败: {exc.__class__.__name__}"), 502
    if upstream.status_code not in (200, 206):
        upstream.close()
        return jsonify(ok=False, error=f"目标返回 HTTP {upstream.status_code}"), 502
    if BLOCK_PRIVATE_NETWORKS and not allow_private:
        try:
            _assert_public(upstream.url)
        except FetchError as exc:
            upstream.close()
            return jsonify(ok=False, error=str(exc)), 502

    def generate():
        try:
            for chunk in upstream.iter_content(64 * 1024):
                yield chunk
        finally:
            upstream.close()

    content_type = (
        upstream.headers.get("Content-Type", "").split(";")[0].strip().lower()
        or "application/octet-stream"
    )
    resp = Response(generate(), status=upstream.status_code, mimetype=content_type)
    for header in ("Content-Range", "Accept-Ranges", "Content-Length", "Content-Disposition"):
        if upstream.headers.get(header):
            resp.headers[header] = upstream.headers[header]
    resp.headers["Cache-Control"] = "private, max-age=600"
    return resp


@app.post("/api/albums")
def api_albums():
    body = request.get_json(silent=True) or {}
    base = (body.get("lychee_url") or LYCHEE_URL).rstrip("/")
    token = (body.get("lychee_token") or "").strip() or LYCHEE_TOKEN
    if not base or not token:
        return jsonify(ok=False, error="请先填写 Lychee 地址和 API Token"), 400
    try:
        albums = lychee_albums(base, token)
    except FetchError as exc:
        return jsonify(ok=False, error=str(exc)), 502
    except requests.RequestException as exc:
        return jsonify(ok=False, error=f"无法连接 Lychee: {exc.__class__.__name__}"), 502
    return jsonify(ok=True, albums=albums)


@app.post("/api/upload")
def api_upload():
    body = request.get_json(silent=True) or {}
    url = (body.get("url") or "").strip()
    album_id = str(body.get("album_id") or "").strip()
    title = (body.get("title") or "").strip()
    ref = body.get("ref") or ""
    kind = (body.get("type") or "image").strip().lower()
    base = (body.get("lychee_url") or LYCHEE_URL).rstrip("/")
    token = (body.get("lychee_token") or "").strip() or LYCHEE_TOKEN
    allow_private = bool(body.get("allow_private"))
    convert_webp = bool(body.get("convert_webp"))
    if not (url and album_id and base and token):
        return jsonify(ok=False, error="缺少参数(url/album_id/lychee 配置)"), 400

    result = _transfer_one(
        {"url": url, "title": title, "ref": ref, "type": kind},
        album_id,
        base,
        token,
        allow_private,
        convert_webp=convert_webp,
    )
    if not result.get("ok"):
        status = 400 if result.get("error", "").startswith("下载内容不是") else 502
        return jsonify(ok=False, error=result.get("error", "转存失败")), status
    return jsonify(result)


@app.post("/api/album/clear")
def api_album_clear():
    """清空指定相册内的全部照片(相册本身保留)。Lychee v4 专用。"""
    body = request.get_json(silent=True) or {}
    base = (body.get("lychee_url") or LYCHEE_URL).rstrip("/")
    token = (body.get("lychee_token") or "").strip() or LYCHEE_TOKEN
    album_id = str(body.get("album_id") or "").strip()
    if not (base and token and album_id):
        return jsonify(ok=False, error="缺少参数(lychee 配置/album_id)"), 400
    try:
        photos = lychee_album_photos(base, token, album_id)
        if not photos:
            return jsonify(ok=False, error="该相册没有照片或无法访问"), 404
        lychee_delete_photos(base, token, photos)
    except FetchError as exc:
        return jsonify(ok=False, error=str(exc)), 502
    except requests.RequestException as exc:
        return jsonify(ok=False, error=f"无法连接 Lychee: {exc.__class__.__name__}"), 502
    return jsonify(ok=True, deleted=len(photos))


def _batch_cleanup() -> None:
    """删除已过期(保留期结束)的任务。"""
    now = _time.time()
    with _batch_lock:
        expired = [tid for tid, t in _batches.items() if now > t.get("expires_at", 0)]
        for tid in expired:
            _batches.pop(tid, None)


def _batch_worker(task: dict) -> None:
    """后台线程：图片/视频分池并发转存，更新任务进度。"""
    items = task["items"]
    album_id = task["album_id"]
    base = task["base"]
    token = task["token"]
    allow_private = task["allow_private"]
    results = task["results"]

    def run_one(i: int) -> None:
        try:
            res = _transfer_one(
                items[i],
                album_id,
                base,
                token,
                allow_private,
                convert_webp=task.get("convert_webp", False),
            )
            ok = bool(res.get("ok"))
            error = res.get("error", "")
        except Exception as exc:  # noqa: BLE001 防止单条异常拖死整个任务
            ok = False
            error = f"内部错误: {exc}"
        results[i] = {"done": True, "ok": ok, "error": error}
        with _batch_lock:
            task["done"] += 1
            if ok:
                task["ok_count"] += 1
            else:
                task["fail_count"] += 1

    image_idx = [i for i, it in enumerate(items) if it.get("type") != "video"]
    video_idx = [i for i, it in enumerate(items) if it.get("type") == "video"]

    with ThreadPoolExecutor(max_workers=max(1, BATCH_CONCURRENCY_IMAGE)) as img_pool, \
         ThreadPoolExecutor(max_workers=max(1, BATCH_CONCURRENCY_VIDEO)) as vid_pool:
        futures = [img_pool.submit(run_one, i) for i in image_idx]
        futures += [vid_pool.submit(run_one, i) for i in video_idx]
        for fut in futures:
            fut.result()

    with _batch_lock:
        task["status"] = "done"
        task["finished_at"] = _time.time()
        task["expires_at"] = task["finished_at"] + _BATCH_TTL


@app.post("/api/batch/create")
def api_batch_create():
    body = request.get_json(silent=True) or {}
    raw_items = body.get("items")
    album_id = str(body.get("album_id") or "").strip()
    base = (body.get("lychee_url") or LYCHEE_URL).rstrip("/")
    token = (body.get("lychee_token") or "").strip() or LYCHEE_TOKEN
    allow_private = bool(body.get("allow_private"))
    convert_webp = bool(body.get("convert_webp"))
    if not isinstance(raw_items, list) or not raw_items:
        return jsonify(ok=False, error="缺少 items 列表"), 400
    if not (album_id and base and token):
        return jsonify(ok=False, error="缺少参数(album_id/lychee 配置)"), 400

    items: list[dict] = []
    for it in raw_items[: _BATCH_MAX_ITEMS]:
        if not isinstance(it, dict):
            continue
        url = (it.get("url") or "").strip()
        # 只接受 http(s) 直链，过滤空值/非法协议/data: 等
        if not url.startswith(("http://", "https://")):
            continue
        items.append(
            {
                "url": url,
                "title": (it.get("title") or "").strip(),
                "ref": it.get("ref") or "",
                "type": (it.get("type") or "image").strip().lower() or "image",
            }
        )
    if not items:
        return jsonify(ok=False, error="没有有效的媒体链接(仅支持 http/https)"), 400

    _batch_cleanup()
    task_id = secrets.token_urlsafe(16)
    results = [{"done": False, "ok": False, "error": ""} for _ in items]
    task = {
        "id": task_id,
        "status": "running",
        "total": len(items),
        "done": 0,
        "ok_count": 0,
        "fail_count": 0,
        "results": results,
        "items": items,
        "album_id": album_id,
        "base": base,
        "token": token,
        "allow_private": allow_private,
        "convert_webp": convert_webp,
        "created_at": _time.time(),
        "finished_at": None,
        "expires_at": _time.time() + _BATCH_TTL,
    }
    with _batch_lock:
        _batches[task_id] = task
    threading.Thread(target=_batch_worker, args=(task,), daemon=True).start()
    return jsonify(ok=True, task_id=task_id, total=len(items))


@app.post("/api/batch/status")
def api_batch_status():
    body = request.get_json(silent=True) or {}
    task_id = str(body.get("task_id") or "").strip()
    if not _BATCH_ID_RE.match(task_id or ""):
        return jsonify(ok=False, error="任务 ID 无效"), 400
    _batch_cleanup()
    with _batch_lock:
        task = _batches.get(task_id)
    if task is None:
        return jsonify(ok=False, error="任务不存在或已过期"), 404
    return jsonify(
        ok=True,
        task={
            "id": task["id"],
            "status": task["status"],
            "total": task["total"],
            "done": task["done"],
            "ok_count": task["ok_count"],
            "fail_count": task["fail_count"],
            "results": task["results"],
            "created_at": task["created_at"],
            "finished_at": task["finished_at"],
        },
    )


# ---------------------------------------------------------------- 书签
def _load_bookmarks() -> list:
    """从文件读取书签列表, 文件缺失/损坏时返回空列表。"""
    try:
        with open(BOOKMARK_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    out = []
    for item in data:
        if isinstance(item, dict):
            name = str(item.get("name") or "").strip()
            url = str(item.get("url") or "").strip()
            if name and url:
                out.append({"name": name, "url": url})
    return out


def _save_bookmarks(bookmarks: list) -> None:
    """原子写书签文件: 先写临时文件再替换, 避免中途断电损坏。"""
    os.makedirs(os.path.dirname(BOOKMARK_FILE) or ".", exist_ok=True)
    tmp = BOOKMARK_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(bookmarks, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, BOOKMARK_FILE)


@app.get("/api/bookmarks")
def api_bookmarks_get():
    """读取服务器端共享书签。"""
    with _bookmarks_lock:
        bookmarks = _load_bookmarks()
    return jsonify(ok=True, bookmarks=bookmarks)


@app.put("/api/bookmarks")
def api_bookmarks_put():
    """整体保存书签列表(任意设备编辑后提交, 全设备可见)。"""
    body = request.get_json(silent=True) or {}
    raw = body.get("bookmarks")
    if not isinstance(raw, list):
        return jsonify(ok=False, error="bookmarks 必须是数组"), 400
    bookmarks = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        url = str(item.get("url") or "").strip()
        if not name or not url:
            continue
        if len(bookmarks) >= _BOOKMARK_MAX:
            break
        bookmarks.append({"name": name, "url": url})
    with _bookmarks_lock:
        _save_bookmarks(bookmarks)
    return jsonify(ok=True, count=len(bookmarks))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT, debug=False)
