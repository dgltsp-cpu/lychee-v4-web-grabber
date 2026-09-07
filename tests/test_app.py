import io
import json
import re
import pathlib
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote, urlsplit

import pytest
from unittest.mock import Mock
from PIL import Image

LAST_ALBUM_AUTH = {"value": None}  # 记录最近一次 /api/Album::get 的 Authorization 头


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import app as grabber  # noqa: E402


def make_png(width=32, height=32):
    buf = io.BytesIO()
    Image.new("RGB", (width, height), (200, 60, 60)).save(buf, "PNG")
    return buf.getvalue()


PNG = make_png()


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = urlsplit(self.path).path
        if path == "/page.html":
            html = b"""
            <html><head>
              <meta property="og:image" content="/og.jpg">
              <base href="/sub/">
            </head>
            <body>
              <img src="/a.png" alt="alpha">
              <img src="b.png">
              <img data-src="/lazy.png">
              <img srcset="/small.png 400w, /large.png 1200w">
              <img src="https://cdn.example.com/remote.webp">
              <picture><source srcset="/pic1.png 800w" media="(min-width: 600px)">
                <img src="/pic2.png"></picture>
              <div style="background-image: url('/bg.png')"></div>
              <img src="data:image/png;base64,AAAA">
              <video src="/clip.mp4" poster="/poster.jpg"></video>
              <video><source src="/movie.webm" type="video/webm"></video>
              <a href="/download.mp4">download video</a>
            </body></html>
            """
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(html)))
            self.end_headers()
            self.wfile.write(html)
        elif path.endswith((".png", ".jpg", ".webp")):
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(PNG)))
            self.end_headers()
            self.wfile.write(PNG)
        elif path.endswith((".mp4", ".webm")):
            body = b"FAKE-VIDEO-BYTES"
            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path in ("/", "/index.html") or (path.startswith("/gallery/") and path.count("/") == 2):
            html = b"<html><head></head><body>album shell</body></html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(html)))
            self.end_headers()
            self.wfile.write(html)
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        path = urlsplit(self.path).path
        if path == "/api/Album::get":
            LAST_ALBUM_AUTH["value"] = self.headers.get("Authorization")
            length = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                body = {}
            album_id = body.get("albumID") or "AbC1234567890XyZ"
            if album_id == "Fork1234567890XyZ":
                payload = {
                    "id": album_id,
                    "title": "fork 顶层相册",
                    "has_albums": "1",
                    "albums": [{"id": "Sub1234567890XyZ", "title": "子相册"}],
                    "photos": [],
                }
            elif album_id == "Sub1234567890XyZ":
                payload = {
                    "id": album_id,
                    "title": "子相册",
                    "photos": [
                        {
                            "id": "F1",
                            "title": "VID_01",
                            "type": "video/mp4",
                            "url": "uploads/big/aaa.MOV",
                            "sizeVariants": {"thumb": {"url": "uploads/thumb/aaa.jpeg"}},
                        },
                        {
                            "id": "F2",
                            "title": "IMG_01",
                            "type": "image/jpeg",
                            "url": "uploads/big/bbb.jpg",
                        },
                    ],
                }
            else:
                payload = {
                "id": album_id,
                "title": "v4 album",
                "is_public": True,
                "photos": [
                    {
                        "id": "Pv1",
                        "title": "IMG_1789",
                        "type": "video/quicktime",
                        "size_variants": {
                            "original": {"url": "uploads/original/xx/abc.MOV"},
                            "small": {"url": "uploads/small/xx/abc.jpeg"},
                        },
                    },
                    {
                        "id": "Pv2",
                        "title": "photo",
                        "type": "image/jpeg",
                        "size_variants": {
                            "original": {"url": "uploads/original/xx/pic.jpg"},
                        },
                    },
                ],
            }
            data = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *args):
        pass


@pytest.fixture(scope="module", autouse=True)
def site():
    grabber.BLOCK_PRIVATE_NETWORKS = False
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


@pytest.fixture()
def client():
    grabber.app.config["TESTING"] = True
    return grabber.app.test_client()


def test_extract_images_from_html(site):
    html, _, final = grabber._download(f"{site}/page.html", max_bytes=1024 * 1024)
    items = grabber.extract_images(final, html)
    urls = [item.url for item in items]
    assert f"{site}/og.jpg" in urls                       # og:image
    assert f"{site}/a.png" in urls                        # 普通 img
    assert f"{site}/sub/b.png" in urls                    # base href + 相对路径
    assert f"{site}/lazy.png" in urls                     # data-src 懒加载
    assert f"{site}/large.png" in urls                    # srcset 取最高清
    assert f"{site}/small.png" not in urls                # srcset 低清不重复收录
    assert "https://cdn.example.com/remote.webp" in urls  # 外链图片
    assert f"{site}/pic1.png" in urls                     # picture source
    assert f"{site}/bg.png" in urls                       # css background
    assert f"{site}/clip.mp4" not in urls                 # 视频不混入图片
    assert len(urls) == len(set(urls))                    # 去重
    assert all(item.ref == f"{site}/page.html" for item in items)


def test_extract_videos_from_html(site):
    html, _, final = grabber._download(f"{site}/page.html", max_bytes=1024 * 1024)
    items = grabber.extract_videos(final, html)
    urls = [item.url for item in items]
    assert f"{site}/clip.mp4" in urls                     # video src
    assert f"{site}/movie.webm" in urls                   # video source
    assert f"{site}/download.mp4" in urls                 # a[href] 视频链接
    assert f"{site}/poster.jpg" not in urls               # poster 不算视频
    assert f"{site}/a.png" not in urls                    # 图片不混入视频


def test_flatten_albums():
    tree = {"albums": [{"id": "1", "title": "旅行", "children": [{"id": "2", "title": "2026"}]}],
            "shared": [{"id": "3", "title": "共享"}],
            "smart": [{"id": "starred", "title": "收藏"}]}
    result = grabber.flatten_albums(tree)
    assert result == [{"id": "1", "title": "旅行"}, {"id": "2", "title": "2026"},
                      {"id": "3", "title": "共享"}]
    result_list = grabber.flatten_albums([{"id": "9", "title": "数组形式"}])
    assert result_list == [{"id": "9", "title": "数组形式"}]


def test_lychee_albums_v4_nested_and_shared(monkeypatch):
    # v4: POST /api/Albums::get 一次返回完整嵌套树(albums + shared_albums)
    calls = []

    def fake_post(url, json=None, headers=None, timeout=None):
        calls.append(urlsplit(url).path)
        payload = {
            "albums": [
                {
                    "id": "1",
                    "title": "旅行",
                    "albums": [
                        {"id": "2", "title": "2026", "albums": [{"id": "4", "title": "北京"}]}
                    ],
                },
                {"id": "3", "title": "杂图"},
            ],
            "shared_albums": [{"id": "7", "title": "分享相册"}],
        }
        resp = Mock()
        resp.status_code = 200
        resp.headers = {"Content-Type": "application/json"}
        resp.json.return_value = payload
        return resp

    monkeypatch.setattr(grabber.requests, "post", fake_post)
    albums = grabber.lychee_albums("http://lychee", "token")
    assert calls == ["/api/Albums::get"]
    assert albums == [
        {"id": "1", "title": "旅行", "path": "旅行"},
        {"id": "2", "title": "2026", "path": "旅行 / 2026"},
        {"id": "4", "title": "北京", "path": "旅行 / 2026 / 北京"},
        {"id": "3", "title": "杂图", "path": "杂图"},
        {"id": "7", "title": "分享相册", "path": "分享相册"},
    ]


def test_lychee_albums_v4_dedup_and_deep_limit(monkeypatch):
    # 同一相册出现多次(如同时出现在 albums 与 shared_albums)只保留一次
    def fake_post_dup(url, json=None, headers=None, timeout=None):
        payload = {
            "albums": [{"id": "9", "title": "重复"}],
            "shared_albums": [{"id": "9", "title": "重复"}],
        }
        resp = Mock()
        resp.status_code = 200
        resp.headers = {"Content-Type": "application/json"}
        resp.json.return_value = payload
        return resp

    monkeypatch.setattr(grabber.requests, "post", fake_post_dup)
    assert grabber.lychee_albums("http://lychee", "token") == [
        {"id": "9", "title": "重复", "path": "重复"}
    ]

    # 嵌套深度超过 20 层时停止, 避免环状数据死循环
    def fake_post_deep(url, json=None, headers=None, timeout=None):
        node = {"id": "leaf", "title": "叶子"}
        for i in range(30):
            node = {"id": f"n{i}", "title": f"层{i}", "albums": [node]}
        resp = Mock()
        resp.status_code = 200
        resp.headers = {"Content-Type": "application/json"}
        resp.json.return_value = {"albums": [node]}
        return resp

    monkeypatch.setattr(grabber.requests, "post", fake_post_deep)
    albums = grabber.lychee_albums("http://lychee", "token")
    # depth 0..20 共 21 层被收录, 更深的被截断
    assert len(albums) == 21
    assert albums[0]["id"] == "n29"
    assert albums[20]["id"] == "n9"



def test_extract_lychee_v4_album(site):
    # v7 接口在 fixture 里不存在(404), 应回退到 v4 的 POST /api/Album::get
    images, videos = grabber._extract_lychee_album(f"{site}/gallery/AbC1234567890XyZ")
    assert videos and any(v.url.endswith(".MOV") for v in videos)
    assert images and any(i.url.endswith("pic.jpg") for i in images)
    for item in videos + images:
        assert item.url.startswith(site)  # 相对路径已补全为绝对地址


def test_extract_lychee_album_uses_api_token(site):
    # 带 token 时, Album::get 应携带 Authorization 头(私有相册/子相册必须)
    LAST_ALBUM_AUTH["value"] = None
    images, videos = grabber._extract_lychee_album(
        f"{site}/gallery/AbC1234567890XyZ", token="MY-TOKEN-123"
    )
    assert LAST_ALBUM_AUTH["value"] == "MY-TOKEN-123"
    assert images and videos


def test_extract_lychee_v4_hash_album(site):
    # Lychee v4 的相册 ID 在 hash 里: /#{albumID}
    images, videos = grabber._extract_lychee_album(f"{site}/#AbC1234567890XyZ")
    assert videos and any(v.url.endswith(".MOV") for v in videos)
    assert images and any(i.url.endswith("pic.jpg") for i in images)


def test_extract_lychee_fork_nested_albums(site):
    # 魔改版: 顶层 url + 驼峰 sizeVariants + 嵌套子相册, 应递归提取
    images, videos = grabber._extract_lychee_album(f"{site}/#Fork1234567890XyZ")
    assert videos and any(v.url.endswith(".MOV") for v in videos)
    assert images and any(i.url.endswith("bbb.jpg") for i in images)
    for item in videos + images:
        assert item.url.startswith(site)


def test_media_urls_from_text_sniff():
    text = (
        '{"photos":[{"url":"uploads/big/a.MOV"},'
        '{"url":"/b/c.jpg"},'
        '{"url":"https://cdn.example.com/x.webp?v=2"},'
        '{"url":"data:image/png;base64,AAAA"}]}'
    )
    items = grabber._media_urls_from_text(text, "http://example.com/", 10)
    urls = [i.url for i in items]
    assert "http://example.com/uploads/big/a.MOV" in urls
    assert "http://example.com/b/c.jpg" in urls
    assert "https://cdn.example.com/x.webp?v=2" in urls
    assert not any("data:" in u for u in urls)  # data: 直链被排除


def test_media_urls_from_text_unescapes_and_filters_garbage():
    text = (
        '{"videos":[{"url":"https://cdn.example.com/uploads\\/tenants\\/1\\/a_mobile.mp4"},'
        '"https://cdn.example.com/i=h*e,j=this.modules[f][h];j&&(d.beginFill(0,100),d.mov",'
        '"https://cdn.example.com/uploads/plain.webm?v=1"]}'
    )
    items = grabber._media_urls_from_text(text, "http://example.com/", 10)
    urls = [i.url for i in items]
    assert "https://cdn.example.com/uploads/tenants/1/a_mobile.mp4" in urls  # \/ 已还原
    assert "https://cdn.example.com/uploads/plain.webm?v=1" in urls
    assert not any("\\" in u for u in urls)
    assert not any("i=h*e" in u for u in urls)  # JS 代码碎片被过滤


def test_proxy_endpoint(client, site):
    resp = client.get(f"/api/proxy?url={quote(site + '/a.png', safe='/')}")
    assert resp.status_code == 200
    assert resp.mimetype == "image/png"
    assert resp.data == PNG


def test_frame_blocks_private_non_lychee(client, site, monkeypatch):
    monkeypatch.setattr(grabber, "BLOCK_PRIVATE_NETWORKS", True)
    resp = client.get(f"/api/frame?url={quote(site + '/page.html', safe='/')}")
    assert resp.status_code == 502
    assert "已阻止内网地址" in resp.get_json()["error"]


def test_frame_allows_private_lychee_album(client, site, monkeypatch):
    monkeypatch.setattr(grabber, "BLOCK_PRIVATE_NETWORKS", True)
    url = f"{site}/gallery/AbC1234567890XyZ"
    resp = client.get(f"/api/frame?url={quote(url, safe='/')}")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "/api/passthrough" in html
    assert "lp_host" in (resp.headers.get("Set-Cookie") or "")


def test_passthrough_blocks_non_api_private(client, site, monkeypatch):
    monkeypatch.setattr(grabber, "BLOCK_PRIVATE_NETWORKS", True)
    resp = client.get(f"/api/passthrough?url={quote(site + '/secret', safe='/')}")
    assert resp.status_code == 502
    assert "已阻止内网地址" in resp.get_json()["error"]


def test_passthrough_forwards_lychee_api(client, site, monkeypatch):
    monkeypatch.setattr(grabber, "BLOCK_PRIVATE_NETWORKS", True)
    resp = client.get(f"/api/passthrough?url={quote(site + '/api/v2/Albums', safe='/')}")
    assert resp.status_code == 404  # 放行并被转发(fixture 无该路由返回 404)


def test_lychee_asset_requires_cookie(client, site, monkeypatch):
    monkeypatch.setattr(grabber, "BLOCK_PRIVATE_NETWORKS", True)
    resp = client.get("/lp/127.0.0.1:1/build/app.js")
    assert resp.status_code == 403


def test_lychee_asset_proxies_with_cookie(client, site, monkeypatch):
    monkeypatch.setattr(grabber, "BLOCK_PRIVATE_NETWORKS", True)
    host = urlsplit(site).netloc
    resp = client.get(f"/lp/{host}/build/app.js")
    assert resp.status_code == 403  # 未授权
    client.set_cookie("lp_host", host)
    resp = client.get(f"/lp/{host}/build/app.js")
    assert resp.status_code == 502  # 已授权, 转发到 fixture(无该路由返回 404 -> FetchError)


def test_lychee_page_served_via_lp(client, site, monkeypatch):
    monkeypatch.setattr(grabber, "BLOCK_PRIVATE_NETWORKS", True)
    host = urlsplit(site).netloc
    resp = client.get(f"/lp/{host}/gallery/AbC1234567890XyZ")  # 首次无 cookie 也允许
    assert resp.status_code == 200
    client.set_cookie("lp_host", host)
    resp = client.get(f"/lp/{host}/gallery/AbC1234567890XyZ")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "/api/passthrough" in html
    assert "lp_host" in (resp.headers.get("Set-Cookie") or "")


def test_proxy_rejects_non_http(client):
    resp = client.get("/api/proxy?url=file:///etc/passwd")
    assert resp.status_code == 400


def test_extract_route(client, site):
    resp = client.post("/api/extract", json={"url": f"{site}/page.html"})
    data = resp.get_json()
    assert resp.status_code == 200
    assert data["ok"] is True
    assert data["count"] > 8
    assert len(data["videos"]) >= 3


def test_proxy_stream_endpoint(client, site):
    resp = client.get(f"/api/proxy_stream?url={quote(site + '/clip.mp4', safe='/')}")
    assert resp.status_code == 200
    assert resp.mimetype == "video/mp4"
    assert resp.data == b"FAKE-VIDEO-BYTES"


def test_albums_route(client, monkeypatch):
    monkeypatch.setattr(
        grabber, "lychee_albums",
        lambda base, token: [{"id": "3", "title": "测试相册"}],
    )
    resp = client.post("/api/albums", json={"lychee_url": "http://lychee", "lychee_token": "t"})
    data = resp.get_json()
    assert resp.status_code == 200
    assert data["albums"][0]["title"] == "测试相册"


def test_upload_route(client, monkeypatch):
    monkeypatch.setattr(grabber, "_download", lambda url, referer=None, max_bytes=0, **kw: (PNG, "image/png", url))
    monkeypatch.setattr(grabber, "_probe", lambda data: ((640, 480), "png"))
    monkeypatch.setattr(grabber, "lychee_upload", lambda *a, **k: "77")
    resp = client.post(
        "/api/upload",
        json={"url": "https://example.com/img.jpg", "album_id": "3",
              "lychee_url": "http://lychee", "lychee_token": "t", "title": "标题"},
    )
    data = resp.get_json()
    assert resp.status_code == 200
    assert data["ok"] is True
    assert data["photo_id"] == "77"


def test_upload_rejects_non_image(client, monkeypatch):
    monkeypatch.setattr(grabber, "_download", lambda url, referer=None, max_bytes=0, **kw: (b"not an image", "text/html", url))
    monkeypatch.setattr(grabber, "_probe", lambda data: None)
    resp = client.post(
        "/api/upload",
        json={"url": "https://example.com/x", "album_id": "3",
              "lychee_url": "http://lychee", "lychee_token": "t"},
    )
    assert resp.status_code == 400
    assert resp.get_json()["ok"] is False


def test_upload_video_route(client, monkeypatch):
    monkeypatch.setattr(
        grabber,
        "_download",
        lambda url, referer=None, max_bytes=0, **kw: (b"FAKE-VIDEO", "video/mp4", url),
    )
    monkeypatch.setattr(grabber, "lychee_upload", lambda *a, **k: "99")
    resp = client.post(
        "/api/upload",
        json={"url": "https://example.com/clip.mp4", "album_id": "3",
              "type": "video", "lychee_url": "http://lychee", "lychee_token": "t"},
    )
    data = resp.get_json()
    assert resp.status_code == 200
    assert data["ok"] is True
    assert data["photo_id"] == "99"
    assert data["type"] == "video"


def test_upload_rejects_non_video(client, monkeypatch):
    monkeypatch.setattr(
        grabber,
        "_download",
        lambda url, referer=None, max_bytes=0, **kw: (b"<html>not video</html>", "text/html", url),
    )
    resp = client.post(
        "/api/upload",
        json={"url": "https://example.com/embed", "album_id": "3",
              "type": "video", "lychee_url": "http://lychee", "lychee_token": "t"},
    )
    assert resp.status_code == 400
    assert resp.get_json()["ok"] is False


# ---------------------------------------------------------------- 本地文件上传
def _post_local(client, file_tuple, form=None):
    data = {"album_id": "3", "lychee_url": "http://lychee", "lychee_token": "t"}
    data.update(form or {})
    data["file"] = file_tuple
    return client.post("/api/upload_local", data=data, content_type="multipart/form-data")


def test_upload_local_image(client, monkeypatch):
    seen = {}

    def fake_upload(base, token, album_id, data, filename, content_type, title=""):
        seen.update(album_id=album_id, data=data, filename=filename, content_type=content_type)
        return "77"

    monkeypatch.setattr(grabber, "lychee_upload", fake_upload)
    resp = _post_local(client, (io.BytesIO(PNG), "照片.png"))
    data = resp.get_json()
    assert resp.status_code == 200
    assert data["ok"] is True and data["photo_id"] == "77" and data["type"] == "image"
    assert seen["album_id"] == "3"
    assert seen["data"] == PNG
    assert seen["filename"] == "照片.png"
    assert seen["content_type"].startswith("image/")


def test_upload_local_image_converts_webp(client, monkeypatch):
    seen = {}
    monkeypatch.setattr(
        grabber,
        "lychee_upload",
        lambda *a, **k: seen.update(data=a[3], filename=a[4], content_type=a[5]) or "78",
    )
    resp = _post_local(client, (io.BytesIO(PNG), "a.png"), {"convert_webp": "1"})
    data = resp.get_json()
    assert data["ok"] is True and data["webp"] is True
    assert seen["filename"] == "a.webp"
    assert seen["content_type"] == "image/webp"
    assert grabber._probe(seen["data"])[1] == "webp"


def test_upload_local_video_passthrough(client, monkeypatch):
    """iPhone 拍的 .mov 是 Lychee v4 原生支持的容器, 直接转发不改名不转码。"""
    seen = {}
    monkeypatch.setattr(
        grabber,
        "lychee_upload",
        lambda *a, **k: seen.update(data=a[3], filename=a[4], content_type=a[5]) or "99",
    )
    resp = _post_local(client, (io.BytesIO(b"FAKE-MOV-DATA"), "IMG_0001.MOV"))
    data = resp.get_json()
    assert resp.status_code == 200
    assert data["ok"] is True and data["type"] == "video" and data["photo_id"] == "99"
    assert seen["filename"] == "IMG_0001.MOV"
    assert seen["content_type"] == "video/quicktime"
    assert not isinstance(seen["data"], bytes), "视频应流式转发文件对象, 不整段读进内存"


def test_upload_local_video_transcodes_unsupported_ext(client, monkeypatch):
    monkeypatch.setattr(grabber, "_transcode_local_video", lambda stored, ext: (b"MP4-BYTES", ""))
    monkeypatch.setattr(grabber, "lychee_upload", lambda *a, **k: "100")
    resp = _post_local(client, (io.BytesIO(b"matroska-head"), "clip.mkv"))
    data = resp.get_json()
    assert data["ok"] is True and data["type"] == "video"
    assert data["name"] == "clip.mp4" and data["transcoded"] is True


def test_upload_local_rejects_non_media(client, monkeypatch):
    monkeypatch.setattr(grabber, "lychee_upload", lambda *a, **k: "nope")
    resp = _post_local(client, (io.BytesIO(b"just some text"), "notes.txt"))
    assert resp.status_code == 400
    assert resp.get_json()["ok"] is False


def test_upload_local_rejects_fake_image(client, monkeypatch):
    monkeypatch.setattr(grabber, "lychee_upload", lambda *a, **k: "nope")
    resp = _post_local(client, (io.BytesIO(b"not an image"), "pic.jpg"))
    assert resp.status_code == 400
    assert "图片" in resp.get_json()["error"]


def test_upload_local_rejects_oversize(client, monkeypatch):
    monkeypatch.setattr(grabber, "MAX_LOCAL_IMAGE_BYTES", 16)
    monkeypatch.setattr(grabber, "lychee_upload", lambda *a, **k: "nope")
    resp = _post_local(client, (io.BytesIO(PNG), "big.png"))
    assert resp.status_code == 413
    assert resp.get_json()["ok"] is False


def test_upload_local_requires_album_and_config(client):
    resp = client.post(
        "/api/upload_local",
        data={"file": (io.BytesIO(PNG), "a.png")},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 400
    assert "album_id" in resp.get_json()["error"]


# ---------------------------------------------------------------- 后台转存
def _wait_batch(client, task_id, timeout=10):
    import time as _t
    deadline = _t.time() + timeout
    while _t.time() < deadline:
        resp = client.post("/api/batch/status", json={"task_id": task_id})
        data = resp.get_json()
        if data.get("ok") and data["task"]["status"] == "done":
            return data["task"]
        _t.sleep(0.05)
    return None


def test_batch_create_and_run(client, monkeypatch):
    """后台任务：create 后 worker 自动逐张转存，status 返回进度与结果。"""
    calls = []

    def fake_transfer(item, album_id, base, token, allow_private=False, convert_webp=False):
        calls.append(item["url"])
        return {"ok": True, "photo_id": "p1", "type": item.get("type", "image")}

    monkeypatch.setattr(grabber, "_transfer_one", fake_transfer)
    resp = client.post(
        "/api/batch/create",
        json={
            "items": [
                {"url": "https://example.com/1.jpg", "type": "image"},
                {"url": "https://example.com/2.mp4", "type": "video"},
                {"url": "https://example.com/3.png"},
            ],
            "album_id": "3",
            "lychee_url": "http://lychee",
            "lychee_token": "t",
        },
    )
    data = resp.get_json()
    assert resp.status_code == 200
    assert data["ok"] is True
    assert data["total"] == 3
    task = _wait_batch(client, data["task_id"])
    assert task is not None
    assert task["status"] == "done"
    assert task["done"] == 3
    assert task["ok_count"] == 3
    assert task["fail_count"] == 0
    assert all(r["ok"] for r in task["results"])
    assert len(calls) == 3


def test_batch_records_failures(client, monkeypatch):
    """后台任务：部分失败时记录 error，fail_count 正确。"""

    def fake_transfer(item, album_id, base, token, allow_private=False, convert_webp=False):
        if item["url"].endswith("bad.jpg"):
            return {"ok": False, "error": "下载失败: 404"}
        return {"ok": True, "photo_id": "x"}

    monkeypatch.setattr(grabber, "_transfer_one", fake_transfer)
    resp = client.post(
        "/api/batch/create",
        json={
            "items": [
                {"url": "https://example.com/good.jpg"},
                {"url": "https://example.com/bad.jpg"},
                {"url": "https://example.com/ok.png"},
            ],
            "album_id": "3",
            "lychee_url": "http://lychee",
            "lychee_token": "t",
        },
    )
    task = _wait_batch(client, resp.get_json()["task_id"])
    assert task is not None
    assert task["ok_count"] == 2
    assert task["fail_count"] == 1
    bad = [r for r in task["results"] if not r["ok"]]
    assert len(bad) == 1
    assert bad[0]["error"] == "下载失败: 404"


def test_batch_filters_invalid_links(client, monkeypatch):
    """后台任务：非 http(s)/空/非法链接被过滤，不进入执行列表。"""
    calls = []

    def fake_transfer(item, album_id, base, token, allow_private=False, convert_webp=False):
        calls.append(item["url"])
        return {"ok": True}

    monkeypatch.setattr(grabber, "_transfer_one", fake_transfer)
    resp = client.post(
        "/api/batch/create",
        json={
            "items": [
                {"url": "https://example.com/ok.jpg"},
                {"url": "ftp://example.com/x.jpg"},
                {"url": "data:image/png;base64,AAAA"},
                {"url": "file:///etc/passwd"},
                {"url": ""},
                {"url": "javascript:alert(1)"},
                "not-a-dict",
            ],
            "album_id": "3",
            "lychee_url": "http://lychee",
            "lychee_token": "t",
        },
    )
    data = resp.get_json()
    assert resp.status_code == 200
    assert data["total"] == 1
    task = _wait_batch(client, data["task_id"])
    assert task is not None
    assert task["done"] == 1
    assert task["ok_count"] == 1
    assert len(calls) == 1


def test_batch_concurrency_caps(client, monkeypatch):
    """后台任务：图片/视频分池，并发不超过各自上限。"""
    import time as _t

    monkeypatch.setattr(grabber, "BATCH_CONCURRENCY_IMAGE", 2)
    monkeypatch.setattr(grabber, "BATCH_CONCURRENCY_VIDEO", 1)
    state = {"img_active": 0, "vid_active": 0, "max_img": 0, "max_vid": 0}
    lock = threading.Lock()

    def fake_transfer(item, album_id, base, token, allow_private=False, convert_webp=False):
        is_video = item.get("type") == "video"
        with lock:
            if is_video:
                state["vid_active"] += 1
                state["max_vid"] = max(state["max_vid"], state["vid_active"])
            else:
                state["img_active"] += 1
                state["max_img"] = max(state["max_img"], state["img_active"])
        _t.sleep(0.05)
        with lock:
            if is_video:
                state["vid_active"] -= 1
            else:
                state["img_active"] -= 1
        return {"ok": True, "type": item.get("type", "image")}

    monkeypatch.setattr(grabber, "_transfer_one", fake_transfer)
    resp = client.post(
        "/api/batch/create",
        json={
            "items": [
                {"url": f"https://example.com/{i}.jpg", "type": "image"}
                for i in range(5)
            ]
            + [
                {"url": f"https://example.com/v{j}.mp4", "type": "video"}
                for j in range(3)
            ],
            "album_id": "3",
            "lychee_url": "http://lychee",
            "lychee_token": "t",
        },
    )
    task = _wait_batch(client, resp.get_json()["task_id"])
    assert task is not None
    assert task["status"] == "done"
    assert task["done"] == 8
    assert task["ok_count"] == 8
    assert state["max_img"] <= 2
    assert state["max_vid"] <= 1


def test_batch_status_expired(client):
    """任务过期/不存在时返回错误；非法 task_id 被拒绝。"""
    resp = client.post("/api/batch/status", json={"task_id": "nonexistent12345678"})
    assert resp.status_code == 404
    assert resp.get_json()["ok"] is False
    assert "不存在或已过期" in resp.get_json()["error"]

    resp = client.post("/api/batch/status", json={"task_id": "../evil"})
    assert resp.status_code == 400

    resp = client.post("/api/batch/create", json={"items": [], "album_id": "3",
                                                  "lychee_url": "http://lychee", "lychee_token": "t"})
    assert resp.status_code == 400


# ---------------------------------------------------------------- 转 WebP
def test_to_webp_converts_png():
    out = grabber._to_webp(PNG, "png")
    assert out is not None
    assert out.startswith(b"RIFF")
    with Image.open(io.BytesIO(out)) as im:
        assert (im.format or "").lower() == "webp"


def test_to_webp_skips_webp_gif_and_garbage():
    assert grabber._to_webp(PNG, "webp") is None          # 已是 WebP，跳过
    assert grabber._to_webp(PNG, "gif") is None           # GIF(可能多帧)，跳过
    assert grabber._to_webp(b"not an image", "png") is None  # 无法解码，回退原图


def test_upload_route_with_webp_conversion(client, monkeypatch):
    captured = {}

    def fake_download(url, referer=None, max_bytes=0, **kw):
        return (PNG, "image/png", url)

    def fake_probe(data):
        return ((640, 480), "png")

    def fake_upload(base, token, album_id, data, filename, content_type, title=""):
        captured.update(data=data, filename=filename, content_type=content_type)
        return "77"

    monkeypatch.setattr(grabber, "_download", fake_download)
    monkeypatch.setattr(grabber, "_probe", fake_probe)
    monkeypatch.setattr(grabber, "lychee_upload", fake_upload)
    resp = client.post(
        "/api/upload",
        json={"url": "https://example.com/img.jpg", "album_id": "3",
              "lychee_url": "http://lychee", "lychee_token": "t",
              "convert_webp": True},
    )
    assert resp.status_code == 200
    assert resp.get_json()["webp"] is True
    assert captured["content_type"] == "image/webp"
    assert captured["filename"].endswith(".webp")
    assert captured["data"].startswith(b"RIFF")


def test_upload_route_keeps_original_without_flag(client, monkeypatch):
    captured = {}

    def fake_download(url, referer=None, max_bytes=0, **kw):
        return (PNG, "image/png", url)

    def fake_probe(data):
        return ((640, 480), "png")

    def fake_upload(base, token, album_id, data, filename, content_type, title=""):
        captured.update(data=data, filename=filename, content_type=content_type)
        return "77"

    monkeypatch.setattr(grabber, "_download", fake_download)
    monkeypatch.setattr(grabber, "_probe", fake_probe)
    monkeypatch.setattr(grabber, "lychee_upload", fake_upload)
    resp = client.post(
        "/api/upload",
        json={"url": "https://example.com/img.jpg", "album_id": "3",
              "lychee_url": "http://lychee", "lychee_token": "t"},
    )
    assert resp.status_code == 200
    assert "webp" not in resp.get_json()
    assert captured["content_type"] == "image/png"
    assert captured["filename"].endswith(".jpg")


def test_batch_passes_convert_webp(client, monkeypatch):
    seen = []

    def fake_transfer(item, album_id, base, token, allow_private=False, convert_webp=False):
        seen.append(convert_webp)
        return {"ok": True}

    monkeypatch.setattr(grabber, "_transfer_one", fake_transfer)
    resp = client.post(
        "/api/batch/create",
        json={
            "items": [{"url": "https://example.com/1.jpg"}],
            "album_id": "3",
            "lychee_url": "http://lychee",
            "lychee_token": "t",
            "convert_webp": True,
        },
    )
    task = _wait_batch(client, resp.get_json()["task_id"])
    assert task is not None
    assert task["done"] == 1
    assert seen == [True]


# ---------------------------------------------------------------- 相册图片管理
def test_album_photos_detail_against_mock_lychee(site):
    """真实走 HTTP：相对媒体地址补全为绝对地址，缩略图取最小尺寸变体。"""
    album, photos = grabber.lychee_album_photos_detail(site, "tok", "AbC1234567890XyZ")
    assert album["id"] == "AbC1234567890XyZ"
    assert album["title"] == "v4 album"
    assert album["count"] == 2
    assert album["truncated"] is False
    # Pv1 是 video/quicktime，缩略图退回 small 变体，原图是 .MOV
    assert photos[0]["id"] == "Pv1"
    assert photos[0]["type"] == "video"
    assert photos[0]["thumb"] == f"{site}/uploads/small/xx/abc.jpeg"
    assert photos[0]["url"] == f"{site}/uploads/original/xx/abc.MOV"
    # Pv2 没有 thumb/small 变体，缩略图退回原图地址
    assert photos[1]["id"] == "Pv2"
    assert photos[1]["type"] == "image"
    assert photos[1]["thumb"] == f"{site}/uploads/original/xx/pic.jpg"
    assert LAST_ALBUM_AUTH["value"] == "tok"  # v4 用原始 token，不加 Bearer


def _album_get_handler(payload, calls=None):
    def fake_post(url, json=None, headers=None, timeout=None):
        if calls is not None:
            calls.append((urlsplit(url).path, json, headers))
        resp = Mock()
        resp.status_code = 200
        resp.headers = {"Content-Type": "application/json"}
        resp.json.return_value = payload
        return resp

    return fake_post


def test_album_photos_detail_variant_priority_and_filtering(monkeypatch):
    """thumb→small→medium 优先；重复/缺 ID 的照片跳过；类型按 mime 或扩展名判断。"""
    payload = {
        "id": "A1",
        "title": "相册",
        "photos": [
            {
                "id": "1",
                "title": "a",
                "type": "image/jpeg",
                "created_at": "2026-09-01 10:00:00",
                "size_variants": {
                    "original": {"url": "uploads/original/aa/1.jpg"},
                    "medium": {"url": "uploads/medium/aa/1.jpeg"},
                    "thumb2x": {"url": "uploads/thumb2x/aa/1.jpeg"},
                    "thumb": {"url": "uploads/thumb/aa/1.jpeg"},
                },
            },
            {"id": "2", "title": "b", "type": "image/jpeg",
             "sizeVariants": {"small": "uploads/small/xx/2.jpeg"}},  # 旧版驼峰 + 字符串变体
            {"id": "3", "title": "c", "url": "uploads/original/xx/clip.mp4"},  # 无 mime, 靠扩展名
            {"id": "3", "title": "重复"},
            {"title": "没有 ID"},
            "不是对象",
        ],
    }
    calls: list = []
    monkeypatch.setattr(grabber.requests, "post", _album_get_handler(payload, calls))
    # base 带尾斜杠时也不能产生 //api、//uploads 这类双斜杠地址
    album, photos = grabber.lychee_album_photos_detail("http://lychee/", "tok", "A1")

    assert album == {"id": "A1", "title": "相册", "count": 3, "truncated": False}
    assert [p["id"] for p in photos] == ["1", "2", "3"]  # 重复 ID 与缺 ID 被丢弃
    assert photos[0]["thumb"] == "http://lychee/uploads/thumb/aa/1.jpeg"
    assert photos[0]["url"] == "http://lychee/uploads/original/aa/1.jpg"
    assert photos[0]["created_at"] == "2026-09-01 10:00:00"
    assert photos[1]["thumb"] == "http://lychee/uploads/small/xx/2.jpeg"
    assert photos[2]["type"] == "video"
    assert calls[0][0] == "/api/Album::get"
    assert calls[0][1] == {"albumID": "A1"}
    assert calls[0][2]["Authorization"] == "tok"


def test_album_photos_detail_truncates_at_limit(monkeypatch):
    """超过 ALBUM_MANAGE_LIMIT 张只返回前若干张，count 仍是相册真实张数。"""
    monkeypatch.setattr(grabber, "ALBUM_MANAGE_LIMIT", 2)
    payload = {
        "title": "大相册",
        "photos": [{"id": f"P{i}", "type": "image/jpeg", "url": f"uploads/original/x/{i}.jpg"}
                   for i in range(5)],
    }
    monkeypatch.setattr(grabber.requests, "post", _album_get_handler(payload))
    album, photos = grabber.lychee_album_photos_detail("http://lychee", "tok", "BIG")
    assert album["count"] == 5
    assert album["truncated"] is True
    assert len(photos) == 2


def test_album_photos_detail_reports_auth_failure(monkeypatch):
    def fake_post(url, json=None, headers=None, timeout=None):
        resp = Mock()
        resp.status_code = 401
        resp.text = "Unauthorized"
        resp.headers = {"Content-Type": "application/json"}
        return resp

    monkeypatch.setattr(grabber.requests, "post", fake_post)
    with pytest.raises(grabber.FetchError):
        grabber.lychee_album_photos_detail("http://lychee", "bad", "A1")


def test_album_photos_route(client, monkeypatch):
    seen = {}

    def fake_detail(base, token, album_id):
        seen["args"] = (base, token, album_id)
        return {"id": album_id, "title": "相册", "count": 1, "truncated": False}, [
            {"id": "P1", "title": "a", "type": "image", "created_at": "", "thumb": "t", "url": "u"}
        ]

    monkeypatch.setattr(grabber, "lychee_album_photos_detail", fake_detail)
    resp = client.post(
        "/api/album/photos",
        json={"lychee_url": "http://lychee/", "lychee_token": "t", "album_id": " A1 "},
    )
    data = resp.get_json()
    assert resp.status_code == 200
    assert data["ok"] is True
    assert data["album"]["count"] == 1
    assert data["photos"][0]["id"] == "P1"
    assert seen["args"] == ("http://lychee", "t", "A1")  # 尾部斜杠去掉、ID 去空格


def test_album_photos_route_requires_config(client, monkeypatch):
    monkeypatch.setattr(grabber, "LYCHEE_URL", "")
    monkeypatch.setattr(grabber, "LYCHEE_TOKEN", "")
    resp = client.post("/api/album/photos", json={"album_id": "A1"})
    assert resp.status_code == 400
    assert resp.get_json()["ok"] is False


def test_album_photos_route_maps_errors(client, monkeypatch):
    monkeypatch.setattr(
        grabber, "lychee_album_photos_detail",
        lambda base, token, album_id: (_ for _ in ()).throw(grabber.FetchError("token 无效")),
    )
    resp = client.post("/api/album/photos",
                       json={"lychee_url": "http://lychee", "lychee_token": "t", "album_id": "A1"})
    assert resp.status_code == 502
    assert resp.get_json()["error"] == "token 无效"

    monkeypatch.setattr(
        grabber, "lychee_album_photos_detail",
        lambda base, token, album_id: (_ for _ in ()).throw(grabber.requests.ConnectionError("boom")),
    )
    resp = client.post("/api/album/photos",
                       json={"lychee_url": "http://lychee", "lychee_token": "t", "album_id": "A1"})
    assert resp.status_code == 502
    assert "无法连接" in resp.get_json()["error"]


def test_album_photos_delete_route(client, monkeypatch):
    calls = []

    def fake_delete(base, token, photo_ids):
        calls.append((base, token, list(photo_ids)))

    monkeypatch.setattr(grabber, "lychee_delete_photos", fake_delete)
    resp = client.post(
        "/api/album/photos/delete",
        json={"lychee_url": "http://lychee", "lychee_token": "t", "album_id": "A1",
              "photo_ids": ["P2", "P1", "P2", " ", "P3"]},
    )
    data = resp.get_json()
    assert resp.status_code == 200
    assert data["ok"] is True
    assert data["deleted"] == 3
    assert data["photo_ids"] == ["P2", "P1", "P3"]  # 去重且保持勾选顺序
    assert calls == [("http://lychee", "t", ["P2", "P1", "P3"])]


def test_album_photos_delete_chunks_requests(client, monkeypatch):
    monkeypatch.setattr(grabber, "ALBUM_DELETE_CHUNK", 2)
    calls = []
    monkeypatch.setattr(
        grabber, "lychee_delete_photos",
        lambda base, token, ids: calls.append(list(ids)),
    )
    resp = client.post(
        "/api/album/photos/delete",
        json={"lychee_url": "http://lychee", "lychee_token": "t", "album_id": "A1",
              "photo_ids": ["1", "2", "3", "4", "5"]},
    )
    assert resp.get_json()["deleted"] == 5
    assert calls == [["1", "2"], ["3", "4"], ["5"]]


def test_album_photos_delete_rejects_bad_requests(client, monkeypatch):
    monkeypatch.setattr(grabber, "lychee_delete_photos",
                        lambda *a, **k: pytest.fail("不应调用删除"))
    base = {"lychee_url": "http://lychee", "lychee_token": "t", "album_id": "A1"}

    resp = client.post("/api/album/photos/delete", json={**base, "photo_ids": "P1"})
    assert resp.status_code == 400          # 必须是一组 ID
    resp = client.post("/api/album/photos/delete", json={**base})
    assert resp.status_code == 400          # 缺 photo_ids
    resp = client.post("/api/album/photos/delete", json={**base, "photo_ids": ["", "  "]})
    assert resp.status_code == 400          # 全空 ID
    assert "勾选" in resp.get_json()["error"]
    resp = client.post("/api/album/photos/delete",
                       json={"lychee_url": "http://lychee", "lychee_token": "t",
                             "photo_ids": ["P1"]})
    assert resp.status_code == 400          # 缺 album_id
    monkeypatch.setattr(grabber, "ALBUM_DELETE_LIMIT", 2)
    resp = client.post("/api/album/photos/delete",
                       json={**base, "photo_ids": ["1", "2", "3"]})
    assert resp.status_code == 400          # 超出单次上限
    assert "分批" in resp.get_json()["error"]


def test_album_photos_delete_route_maps_errors(client, monkeypatch):
    monkeypatch.setattr(
        grabber, "lychee_delete_photos",
        lambda base, token, ids: (_ for _ in ()).throw(grabber.FetchError("只读账号无删除权限")),
    )
    resp = client.post(
        "/api/album/photos/delete",
        json={"lychee_url": "http://lychee", "lychee_token": "t", "album_id": "A1",
              "photo_ids": ["P1"]},
    )
    assert resp.status_code == 502
    assert resp.get_json()["error"] == "只读账号无删除权限"


def test_index_html_has_album_manager_panel(client):
    """首页包含图片管理面板及其交互控件，选相册即可加载。"""
    html = client.get("/").get_data(as_text=True)
    for needle in ("mgmt-wrap", "mgmt-grid", "mgmt-select-all", "mgmt-delete",
                   "/api/album/photos", "/api/album/photos/delete", "loadMgmt()"):
        assert needle in html
    assert 'id="album-select"' in html


def test_index_html_has_footer_pager(client):
    """底栏两页 pager: 抓取按钮进底栏, 抓取/管理各有独立相册下拉框。"""
    html = client.get("/").get_data(as_text=True)
    for needle in ('id="footer" data-page="0"', 'id="pager"', 'id="pager-track"',
                   'id="tab-grab"', 'id="tab-mgmt"', 'id="album-mgmt-select"',
                   'id="transfer-row"', 'setFooterPage', 'bindSlidePager',
                   "--footer-h", "env(safe-area-inset-bottom)", "no-transfer"):
        assert needle in html, needle
    # 三个抓取按钮已移入底栏, 抓取卡片里只剩网址输入框
    actions = re.search(r'<div class="row actions">(.*?)</div>', html, re.S).group(1)
    assert "preview-btn" not in actions and "extract-btn" not in actions
    for btn in ("preview-btn", "extract-btn", "deep-btn"):
        assert html.index('id="footer"') < html.index('id="%s"' % btn)
    # 选相册语义隔离: 管理面板只读管理用的下拉框
    assert '$("album-mgmt-select")' in html


def test_index_html_two_independent_views(client):
    """抓取转存与相册管理是两个独立界面: 各自一个滚动视图, 整屏滑动切换, 面板不混显。"""
    html = client.get("/").get_data(as_text=True)
    for needle in ('<body data-view="0">', 'id="vp-track"',
                   'class="vp-page" id="view-grab"', 'class="vp-page" id="view-mgmt"',
                   'body[data-view="1"] { --slide: -50%; }',
                   "translateX(var(--slide, 0%))",
                   'document.body.dataset.view = String(footerPage)',
                   'el.inert = i !== footerPage',
                   "function showMgmtPlaceholder", "if (footerPage !== 1) return"):
        assert needle in html, needle

    grab = html[html.index('id="view-grab"'):html.index('id="view-mgmt"')]
    mgmt = html[html.index('id="view-mgmt"'):html.index('id="footer"')]
    # 抓取相关面板只在抓取视图, 管理面板只在管理视图
    for own in ('id="preview-wrap"', 'id="gallery-wrap"', 'id="summary"'):
        assert own in grab and own not in mgmt, own
    assert 'id="mgmt-wrap"' in mgmt and 'id="mgmt-wrap"' not in grab
    # 管理界面不再有「收起面板」这种会留下空白界面的入口, 改为返回抓取
    assert "mgmt-hide" not in html and 'id="mgmt-back"' in html
    # 结果属于抓取界面: 提取成功后自动切回第一个界面
    assert "结果属于「抓取转存」界面" in html and "setFooterPage(0)" in html


def test_index_html_local_upload_and_auto_mgmt(client):
    """上传按钮取代「查看/管理图片」; 面板改为进相册管理页自动打开; 按钮行等宽不溢出。"""
    html = client.get("/").get_data(as_text=True)
    for needle in ('id="local-upload-btn"', 'id="local-files"', 'accept="image/*,video/*"',
                   "function maybeOpenMgmt", "function showMgmtPlaceholder", "MGMT_CACHE_MS",
                   "async function startLocalUpload", "function uploadOneLocal",
                   "/api/upload_local", "if (footerPage === 1) {", "maybeOpenMgmt();",
                   ".prow.btns", "clamp(13px, 3.5vw, 15px)", "min-height: 44px",
                   ".prow.btns button { flex: 1 1 0; min-width: 0; min-height: 44px; }"):
        assert needle in html, needle
    assert "mgmt-open-btn" not in html
    footer_at = html.index('id="footer"')
    assert footer_at < html.index('id="local-upload-btn"')
    assert footer_at < html.index('id="page-mgmt"')
    # 窄屏短文案 + 完整文案并存, title 保留说明
    row = re.search(r'<button id="local-upload-btn".*?</button>', html, re.S).group(0)
    assert 'class="t-long"' in row and 'class="t-short"' in row and "title=" in row
    assert ".t-short { display: none; }" in html


def test_index_html_qr_scan(client):
    """扫码识图: 图片只在本机解码, 用仓库内 vendored 的 jsQR, 识别结果只填网址不自动抓取。"""
    html = client.get("/").get_data(as_text=True)
    for needle in ('id="url-row"', 'id="qr-btn"', '"/static/qr.js"',
                   "function loadQrLib", "function qrToUrl", "async function qrDecode",
                   "async function handleQrFile", "jsQR(data.data", "inversionAttempts",
                   ".actions .url-row { width: 100%; }"):
        assert needle in html, needle
    # 只吃图片, 不接受视频; 且必须是本机静态资源, 不引外部 CDN(局域网/离线可用)
    file_input = re.search(r'<input type="file" id="qr-file"[^>]*>', html).group(0)
    assert 'accept="image/*"' in file_input and "multiple" not in file_input
    for bad in ("cdn.jsdelivr.net", "unpkg.com", "cdnjs.", "ga.jspm.io"):
        assert bad not in html, bad
    # 识别成功: 填入 #page-url + 高亮「提取图片」, 不直接发起抓取
    assert "input.value = url" in html and 'eb.classList.add("qr-flash")' in html
    assert "点「提取图片」开始" in html
    # 非 http(s) 内容(如 weixin://)要被拒绝, 裸域名/IP 自动补 http://
    assert 'if (/^[a-z][a-z0-9+.-]*:\\/\\//i.test(raw)) return "";' in html
    assert 'return "http://" + raw;' in html


def test_static_qr_library_is_vendored(client):
    """jsQR 随镜像一起发布, /static/qr.js 必须能离线取到。"""
    resp = client.get("/static/qr.js")
    assert resp.status_code == 200
    body = resp.get_data()
    assert len(body) > 50_000 and b"jsQR" in body


def test_index_html_bookmarks_collapsed(client):
    """书签改成与 Lychee 设置同款的折叠面板: 默认收起, 点「📑 书签」才展开, 收起时不占空间。"""
    html = client.get("/").get_data(as_text=True)
    toggle = re.search(r'<button class="settings-btn" id="bm-toggle".*?</button>', html, re.S).group(0)
    assert "📑 书签" in toggle and 'id="bm-count"' in toggle and "▸" in toggle
    body = re.search(r'<div class="bm-body" id="bm-body"[^>]*>', html).group(0)
    assert 'style="display:none"' in body
    for needle in ("function setBmOpen", '$("bm-toggle").addEventListener',
                   '$("bm-body").style.display = open ? "" : "none"'):
        assert needle in html, needle
    # 旧的常驻标题行不再存在, 书签控件全部收进可折叠面板里
    assert "<b>📑 书签</b>" not in html
    assert html.index('id="bm-toggle"') < html.index('id="bm-body"')
    seg = html[html.index('id="bm-body"'):html.index('id="preview-wrap"')]
    for own in ("bm-select", "bm-add", "bm-edit", "bm-del", "bm-editor"):
        assert own in seg, own


def test_index_html_settings_panel_no_layout_jump(client):
    """Lychee 设置的开合由用户说了算: 手动开合过就记住, 连接成功/切界面都不再强行收起; 圆点只反映真实连通状态。"""
    html = client.get("/").get_data(as_text=True)
    for needle in ('const KEY_SETTINGS_OPEN = "img-grabber.settings_open"',
                   "function autoCollapseSettings",
                   "if (localStorage.getItem(KEY_SETTINGS_OPEN) === null) setSettingsOpen(false)",
                   'setSettingsOpen($("lychee-form").style.display === "none", true)',
                   'savedOpen === null ? !settingsConfigured() : savedOpen === "1"',
                   ".dot.warn {", "let lycheeConnected = false"):
        assert needle in html, needle
    # 收起只可能来自 autoCollapseSettings 这一处判断, 别处不再无条件收起
    assert html.count("setSettingsOpen(false)") == 1
    assert html.count("autoCollapseSettings();") == 2
    # 改过地址/Token 视为未验证, 圆点不能继续装绿
    assert '["lychee-url", "lychee-token"].forEach' in html
    assert '"dot" + (lycheeConnected ? " ok" : settingsConfigured() ? " warn" : "")' in html


def test_index_html_mobile_keyboard_zoom(client):
    """键盘/缩放修复: 输入框 ≥16px 不再触发 iOS 自动放大; touch-action 保留 pinch-zoom 才缩得回去;
    键盘高度由 visualViewport 算成 --kb-h 驱动底栏与内容区。"""
    html = client.get("/").get_data(as_text=True)
    assert "initial-scale=1, viewport-fit=cover" in html
    # 所有 touch-action 都必须带 pinch-zoom(底栏按钮的 manipulation 除外), 否则放大后缩不回来
    for value in re.findall(r"touch-action:\s*([^;}]+)", html):
        v = value.strip()
        assert "pinch-zoom" in v or v == "manipulation", v
    assert html.count("touch-action: pan-y pinch-zoom") == 3
    for needle in ("--kb-h", "window.visualViewport", "function bindKeyboardViewport",
                   "calc(100dvh - var(--kb-h, 0px))", "bottom: var(--kb-h, 0px)",
                   "@media (hover: none)"):
        assert needle in html, needle
    # 手机端与触屏档: 可输入控件一律 16px(iOS 的自动放大红线)
    mob = html[html.index("@media (max-width: 640px)"):html.index("@media (hover: none)")]
    rule = re.search(r"input\[type=text\], input\[type=url\], select \{[^}]*\}", mob).group(0)
    assert "font-size: 16px" in rule and "15px" not in rule
    assert ".bm-editor input[type=text] { font-size: 16px; }" in mob
    touch = html[html.index("@media (hover: none)"):]
    assert "textarea { font-size: 16px; }" in touch.split("@media (max-width: 400px)")[0]


def test_index_html_track_alignment(client):
    """底栏轨道不能留 gap: 否则第二页停靠时右移 8px, 下拉框箭头与「清空相册」被裁。"""
    html = client.get("/").get_data(as_text=True)
    track = re.search(r"\.track \{([^}]*)\}", html).group(1)
    assert "gap" not in track, track
    assert "width: 200%" in track
    # 等宽按钮行不能用 auto-fit 栅格: WebKit 会按最长内容撑破容器
    assert "auto-fit" not in html, "底栏等宽必须用 flex:1 1 0, 不能用 repeat(auto-fit,...)"
    page = re.search(r"\.page \{[^}]*padding-right: 8px[^}]*\}", html)
    assert page, "两页的缝隙必须写在 .page 的 padding-right 里(含在 50% 宽度内)"
    # flex 项的 min-width:auto 会让装满长相册名的下拉框把整页撑宽(手机上表现为按钮被裁)
    assert re.search(r"\.page \{[^}]*flex: none; min-width: 0;", html), ".page 必须 min-width:0"
    # 上限用 JS 写死的像素宽度, 不依赖引擎对百分比/固有宽度的解释
    assert "max-width: var(--pager-w, 50%)" in html
    assert "max-width: calc(var(--pager-w, 100%) - 8px)" in html
    assert "function syncPagerWidth" in html and '--pager-w", w + "px"' in html
    assert ".page { flex-direction: column; flex-wrap: nowrap;" in html


def test_index_html_auto_unzoom(client):
    """放大状态: --kb-h 必须按 scale 折算(否则凭空多出假键盘高度), 且键盘收起后要把缩放复位。"""
    html = client.get("/").get_data(as_text=True)
    assert "const kb = Math.max(0, Math.round(innerHeight - (vv.height + vv.offsetTop) * s));" in html
    assert "const s = vv.scale || 1;" in html
    assert "function bindAutoUnzoom" in html
    for needle in ('meta[name="viewport"]', "maximum-scale=1", "vv.scale > 1.01",
                   "setMeta(base + \", maximum-scale=1, user-scalable=no\", tries > 1)",
                   "setTimeout(() => setMeta(base, false), 600)"):
        assert needle in html, needle
