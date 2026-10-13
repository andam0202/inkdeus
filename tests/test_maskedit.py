"""部分マスク・人物マスクの手直しの土台（サーバ・コマンド）: 置き場と API、元のマスクとの合成、書き出し、
白黒版の「古い」判定、世代バックアップ。画面のツール（描いて手直しする側）は、この上に載る"""

from __future__ import annotations

import io
import json
import struct

import numpy as np
import pytest
from PIL import Image

import synth
from inkdeus import cli, maskedit
from inkdeus.projects import create_project
from inkdeus.server import create_app

W, H = 40, 30


def png_of(arr: np.ndarray) -> bytes:
    buf = io.BytesIO()
    Image.fromarray(arr.astype(np.uint8), "L").save(buf, "PNG")
    return buf.getvalue()


def edit(**boxes) -> bytes:
    """3 値の手直し: add=(x0, y0, x1, y1) を 255、remove=… を 0、ほかは 128"""
    arr = np.full((H, W), 128, np.uint8)
    for key, value in (("add", 255), ("remove", 0)):
        if key in boxes:
            x0, y0, x1, y1 = boxes[key]
            arr[y0:y1, x0:x1] = value
    return png_of(arr)


def read(data_or_path) -> np.ndarray:
    src = io.BytesIO(data_or_path) if isinstance(data_or_path, bytes) else data_or_path
    with Image.open(src) as im:
        return np.asarray(im.convert("L")).copy()


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("INKDEUS_PROJECTS", str(tmp_path / "projects"))
    monkeypatch.setattr("inkdeus.server.default_local_config", lambda: tmp_path / "none.toml")
    images, masks, parts = tmp_path / "images", tmp_path / "masks", tmp_path / "parts"
    synth.save(images / "a.png", synth.flat(W, H, 120))
    synth.save(images / "b.png", synth.flat(W, H, 200))
    person = np.zeros((H, W), np.uint8)
    person[5:25, 5:35] = 255                                      # 人物: x 5〜35, y 5〜25
    hair = np.zeros((H, W), np.uint8)
    hair[5:12, 5:35] = 255                                        # 髪: 人物の上の帯
    skin = np.zeros((H, W), np.uint8)
    skin[12:20, 5:35] = 255                                       # 肌: その下の帯（y 20〜25 はどの部分でもない人物）
    synth.save(masks / "a.png", person)
    synth.save(parts / "hair" / "a.png", hair)
    synth.save(parts / "skin" / "a.png", skin)
    project, _ = create_project("book", images, masks, root=tmp_path / "projects", parts=parts)
    app = create_app(project.source, project.masks, project.out, project=project.info(), watch=False,
                     parts_dir=project.parts, local_config=tmp_path / "none.toml", config_dir=tmp_path / "config")
    originals = {p: p.read_bytes() for p in (masks / "a.png", parts / "hair" / "a.png", parts / "skin" / "a.png")}
    return {"tmp": tmp_path, "project": project, "app": app, "client": app.test_client(), "out": project.out,
            "originals": originals, "person": person, "hair": hair, "skin": skin}


def composed(client, target, name="a.png") -> np.ndarray:
    r = client.get(f"/api/maskedit-composed/{name}/{target}")
    assert r.status_code == 200, r.data
    return read(r.data)


def test_edits_are_stored_per_image_and_validated(env):
    client, out = env["client"], env["out"]
    listing = client.get("/api/maskedit/a.png").get_json()
    assert listing["targets"] == [] and listing["partNames"] == ["hair", "skin"]
    assert listing["values"] == {"add": 255, "remove": 0, "untouched": 128, "addMin": 192, "removeMax": 63}
    r = client.put("/api/maskedit/a.png/hair", data=edit(add=(5, 12, 20, 16)), headers={"X-Inkdeus-Client": "me"})
    assert r.status_code == 200 and (out / "a.maskedit" / "hair.png").is_file()
    assert client.put("/api/maskedit/a.png/person", data=edit(remove=(30, 5, 35, 25))).status_code == 200
    assert [t["target"] for t in client.get("/api/maskedit/a.png").get_json()["targets"]] == ["hair", "person"]
    assert client.get("/api/maskedit/a.png/hair").data == edit(add=(5, 12, 20, 16))
    # 対象は person か、部分マスクの置き場にある部分名だけ。PNG だけ
    assert client.put("/api/maskedit/a.png/nose", data=edit()).status_code == 400
    assert client.put("/api/maskedit/a.png/hair", data=b"not a png").status_code == 400
    assert client.get("/api/maskedit/a.png/skin").status_code == 404
    assert client.get("/api/maskedit/nope.png").status_code == 404
    # 画像の一覧に「手直しあり」が出る。バージョンには依存しない
    rows = {i["name"]: i for i in client.get("/api/images").get_json()["images"]}
    assert rows["a.png"]["maskEdits"] == ["hair", "person"] and rows["b.png"]["maskEdits"] == []
    assert client.get("/api/maskedit/a.png?version=whatever").get_json()["targets"][0]["target"] == "hair"
    # 元のマスクのファイルは変わらない
    assert all(p.read_bytes() == data for p, data in env["originals"].items())
    # 対象ごとの取り消し・全部の取り消し
    r = client.delete("/api/maskedit/a.png/hair").get_json()
    assert r["deleted"] == ["hair"] and [t["target"] for t in r["targets"]] == ["person"]
    r = client.delete("/api/maskedit/a.png/all-targets").get_json()
    assert r["deleted"] == ["person"] and r["targets"] == [] and not (out / "a.maskedit").exists()
    assert {i["name"]: i for i in client.get("/api/images").get_json()["images"]}["a.png"]["maskEdits"] == []


def test_composition_is_exclusive_and_stays_inside_the_person(env):
    client = env["client"]
    person, hair, skin = env["person"], env["hair"], env["skin"]
    assert (composed(client, "person") == person).all() and (composed(client, "hair") == hair).all()
    # 髪に塗る（肌だった所と、どの部分でもなかった所と、人物の外）
    client.put("/api/maskedit/a.png/hair", data=edit(add=(0, 10, 20, 23)))
    new_hair, new_skin = composed(client, "hair"), composed(client, "skin")
    assert (new_hair[10:23, 5:20] == 255).all()                   # 塗った範囲は髪になる
    assert (new_skin[12:20, 5:20] == 0).all() and (new_skin[12:20, 20:35] == 255).all()   # 元いた部分（肌）からは外れる
    assert (new_hair[10:23, 0:5] == 0).all()                      # 人物の外には塗れない
    assert not ((new_hair > 0) & (new_skin > 0)).any()            # 部分どうしは排他
    assert (new_hair[5:12, 5:35] == 255).all()                    # 元の髪はそのまま
    # 髪から消す → どの部分でもない人物の領域になる（人物のまま）
    client.put("/api/maskedit/a.png/hair", data=edit(remove=(25, 5, 35, 12)))
    assert (composed(client, "hair")[5:12, 25:35] == 0).all() and (composed(client, "skin")[5:12, 25:35] == 0).all()
    assert (composed(client, "person")[5:12, 25:35] == 255).all()
    # 人物から外す → 背景になり、そこにあった部分も外れる。人物に加える → そこは人物になる
    arr = np.full((H, W), 128, np.uint8)
    arr[5:25, 5:10] = 0
    arr[0:5, 10:20] = 255
    client.put("/api/maskedit/a.png/person", data=png_of(arr))
    new_person = composed(client, "person")
    assert (new_person[5:25, 5:10] == 0).all() and (new_person[0:5, 10:20] == 255).all()
    assert (new_person[5:25, 10:35] == 255).all() and (new_person[0:5, 0:10] == 0).all()
    assert (composed(client, "hair")[5:12, 5:10] == 0).all() and (composed(client, "skin")[12:20, 5:10] == 0).all()
    assert ((composed(client, "hair") > 0) <= (new_person > 0)).all()          # 部分は人物の内側だけ
    # 同じ画素を 2 つの部分に加えてあるときは、並びが後ろの部分（skin）が勝つ
    client.put("/api/maskedit/a.png/hair", data=edit(add=(20, 20, 30, 24)))
    client.put("/api/maskedit/a.png/skin", data=edit(add=(25, 20, 30, 24)))
    assert (composed(client, "skin")[20:24, 25:30] == 255).all() and (composed(client, "hair")[20:24, 25:30] == 0).all()
    assert (composed(client, "hair")[20:24, 20:25] == 255).all()
    # 途中の値（やわらかい筆の縁）は、192 以上が「加える」、63 以下が「外す」、その間は元のまま
    soft = np.full((H, W), 128, np.uint8)
    soft[13, 10:14] = (191, 192, 64, 63)
    client.put("/api/maskedit/a.png/skin", data=png_of(soft))
    client.delete("/api/maskedit/a.png/hair")
    client.delete("/api/maskedit/a.png/person")
    assert composed(client, "skin")[13, 10:14].tolist() == [255, 255, 255, 0]
    # 縮めて持っている手直し（長辺が違う）は、画像の大きさへ広げて重ねる
    small = np.full((H // 2, W // 2), 128, np.uint8)
    small[3:6, 3:6] = 255                                         # 原寸では x 6〜12, y 6〜12（髪の帯の中 → 肌に加える）
    client.put("/api/maskedit/a.png/skin", data=png_of(small))
    assert (composed(client, "skin")[6:12, 6:12] == 255).all() and (composed(client, "hair")[6:12, 6:12] == 0).all()
    # マスクの無い画像（全体が人物）: person を外すと、そこが背景になる
    assert client.get("/api/maskedit-composed/b.png/person").status_code == 404
    client.put("/api/maskedit/b.png/person", data=edit(remove=(0, 0, 10, 10)))
    solo = composed(client, "person", "b.png")
    assert (solo[0:10, 0:10] == 0).all() and (solo[10:, :] == 255).all()
    assert all(p.read_bytes() == data for p, data in env["originals"].items())


def test_tiles_backups_and_stale_exports(env):
    client, out, store = env["client"], env["out"], env["app"].config["INKDEUS_BACKUPS"]
    params = client.get("/api/params/a.png?resolve=1").get_json()["params"]
    assert client.put("/api/params/a.png", data=json.dumps(params)).status_code == 200
    client.post("/api/versions/a.png", data=json.dumps({"version": "v2"}))
    buf = io.BytesIO()
    Image.new("L", (W, H), 0).save(buf, "PNG")
    for version in ("", "v2"):
        assert client.post(f"/api/export/a.png?version={version}", data=buf.getvalue()).status_code == 200
    row = {i["name"]: i for i in client.get("/api/images").get_json()["images"]}["a.png"]
    assert [v["exportFresh"] for v in row["versions"]] == [True, True]
    # 1 筆で変わったタイルだけの保存（触っていない所は 128）
    q = env["app"].config["INKDEUS_SESSION"].hub.subscribe()
    head = json.dumps({"width": W, "height": H, "channels": 1, "encoding": "rle",
                       "tiles": [{"x": 0, "y": 6, "w": 8, "h": 1, "n": 2}]}).encode("utf-8")
    body = struct.pack("<I", len(head)) + head + bytes([8, 255])
    r = client.patch("/api/maskedit/a.png/hair", data=body, headers={"X-Inkdeus-Client": "me"})
    assert r.status_code == 200 and r.get_json()["deleted"] is False
    saved = read(out / "a.maskedit" / "hair.png")
    assert (saved[6, 0:8] == 255).all() and (saved == 128).sum() == W * H - 8
    events = [q.get_nowait() for _ in range(q.qsize())]
    assert [(e["type"], e["action"], e["targets"], e["source"]) for e in events] == [("maskedit", "patch", ["hair"], "me")]
    # 手直しは全バージョンで共通なので、どのバージョンの白黒版も「古い」になる
    import os
    import time

    os.utime(out / "a.maskedit" / "hair.png", (time.time() + 5, time.time() + 5))
    row = {i["name"]: i for i in client.get("/api/images").get_json()["images"]}["a.png"]
    assert [v["exportFresh"] for v in row["versions"]] == [False, False] and row["maskEdits"] == ["hair"]
    assert [v["exportFresh"] for v in client.get("/api/versions/a.png").get_json()["versions"]] == [False, False]
    # 上書き・削除の前に退避される。全部 128 に戻ったら、ファイルごと片付ける
    assert client.put("/api/maskedit/a.png/hair", data=edit(add=(0, 0, 4, 4))).status_code == 200
    kept = [e for e in store.entries("a.maskedit/hair.png") if not e.absent]
    assert len(kept) == 1 and (read(kept[0].path)[6, 0:8] == 255).all()
    head = json.dumps({"width": W, "height": H, "channels": 1, "encoding": "rle",
                       "tiles": [{"x": 0, "y": 0, "w": 4, "h": 4, "n": 2}]}).encode("utf-8")
    r = client.patch("/api/maskedit/a.png/hair", data=struct.pack("<I", len(head)) + head + bytes([16, 128]))
    assert r.get_json()["deleted"] is True and not (out / "a.maskedit").exists()
    assert len([e for e in store.entries("a.maskedit/hair.png") if not e.absent]) == 2
    # 「以前の状態に戻す」の時点にも入る（設定・消しゴムとペンと一緒に戻る）
    points = client.get("/api/backups/a.png").get_json()["points"]
    assert any("マスクの手直し（hair）" in p["what"] for p in points)
    newest = next(p for p in points if "マスクの手直し（hair）" in p["what"])
    restored = client.post("/api/backups/a.png", data=json.dumps({"at": newest["at"]})).get_json()
    assert "マスクの手直し（hair）" in restored["restored"] and (read(out / "a.maskedit" / "hair.png")[0:4, 0:4] == 255).all()
    events = [q.get_nowait() for _ in range(q.qsize())]
    assert ("maskedit", "restore", None) in [(e["type"], e.get("action"), e.get("source")) for e in events]


def test_export_command_writes_composed_masks(env, capsys):
    client, tmp = env["client"], env["tmp"]
    client.put("/api/maskedit/a.png/hair", data=edit(add=(0, 10, 20, 23)))
    client.put("/api/maskedit/a.png/person", data=edit(remove=(30, 5, 35, 25)))
    assert cli.main(["maskedit", "list", "book", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["images"] == [{"name": "a.png", "targets": ["hair", "person"]}]
    dest = tmp / "fixed"
    assert cli.main(["maskedit", "export", "book", "-o", str(dest)]) == 0
    text = capsys.readouterr().out
    assert "画像 2 枚（うち手直しあり 1 枚）/ 3 ファイル" in text
    assert sorted(p.relative_to(dest).as_posix() for p in dest.rglob("*.png")) == [
        "hair/a.png", "person/a.png", "skin/a.png"]                # b.png にはマスクも手直しも無い
    person, hair, skin = (read(dest / t / "a.png") for t in ("person", "hair", "skin"))
    assert Image.open(dest / "hair" / "a.png").size == (W, H) and set(np.unique(hair)) <= {0, 255}
    assert (person == composed(client, "person")).all() and (hair == composed(client, "hair")).all()
    assert (skin == composed(client, "skin")).all()
    assert not ((hair > 0) & (skin > 0)).any()                    # 排他
    assert ((hair > 0) <= (person > 0)).all() and ((skin > 0) <= (person > 0)).all()      # 人物の内側だけ
    assert (person[5:25, 30:35] == 0).all() and (hair[5:12, 30:35] == 0).all()
    # 書き出した物は、そのまま部分マスクの置き場・人物マスクのフォルダとして使える形（元のマスクは変えていない）
    assert maskedit.rows_in(env["out"] / "a.maskedit", ["hair", "skin"])[0]["target"] == "hair"
    assert all(p.read_bytes() == data for p, data in env["originals"].items())
    assert cli.main(["maskedit", "export", "book", "-o", str(env["project"].parts)]) == 1
    assert "指定できません" in capsys.readouterr().err
