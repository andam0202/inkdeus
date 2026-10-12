"""漫画のページを PDF にまとめる（サーバ側）: ページの寸法、埋め込み画像、色、綴じ方向、不正な入力、片付け"""

from __future__ import annotations

import io
import json
import threading
import time
import urllib.error
import urllib.request

import numpy as np
import pytest
from PIL import Image

import synth
from conftest import serve
from inkdeus import cli, pdf
from inkdeus.server import create_app

pypdf = pytest.importorskip("pypdf")

MM = 72.0 / 25.4


def png(width: int, height: int, mode: str = "L") -> bytes:
    """左上が黒・右下が白の斜めのグラデーション（合成画像）。RGB は赤みを付ける"""
    y, x = np.mgrid[0:height, 0:width]
    gray = ((x / max(1, width - 1) + y / max(1, height - 1)) * 127.5).astype(np.uint8)
    if mode == "RGB":
        data = np.dstack([gray, gray // 2, gray // 4])
    elif mode == "RGBA":
        data = np.dstack([gray, gray, gray, np.full_like(gray, 255)])
        data[: height // 2, :, 3] = 0                                # 上半分は透明
    else:
        data = gray
    buf = io.BytesIO()
    Image.fromarray(data, mode).save(buf, "PNG")
    return buf.getvalue()


@pytest.fixture()
def client(tmp_path):
    folder = tmp_path / "images"
    synth.save(folder / "a.png", synth.flat(32, 16, 200))
    app = create_app(folder, out_dir=tmp_path / "out", local_config=tmp_path / "none.toml",
                     cache_root=tmp_path / "cache", watch=False)
    c = app.test_client()
    c.jobs = app.config["INKDEUS_PDF_JOBS"]
    c.out = tmp_path / "out"
    return c


def build(client, name: str, settings: dict, pages: dict[int, bytes]) -> dict:
    r = client.post(f"/api/manga/pdf/{name}/begin", json=settings)
    assert r.status_code == 200, r.get_json()
    for number, data in pages.items():
        r = client.post(f"/api/manga/pdf/{name}/page/{number}", data=data)
        assert r.status_code == 200, r.get_json()
    r = client.post(f"/api/manga/pdf/{name}/finish", json={})
    assert r.status_code == 200, r.get_json()
    return r.get_json()


def read(path) -> pypdf.PdfReader:
    return pypdf.PdfReader(str(path))


def box(page, name: str) -> list[float]:
    return [float(v) for v in page[name]]


def image_of(page):
    return page["/Resources"]["/XObject"]["/Im0"]


def pixels(page) -> np.ndarray:
    """埋め込まれた画像の画素（展開した物）"""
    im = image_of(page)
    raw = im.get_data()
    w, h, bits = int(im["/Width"]), int(im["/Height"]), int(im["/BitsPerComponent"])
    if bits == 1:
        rows = np.frombuffer(raw, np.uint8).reshape(h, -1)
        return np.unpackbits(rows, axis=1)[:, :w] * 255
    channels = 3 if im["/ColorSpace"] == "/DeviceRGB" else 1
    data = np.frombuffer(raw, np.uint8).reshape(h, w, channels)
    return data[..., 0] if channels == 1 else data


def test_pages_boxes_and_gray_image(client):
    # B5（182×257mm）・600dpi・塗り足しなし。絵の縦横比は用紙と同じ
    info = build(client, "book", {"size": "B5", "dpi": 600}, {1: png(364, 514), 2: png(364, 514), 3: png(364, 514)})
    assert info["pages"] == 3 and info["numbers"] == [1, 2, 3] and info["url"] == "/api/manga/pdf/book.pdf"
    path = client.out / "manga" / "pdf" / "book.pdf"
    assert info["path"] == str(path) and path.stat().st_size == info["bytes"]
    doc = read(path)
    assert len(doc.pages) == 3
    for page in doc.pages:
        assert box(page, "/MediaBox") == pytest.approx([0, 0, 182 * MM, 257 * MM], abs=0.01)
        assert box(page, "/TrimBox") == pytest.approx([0, 0, 182 * MM, 257 * MM], abs=0.01)
        assert box(page, "/BleedBox") == pytest.approx([0, 0, 182 * MM, 257 * MM], abs=0.01)
        im = image_of(page)
        assert (int(im["/Width"]), int(im["/Height"])) == (round(182 / 25.4 * 600), round(257 / 25.4 * 600))
        assert im["/ColorSpace"] == "/DeviceGray" and int(im["/BitsPerComponent"]) == 8
        assert im["/Filter"] == "/FlateDecode"                       # 可逆（JPEG にしない）
    data = pixels(doc.pages[0])
    assert data[0, 0] < 10 and data[-1, -1] > 245                     # 絵がページいっぱいに入っている
    assert doc.metadata.title == "" or doc.metadata.title is None or isinstance(doc.metadata.title, str)


def test_bleed_extends_the_page_and_sets_trim_box(client):
    info = build(client, "bleed", {"size": "A5", "dpi": 350, "bleedMm": 3, "fit": "cover"}, {1: png(296, 420)})
    page = read(info["path"]).pages[0]
    full_w, full_h = (148 + 6) * MM, (210 + 6) * MM
    assert box(page, "/MediaBox") == pytest.approx([0, 0, full_w, full_h], abs=0.01)
    assert box(page, "/BleedBox") == pytest.approx([0, 0, full_w, full_h], abs=0.01)
    assert box(page, "/TrimBox") == pytest.approx([3 * MM, 3 * MM, full_w - 3 * MM, full_h - 3 * MM], abs=0.01)
    im = image_of(page)
    assert (int(im["/Width"]), int(im["/Height"])) == (round(154 / 25.4 * 350), round(216 / 25.4 * 350))
    data = pixels(page)
    # 埋める: 絵が「仕上がり＋塗り足し」いっぱいに広がる（四隅まで絵で、白い余白が無い）
    assert data[0, 0] < 10 and data[0, -1] < 200 and data[-1, 0] < 200
    assert info["pageInfo"][0]["trimMm"] == pytest.approx([148, 210])


def test_contain_leaves_white_margins_and_cover_crops(client):
    wide = png(400, 100)                                              # 用紙（縦長）より横長の絵 → 用紙は横向きになる
    square = png(300, 300)
    contain = read(build(client, "c1", {"size": "B6", "dpi": 100, "fit": "contain"}, {1: square})["path"]).pages[0]
    data = pixels(contain)
    h, w = data.shape
    assert (w, h) == (round(128 / 25.4 * 100), round(182 / 25.4 * 100))
    assert (data[: (h - w) // 2 - 2] == 255).all() and (data[-((h - w) // 2 - 2):] == 255).all()   # 上下が白い余白
    assert data[h // 2, 2] < 140                                      # 中央の帯には絵がある
    cover = pixels(read(build(client, "c2", {"size": "B6", "dpi": 100, "fit": "cover"}, {1: square})["path"]).pages[0])
    assert cover[0, 0] < 80 and (cover == 255).mean() < 0.02          # 余白なし（左右が切られている）
    # 絵と用紙の向きが違うときは、用紙の縦横を入れ替える
    page = read(build(client, "c3", {"size": "B6", "dpi": 100}, {1: wide})["path"]).pages[0]
    assert box(page, "/MediaBox") == pytest.approx([0, 0, 182 * MM, 128 * MM], abs=0.01)


def test_match_aspect_shrinks_the_trim_to_the_page_ratio(client):
    info = build(client, "fit", {"size": "A4", "dpi": 150, "matchAspect": True, "bleedMm": 5}, {1: png(200, 400)})
    page = read(info["path"]).pages[0]
    # 1:2 の絵 → A4（210×297）に余白なしで収まる最大は 148.5×297
    assert info["pageInfo"][0]["trimMm"] == pytest.approx([148.5, 297])
    assert box(page, "/TrimBox") == pytest.approx([5 * MM, 5 * MM, 153.5 * MM, 302 * MM], abs=0.01)
    assert box(page, "/MediaBox") == pytest.approx([0, 0, 158.5 * MM, 307 * MM], abs=0.01)


def test_bw_is_embedded_as_one_bit_with_the_threshold(client):
    info = build(client, "bw", {"size": "custom", "widthMm": 50.8, "heightMm": 50.8, "dpi": 100, "color": "bw",
                                "threshold": 64}, {1: png(200, 200)})
    page = read(info["path"]).pages[0]
    im = image_of(page)
    assert int(im["/BitsPerComponent"]) == 1 and im["/ColorSpace"] == "/DeviceGray" and im["/Filter"] == "/FlateDecode"
    assert (int(im["/Width"]), int(im["/Height"])) == (200, 200)
    data = pixels(page)
    assert set(np.unique(data)) == {0, 255}
    source = np.asarray(Image.open(io.BytesIO(png(200, 200))))
    assert (data == np.where(source >= 64, 255, 0)).all()             # 同じ大きさなので拡大縮小せず、しきい値どおり
    assert info["pageInfo"][0]["resampled"] is False
    # しきい値を上げると黒が増える
    darker = pixels(read(build(client, "bw2", {"size": "custom", "widthMm": 50.8, "heightMm": 50.8, "dpi": 100,
                                               "color": "bw", "threshold": 200}, {1: png(200, 200)})["path"]).pages[0])
    assert (darker == 0).mean() > (data == 0).mean() + 0.2


def test_rgb_and_transparency(client):
    info = build(client, "rgb", {"size": "custom", "widthMm": 25.4, "heightMm": 25.4, "dpi": 100, "color": "rgb"},
                 {1: png(100, 100, "RGB"), 2: png(100, 100, "RGBA")})
    doc = read(info["path"])
    im = image_of(doc.pages[0])
    assert im["/ColorSpace"] == "/DeviceRGB" and int(im["/BitsPerComponent"]) == 8
    data = pixels(doc.pages[0])
    assert data.shape == (100, 100, 3) and int(data[60, 60, 0]) > int(data[60, 60, 2]) + 30        # 色が残っている
    assert (pixels(doc.pages[1])[:50] == 255).all()                   # 透明な所は白


def test_pages_are_ordered_by_number_and_range_is_parsed(client):
    # 届いた順ではなく番号の順に並ぶ。抜けた番号があってもよい（範囲指定）
    info = build(client, "range", {"size": "custom", "widthMm": 25.4, "heightMm": 25.4, "dpi": 100},
                 {10: png(100, 100), 2: png(40, 40), 1: png(100, 100)})
    assert info["numbers"] == [1, 2, 10] and len(read(info["path"]).pages) == 3
    assert [p["resampled"] for p in info["pageInfo"]] == [False, True, False]
    assert pdf.parse_pages("1-8,10", 12) == [1, 2, 3, 4, 5, 6, 7, 8, 10]
    assert pdf.parse_pages(" 3 , 1-2 ,2", 3) == [1, 2, 3]
    assert pdf.parse_pages("", 4) == [1, 2, 3, 4] and pdf.parse_pages(None, 2) == [1, 2]
    for bad in ("0", "3-1", "a", "1-", "1,,2", "5"):
        with pytest.raises(pdf.PdfError):
            pdf.parse_pages(bad, 4)


@pytest.mark.parametrize("binding,direction", [("right", "/R2L"), ("left", "/L2R")])
def test_binding_direction_and_page_layout(client, binding, direction):
    info = build(client, "dir", {"size": "B5", "dpi": 72, "binding": binding, "layout": "spread-cover",
                                 "title": "第1話"}, {1: png(90, 128)})
    doc = read(info["path"])
    root = doc.trailer["/Root"]
    assert root["/ViewerPreferences"]["/Direction"] == direction
    assert root["/PageLayout"] == "/TwoPageRight"
    assert doc.metadata.title == "第1話"
    assert info["settings"]["binding"] == binding


def test_default_layout_is_single_page(client):
    root = read(build(client, "one", {"dpi": 72}, {1: png(90, 128)})["path"]).trailer["/Root"]
    assert root["/PageLayout"] == "/SinglePage" and root["/ViewerPreferences"]["/Direction"] == "/R2L"


@pytest.mark.parametrize("settings,word", [
    ({"size": "B4"}, "仕上がりサイズ"),
    ({"size": "custom", "widthMm": 5, "heightMm": 100}, "幅"),
    ({"size": "custom", "widthMm": 100}, "高さ"),
    ({"dpi": 10}, "解像度"),
    ({"dpi": "600"}, "解像度"),
    ({"bleedMm": 50}, "塗り足し"),
    ({"fit": "stretch"}, "はめ込み方"),
    ({"color": "cmyk"}, "色"),
    ({"threshold": 0}, "しきい値"),
    ({"binding": "top"}, "綴じ方向"),
    ({"layout": "book"}, "見開き表示"),
    ({"size": "custom", "widthMm": 2000, "heightMm": 2000, "dpi": 2400}, "大きすぎる"),
])
def test_bad_settings_are_rejected(client, settings, word):
    r = client.post("/api/manga/pdf/x/begin", json=settings)
    assert r.status_code == 400 and word in r.get_json()["error"], r.get_json()
    assert client.jobs == {}


def test_bad_requests(client):
    assert client.post("/api/manga/pdf/..%2Fx/begin", json={}).status_code in (400, 404, 405)
    assert client.post("/api/manga/pdf/a.b/begin", json={}).status_code == 400
    # begin の前に送ったページ・finish は断る
    assert client.post("/api/manga/pdf/book/page/1", data=png(10, 10)).status_code == 409
    assert client.post("/api/manga/pdf/book/finish", json={}).status_code == 409
    assert client.post("/api/manga/pdf/book/begin", json={"dpi": 72}).status_code == 200
    assert client.post("/api/manga/pdf/book/page/1", data=b"not a png").status_code == 400
    assert client.post("/api/manga/pdf/book/page/0", data=png(10, 10)).status_code == 400
    assert client.post("/api/manga/pdf/book/page/1", data=b"\x89PNG\r\n\x1a\nbroken").status_code == 400
    # ページが1つも無いまま finish はできない（書き出しは続けられる）
    r = client.post("/api/manga/pdf/book/finish", json={})
    assert r.status_code == 400 and "ページ" in r.get_json()["error"]
    assert client.get("/api/manga/pdf/book.pdf").status_code == 404
    assert not (client.out / "manga" / "pdf" / "book.pdf").exists()


def test_abort_and_restart_clean_up_the_temporary_files(client):
    assert client.post("/api/manga/pdf/book/begin", json={"dpi": 72}).status_code == 200
    client.post("/api/manga/pdf/book/page/1", data=png(90, 128))
    tmp = client.jobs["book"]["tmp"]
    assert tmp.is_dir() and [p.name for p in tmp.iterdir()] == ["0001.z"]
    assert client.get("/api/manga/pdf").get_json()["running"] == ["book"]
    # 同じ名前で始め直すと、前の分は片付く
    assert client.post("/api/manga/pdf/book/begin", json={"dpi": 72}).status_code == 200
    again = client.jobs["book"]["tmp"]
    assert not tmp.exists() and again.is_dir() and again != tmp
    r = client.post("/api/manga/pdf/book/abort", json={})
    assert r.get_json() == {"name": "book", "aborted": True}
    assert not again.exists() and client.jobs == {}
    assert client.post("/api/manga/pdf/book/abort", json={}).get_json()["aborted"] is False
    assert client.post("/api/manga/pdf/book/page/2", data=png(90, 128)).status_code == 409
    assert not (client.out / "manga" / "pdf").exists()
    # 最後まで終えたときも一時フォルダは残らない
    client.post("/api/manga/pdf/book/begin", json={"dpi": 72})
    done = client.jobs["book"]["tmp"]
    client.post("/api/manga/pdf/book/page/1", data=png(90, 128))
    assert client.post("/api/manga/pdf/book/finish", json={}).status_code == 200
    assert not done.exists() and client.jobs == {}


def test_list_and_download(client):
    assert client.get("/api/manga/pdf").get_json()["pdfs"] == []
    build(client, "第1話", {"dpi": 72}, {1: png(90, 128)})
    build(client, "omake", {"dpi": 72}, {1: png(90, 128), 2: png(90, 128)})
    listing = client.get("/api/manga/pdf").get_json()
    assert [p["name"] for p in listing["pdfs"]] == ["omake", "第1話"]
    assert listing["dir"] == str(client.out / "manga" / "pdf") and listing["sizes"]["B5"] == [182.0, 257.0]
    r = client.get("/api/manga/pdf/omake.pdf")
    assert r.status_code == 200 and r.mimetype == "application/pdf" and r.data.startswith(b"%PDF-")
    assert len(pypdf.PdfReader(io.BytesIO(r.data)).pages) == 2
    assert client.get("/api/manga/pdf/none.pdf").status_code == 404
    assert "mangaPdf" in client.get("/api/config").get_json()["features"]


# ---- 外部からの指示（inkdeus ctl manga pdf）: ブラウザの代役が実行して結果を返す ----

class FakeMangaTab(threading.Thread):
    """/manga/ を開いているブラウザの代役: 指示を受けて、PDF を作って結果を返す"""

    def __init__(self, url: str, fail: str | None = None):
        super().__init__(daemon=True)
        self.url, self.fail = url, fail
        self.seen: list[dict] = []
        self.ready = threading.Event()

    def post(self, path, body):
        data = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
        req = urllib.request.Request(self.url + path, data=data, method="POST")
        return json.loads(urllib.request.urlopen(req, timeout=10).read())

    def run(self):
        try:
            self.listen()
        except OSError:
            pass                                                     # サーバが止まった

    def listen(self):
        stream = urllib.request.urlopen(self.url + "api/events?role=manga", timeout=30)
        for raw in stream:
            line = raw.decode("utf-8").strip()
            if line.startswith(":"):
                self.ready.set()
            if not line.startswith("data:"):
                continue
            ev = json.loads(line[5:])
            if ev.get("type") != "manga-command":
                continue
            self.seen.append(ev)
            result = f"api/manga/result/{ev['id']}"
            self.post(result, {"state": "running", "progress": {"done": 0, "total": 1}})
            if self.fail:
                self.post(result, {"state": "error", "error": self.fail})
                continue
            name = ev["args"].get("name") or "manga"
            self.post(f"api/manga/pdf/{name}/begin", ev["args"]["settings"])
            self.post(f"api/manga/pdf/{name}/page/1", png(90, 128))
            self.post(result, {"state": "done", "result": self.post(f"api/manga/pdf/{name}/finish", {})})


@pytest.fixture()
def live(tmp_path):
    folder = tmp_path / "images"
    synth.save(folder / "a.png", synth.flat(32, 16, 200))
    server, url = serve(create_app(folder, out_dir=tmp_path / "out", local_config=tmp_path / "none.toml",
                                   cache_root=tmp_path / "cache", watch=False))
    yield {"port": server.server_port, "url": url, "out": tmp_path / "out"}
    server.shutdown()


@pytest.fixture()
def ctl(live, capsys):
    def run(*args, expect=0):
        capsys.readouterr()
        code = cli.main(["ctl", "--port", str(live["port"]), *args])
        captured = capsys.readouterr()
        assert code == expect, captured.err or captured.out
        return json.loads(captured.out if code == 0 else captured.err)
    return run


def test_ctl_manga_pdf(live, ctl):
    # /manga/ を開いているブラウザが無い
    assert "ブラウザがありません" in ctl("manga", "pdf", "--wait", expect=2)["error"]
    tab = FakeMangaTab(live["url"])
    tab.start()
    assert tab.ready.wait(10)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:                               # 購読が登録されるまで
        if json.loads(urllib.request.urlopen(live["url"] + "api/state").read())["clients"] == 0:
            break
    result = ctl("manga", "pdf", "--name", "book", "--size", "A5", "--dpi", "72", "--bleed", "3", "--pages", "1-2",
                 "--fit", "cover", "--color", "bw", "--binding", "left", "--wait", "--timeout", "30")
    path = live["out"] / "manga" / "pdf" / "book.pdf"
    assert result["path"] == str(path) and result["pages"] == 1
    sent = tab.seen[-1]
    assert sent["command"] == "pdf" and sent["args"]["pages"] == "1-2" and sent["args"]["name"] == "book"
    assert sent["args"]["settings"] == {"size": "A5", "dpi": 72.0, "bleedMm": 3.0, "fit": "cover", "color": "bw",
                                        "binding": "left", "layout": "single", "matchAspect": False, "threshold": 128}
    doc = read(path)
    assert doc.trailer["/Root"]["/ViewerPreferences"]["/Direction"] == "/L2R"
    assert box(doc.pages[0], "/TrimBox") == pytest.approx([3 * MM, 3 * MM, 151 * MM, 213 * MM], abs=0.01)
    # カスタムの大きさ
    ctl("manga", "pdf", "--name", "c", "--width", "100", "--height", "150", "--dpi", "72", "--wait")
    assert tab.seen[-1]["args"]["settings"]["size"] == "custom" and tab.seen[-1]["args"]["settings"]["widthMm"] == 100
    assert "両方" in ctl("manga", "pdf", "--width", "100", expect=1)["error"]
    # 待たなければ、指示の番号だけが返る
    sent = ctl("manga", "pdf", "--name", "d", "--dpi", "72")
    assert sent["clients"] == 1 and sent["command"] == "pdf" and sent["id"]


def test_ctl_manga_pdf_reports_browser_errors(live, ctl):
    tab = FakeMangaTab(live["url"], fail="3 ページはありません（全 2 ページ）")
    tab.start()
    assert tab.ready.wait(10)
    err = ctl("manga", "pdf", "--pages", "3", "--wait", expect=1)
    assert "3 ページはありません" in err["error"] and err["state"] == "error"


def test_manga_command_api(live):
    def call(path, body=None):
        req = urllib.request.Request(live["url"] + path, data=None if body is None else json.dumps(body).encode(),
                                     method="GET" if body is None else "POST")
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            text = e.read()
            return e.code, json.loads(text) if text.startswith(b"{") else None

    assert call("api/manga/command", {"command": "format-disk"})[0] == 400
    assert call("api/manga/command", {"command": "pdf", "args": []})[0] == 400
    code, sent = call("api/manga/command", {"command": "pdf", "args": {}})
    assert code == 200 and sent["clients"] == 0 and sent["state"] == "no-client"
    assert call(f"api/manga/result/{sent['id']}")[1]["state"] == "no-client"
    assert call("api/manga/result/unknown")[0] == 404
    assert call(f"api/manga/result/{sent['id']}", {"state": "bogus"})[0] == 400
    assert call(f"api/manga/result/{sent['id']}", {"state": "done", "result": {"ok": True}})[1]["result"] == {"ok": True}
