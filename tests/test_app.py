import io
import json
import pathlib
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote, urlsplit

import pytest
from unittest.mock import Mock
from PIL import Image

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
