# inkdeus

A browser-based editor that turns one color illustration into a black-and-white, manga-style page
(line art + solid blacks + layered hatching) with hand-tunable sliders. All rendering runs in WebGL2,
so the preview and the exported PNG are the same pixels.

## これは何か

カラーのイラスト1枚を、白黒の漫画原稿風（線画＋ベタ＋ハッチングの重ねがけ）に手で調整して書き出すエディタです。

- 描画はすべてブラウザの WebGL2。プレビューと書き出しは同じ結果になります。
- トーンは「層」を重ねて作ります。各層は「平滑化した輝度がしきい値未満の所」に平行線を引くので、
  暗い所ほど多くの層が重なり、影の中にも段階的な陰影が出ます。
- 人物マスク（白=人物の PNG）があれば、背景を「白」「線だけ」に切り替えられます。
- ソース公開（非商用ライセンス）のソフトウェアです。

## 起動

### サーバあり（おすすめ）

[uv](https://docs.astral.sh/uv/) が必要です。

```bash
uv sync
uv run inkdeus <画像ファイル or 画像フォルダ> [--mask-dir DIR] [--out-dir DIR] [--port 8770] [--host 127.0.0.1]
```

表示された URL（既定 `http://127.0.0.1:8770/`）を Chrome / Edge などで開きます。

- `--mask-dir`: `<画像と同じ名前>.png` を人物マスクとして読みます（8bit、白=人物）。
- `--out-dir`: 書き出し先（既定は `<入力フォルダ>/inkdeus_out`）。`<名前>.png` と、設定の `<名前>.inkdeus.json` が入ります。
- 設定は変更のたびに画像ごとに自動保存されます。入力画像は書き換えません。

サーバは静的ファイルの配信とファイルの読み書きだけを行い、描画には関わりません。

### サーバなし

`web/` を任意の静的サーバで配信すればそのまま動きます（ES Modules のため `file://` 直開きは不可）。

```bash
python3 -m http.server 8000 --directory web
```

画像・マスク・設定JSON はドラッグ＆ドロップかボタンで読み込み、書き出しと設定の保存はダウンロードになります。
ファイル名に `mask` を含む画像はマスクとして扱います。

### 試してみる

```bash
uv run python tests/synth.py demo_images
uv run inkdeus demo_images/images --mask-dir demo_images/masks
```

## 操作の要点

- プレビュー: ホイールで拡大縮小、ドラッグで移動、「全体表示」「100%」。
  スペースキー（または「元画像」ボタン）を押している間は元画像を表示。「比較」で左右分割（仕切りをドラッグ）。
- スライダー: 数値欄に直接入力できます。ラベルかスライダーをダブルクリックで既定値に戻ります。
- 取り消し / やり直し: Ctrl+Z / Ctrl+Y。
- トーン層: 追加・複製・削除・並べ替え・表示のオン/オフ・ソロ表示。プリセット「重ねがけ4段」「単層の細ハッチ」「網点なし・線画のみ」。
- 長さの値は「基準px」です。実際の描画では `unit`（既定 = 画像の幅 / 1280）倍されるので、高解像度の画像でも同じ見た目になります。
- 「PNG を書き出す」でフル解像度の 8bit グレースケール PNG を保存します（「純2値」で白と黒だけ）。

## 透かし

書き出し画像とプレビューには文字の透かしが入ります（文字列・位置・大きさ・濃さは変更可。空の文字列や、下限より小さく・薄くはできません）。

サーバを所有者モードで起動すると「透かしを入れる」スイッチが出ます（既定はオフ）。
次のいずれかで所有者モードになります: 起動引数 `--owner` / 環境変数 `INKDEUS_OWNER=1` /
リポジトリ直下の `inkdeus.local.toml`（`inkdeus.local.toml.example` をコピー。`owner = true`）。

## 設定JSON

```json
{"version": 1, "unit": null, "input": {}, "lines": {}, "solid": {}, "layers": [], "background": {}, "watermark": {}, "output": {}}
```

欠けたキーは既定値で補い、未知のキーや使えない値は警告を表示します。定義の元は `web/js/schema.js` です。

| キー | 既定 | 内容 |
| --- | --- | --- |
| `unit` | `null` | 基準px に掛ける倍率。`null` = 画像の幅 / 1280 |
| `input.autoLevel` | `true` | 輝度の 1〜99 パーセンタイルでのレベル補正 |
| `input.smoothRadius` | `2.0` | トーン分け用の平滑化の半径（基準px、0 で無効） |
| `input.smoothStrength` | `0.1` | 平滑化の強さ（輝度差の許容幅、0 で無効） |
| `lines.enabled` | `true` | 線画（XDoG）を描く |
| `lines.sigma` | `0.8` | ガウスの σ（基準px） |
| `lines.tau` / `epsilon` / `phi` | `0.98` / `-0.01` / `100` | XDoG の τ・しきい値 ε・鋭さ φ |
| `lines.strength` | `1.0` | 線の濃さ |
| `solid.enabled` / `threshold` / `soft` | `true` / `0.15` / `0` | ベタ。しきい値未満を黒に。`soft` は境界のぼかし幅 |
| `layers[].type` | `"hatch"` | 層の種類（現在は `hatch` のみ） |
| `layers[].enabled` | `true` | 表示 |
| `layers[].threshold` / `soft` | `0.5` / `0.03` | 輝度がこれ未満の所に描く / 境界のぼかし幅（線を細らせて表現） |
| `layers[].angle` | `45` | 線の角度（°。0=水平、45=右上がり、90=垂直） |
| `layers[].spacing` / `width` | `2.0` / `0.5` | 線の間隔 / 太さ（基準px） |
| `layers[].widthByDarkness` | `0` | 0=太さ一定、1=しきい値付近は細く暗いほど太く |
| `layers[].opacity` / `phase` | `1.0` / `0` | 濃さ / 位相ずらし（間隔に対する割合） |
| `background.mode` | `"keep"` | `keep`=そのまま / `white`=白 / `lines`=線だけ（マスクが必要） |
| `background.grow` | `3.0` | マスクの広げ（正）/ 縮め（負）（基準px） |
| `background.silhouetteWidth` | `1.5` | `white` のときのシルエット線の太さ（基準px） |
| `background.lineStrength` | `0.6` | `lines` のときの背景の線の濃さ |
| `watermark.enabled` | `true` | 透かし（所有者モード以外では常にオン。所有者モードの既定は `false`） |
| `watermark.text` / `position` | `"inkdeus"` / `"br"` | 文字列 / 位置（`br` `bl` `tr` `tl` `center` `tile`） |
| `watermark.size` / `opacity` | `14` / `0.35` | 大きさ（基準px、下限 8）/ 濃さ（下限 0.15） |
| `output.binary` | `false` | 純2値（0 と 255 だけ） |

層は最大 8。既定は「重ねがけ4段」（しきい値 0.72 / 0.52 / 0.38 / 0.27、角度 45° / 135° / 0° / 90°）とベタ 0.15 です。

## 開発

```bash
uv sync
uv run playwright install chromium     # 描画テスト用（初回だけ）
uv run pytest
```

描画テストはヘッドレス Chromium で合成画像を描き、出力画素を数値で検証します。テスト画像はすべてスクリプトで生成します。

## License

PolyForm Noncommercial License 1.0.0（非商用のみ）。全文は [LICENSE](LICENSE) を参照してください。

Required Notice: Copyright (c) 2026 andam0202 (https://github.com/andam0202/inkdeus)
