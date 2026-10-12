"""設定の検証・移行・スタイル（サーバ側の schema.py）。定義は web/schema.json"""

from __future__ import annotations

import pytest

from inkdeus import schema


def test_defaults():
    p = schema.default_params()
    assert p["version"] == 2 and p["unit"] is None
    assert list(p) == ["version", "unit", "input", "lines", "regions", "watermark", "output"]
    person = p["regions"]["person"]
    assert [(l["threshold"], l["angle"]) for l in person["layers"]] == [(0.72, 45), (0.52, 135), (0.38, 0), (0.27, 90)]
    assert person["levels"] == {"brightness": 0, "contrast": 1, "gamma": 1}
    assert p["regions"]["background"]["linked"] is True
    assert p["watermark"]["enabled"] is True
    assert schema.default_params(owner=True)["watermark"]["enabled"] is False
    # 整えた物をもう一度整えても変わらない
    assert schema.normalize(p, strict=True)[0] == p


def test_strict_validation_reports_every_problem():
    bad = {"version": 2, "mystery": 1, "lines": {"sigma": 99, "phi": "x", "bogus": True},
           "regions": {"person": {"layers": [{"type": "dots", "extra": 1}], "levels": {"gamma": 0}},
                       "background": {"mode": "black"}, "sky": {}}}
    with pytest.raises(schema.ValidationError) as e:
        schema.normalize(bad, strict=True)
    text = "\n".join(e.value.problems)
    for needle in ("未知のキー: mystery", "lines.sigma", "lines.phi", "未知のキー: lines.bogus",
                   "regions.person.layers[0].type", "未知のキー: regions.person.layers[0].extra",
                   "regions.person.levels.gamma", "regions.background.mode", "未知のキー: regions.sky"):
        assert needle in text, text
    # strict でなければ、丸めたり既定値にしたりして読む（問題は一覧で返す）
    params, problems = schema.normalize(bad)
    assert problems and params["lines"]["sigma"] == 4 and params["regions"]["background"]["mode"] == "keep"


def test_too_many_layers_is_an_error():
    layers = [{"type": "hatch"}] * 9
    with pytest.raises(schema.ValidationError):
        schema.normalize({"regions": {"person": {"layers": layers}}}, strict=True)


def test_migrate_old_format():
    old = {"version": 1, "unit": 2, "solid": {"enabled": False, "threshold": 0.3},
           "layers": [{"type": "hatch", "threshold": 0.6, "angle": 10}],
           "background": {"mode": "lines", "grow": 5, "silhouetteWidth": 1, "lineStrength": 0.3},
           "watermark": {"text": "x"}}
    params, problems = schema.normalize(old, strict=True)
    assert problems == [] and params["version"] == 2 and params["unit"] == 2
    assert "layers" not in params and "solid" not in params and "background" not in params
    assert params["regions"]["person"]["solid"] == {"enabled": False, "threshold": 0.3, "soft": 0.0}
    assert [l["angle"] for l in params["regions"]["person"]["layers"]] == [10]
    bg = params["regions"]["background"]
    assert (bg["mode"], bg["grow"], bg["lineStrength"], bg["linked"]) == ("lines", 5, 0.3, True)
    assert params["watermark"]["text"] == "x"


def test_merge_patch_rfc7396():
    base = {"a": {"b": 1, "c": 2}, "list": [1, 2, 3], "keep": True}
    assert schema.merge_patch(base, {"a": {"b": 9}}) == {"a": {"b": 9, "c": 2}, "list": [1, 2, 3], "keep": True}
    assert schema.merge_patch(base, {"a": {"c": None}})["a"] == {"b": 1}          # null は削除
    assert schema.merge_patch(base, {"list": [7]})["list"] == [7]                 # 配列は丸ごと置換
    assert schema.merge_patch(base, {"a": 5})["a"] == 5
    assert base == {"a": {"b": 1, "c": 2}, "list": [1, 2, 3], "keep": True}       # 元は変えない
    # 設定に当てる: 背景の明るさだけ変わり、ほかは残る。削除したキーは既定値に戻る
    p = schema.default_params()
    patched, _ = schema.normalize(schema.merge_patch(p, {
        "regions": {"background": {"linked": False, "levels": {"brightness": 0.25}}},
        "lines": {"sigma": None}}), strict=True)
    assert patched["regions"]["background"]["levels"] == {"brightness": 0.25, "contrast": 1, "gamma": 1}
    assert patched["regions"]["person"] == p["regions"]["person"]
    assert patched["lines"]["sigma"] == p["lines"]["sigma"]
    one = schema.merge_patch(p, {"regions": {"person": {"layers": [{"type": "hatch", "angle": 30}]}}})
    assert len(schema.normalize(one, strict=True)[0]["regions"]["person"]["layers"]) == 1


def test_set_path():
    p = schema.default_params()
    q = schema.set_path(p, "regions.person.layers.1.angle", 100)
    assert q["regions"]["person"]["layers"][1]["angle"] == 100 and p["regions"]["person"]["layers"][1]["angle"] == 135
    assert schema.set_path(p, "regions.background.levels.brightness", 0.25)["regions"]["background"]["levels"]["brightness"] == 0.25
    for key in ("regions.person.layers.9.angle", "regions.nowhere.x.y", "lines.sigma.deep", ""):
        with pytest.raises(schema.ValidationError):
            schema.set_path(p, key, 1)
    with pytest.raises(schema.ValidationError):           # 未知の末端キーは検証で落ちる
        schema.normalize(schema.set_path(p, "lines.nope", 1), strict=True)


@pytest.mark.parametrize("name,key", [
    ("hatch1", "hatch1"), ("ハッチ(1)", "hatch1"), ("ハッチ（1）", "hatch1"), ("ハッチ 1", "hatch1"),
    ("クロスハッチ極細", "crosshatch-ultrafine"), ("CrossHatch-UltraFine", "crosshatch-ultrafine"),
    ("クロスハッチ細", "crosshatch-fine"), ("重ねがけ4段", "crosshatch"), ("線だけ", "lines-only"),
    ("白", "white"), ("単層の細ハッチ", "hatch1-fine"),
])
def test_style_aliases(name, key):
    assert schema.resolve_style(name) == key


def test_style_names_are_unambiguous():
    seen: dict[str, str] = {}
    for key, style in schema.load()["styles"].items():
        for n in [key, style["label"], *style["aliases"]]:
            k = schema._style_key(n)
            assert seen.setdefault(k, key) == key, f"{n} が {seen[k]} と {key} の両方に当たる"
    assert schema.resolve_style("存在しない") is None
    assert {s["name"] for s in schema.list_styles()} >= {
        "hatch1", "hatch1-fine", "crosshatch", "crosshatch-fine", "crosshatch-ultrafine", "lines-only", "white"}


def test_apply_style_per_region():
    p = schema.default_params()
    q = schema.apply_style(p, "ハッチ(1)", "background")
    bg = q["regions"]["background"]
    assert bg["linked"] is False and bg["mode"] == "keep" and len(bg["layers"]) == 1
    assert q["regions"]["person"] == p["regions"]["person"]
    q = schema.apply_style(q, "クロスハッチ極細", "person")
    person = q["regions"]["person"]
    assert [l["spacing"] for l in person["layers"]] == [1.0] * 4
    assert [l["angle"] for l in person["layers"]] == [45, 135, 0, 90]
    assert len(q["regions"]["background"]["layers"]) == 1
    assert schema.normalize(q, strict=True)[0] == q
    assert schema.apply_style(p, "white", "background")["regions"]["background"]["mode"] == "white"
    assert schema.apply_style(p, "lines-only", "background")["regions"]["background"]["mode"] == "lines"
    both = schema.apply_style(p, "hatch1-fine", "both")
    assert len(both["regions"]["person"]["layers"]) == 1 and len(both["regions"]["background"]["layers"]) == 1
    with pytest.raises(schema.ValidationError):
        schema.apply_style(p, "nope", "person")
    with pytest.raises(schema.ValidationError):
        schema.apply_style(p, "hatch1", "sky")
