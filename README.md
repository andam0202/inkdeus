# inkdeus

A browser-based editor that turns one color illustration into a black-and-white, manga-style page
(line art + solid blacks + layered hatching) with hand-tunable sliders. All rendering runs in WebGL2,
so the preview and the exported PNG are the same pixels.

## これは何か

カラーのイラスト1枚を、白黒の漫画原稿風（線画＋ベタ＋ハッチングの重ねがけ）に手で調整して書き出すエディタです。

- 描画はすべてブラウザの WebGL2。プレビューと書き出しは同じ結果になります。
- トーンは「層」を重ねて作ります。各層は「平滑化した輝度がしきい値未満の所」に平行線を引くので、
  暗い所ほど多くの層が重なり、影の中にも段階的な陰影が出ます。
- 人物マスク（白=人物の PNG）があれば、人物と背景で別々の設定（明るさ補正・ベタ・層）を持てます。背景を「白」「線だけ」にもできます。
- 稼働中のエディタを外のコマンド（`inkdeus ctl`）から操作でき、変更は開いている画面にすぐ反映されます。
- ソース公開（非商用ライセンス）のソフトウェアです。

## 起動

[uv](https://docs.astral.sh/uv/) が必要です。最初に `uv sync` を実行してください。
高画質化（拡大）や人物マスクの作成は inkdeus の外で済ませた物を渡します（inkdeus 自体はそれらを行いません）。

### プロジェクトを作って開く（おすすめ）

プロジェクトは「元画像フォルダ・マスクフォルダ・出力先」をまとめた1つのディレクトリです。
元画像とマスクは既定では**参照するだけ**でコピーしません（`--copy` でプロジェクト内にコピー）。

```bash
uv run inkdeus new <名前> --images <画像フォルダ> [--masks <マスクフォルダ>] [--copy] [--note "メモ"]
uv run inkdeus open <名前 or プロジェクトのパス> [--port 8770] [--owner]
uv run inkdeus projects [--json]        # 一覧（画像・マスク・設定済み・書き出し済みの枚数）
```

- 置き場は既定で `~/inkdeus-projects`。環境変数 `INKDEUS_PROJECTS` か `inkdeus.local.toml` の `projects_root` で変えられます。
- プロジェクトの中身は `inkdeus.project.toml`（name / created / source / masks / out / note）と `out/`（設定JSON と書き出した PNG）。
- 既にあるプロジェクトの参照先を変えるときは `inkdeus new <名前> --images <dir> --force`。

表示された URL（既定 `http://127.0.0.1:8770/`）を Chrome / Edge などで開きます。

### プロジェクトなしで直接開く

```bash
uv run inkdeus run <画像ファイル or 画像フォルダ> [--mask-dir DIR] [--out-dir DIR] [--port 8770] [--host 127.0.0.1]
```

`inkdeus <実在するパス>` は `run` の省略形です。

- `--mask-dir`: `<画像と同じ名前>.png` を人物マスクとして読みます（8bit、白=人物）。
- `--out-dir`: 書き出し先（既定は `<入力フォルダ>/inkdeus_out`）。`<名前>.png` と、設定の `<名前>.inkdeus.json` が入ります。
- 設定は変更のたびに画像ごとに自動保存されます。入力画像は書き換えません。

サーバは静的ファイルの配信とファイルの読み書き（と後述の外部操作の中継）だけを行い、描画には関わりません。

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
uv run inkdeus run demo_images/images --mask-dir demo_images/masks
```

## 操作の要点

- プレビュー: ホイールで拡大縮小、ドラッグで移動、「全体表示」「100%」。
  スペースキー（または「元画像」ボタン）を押している間は元画像を表示。「比較」で左右分割（仕切りをドラッグ）。
- スライダー: 数値欄に直接入力できます。ラベルかスライダーをダブルクリックで既定値に戻ります。
- 取り消し / やり直し: Ctrl+Z / Ctrl+Y。
- トーン: 「人物」「背景」のタブで、領域ごとに 明るさ補正・ベタ・層 を持てます（マスクが無い画像は全体が人物扱い）。
  背景は最初は人物と同じ設定で描かれ、「背景だけ別の設定にする」で切り離せます。背景が暗くて黒だらけになる画像は、背景の明るさ補正を上げると救えます。
- 層: 追加・複製・削除・並べ替え・表示のオン/オフ・ソロ表示。名前付きスタイル（重ねがけ4段、単層の細ハッチ、線画のみ など）を領域ごとに適用できます。
- 長さの値は「基準px」です。実際の描画では `unit`（既定 = 画像の幅 / 1280）倍されるので、高解像度の画像でも同じ見た目になります。
- 「PNG を書き出す」でフル解像度の 8bit グレースケール PNG を保存します（「純2値」で白と黒だけ）。

## 外部からの操作（ctl）

稼働中のサーバには `inkdeus ctl` で外から指示できます。設定を変えると、開いているブラウザにすぐ反映されます
（設定ファイル `<out>/<名前>.inkdeus.json` をエディタで直接書き換えても同じです）。出力は JSON です。

```bash
uv run inkdeus ctl state                                   # 開いている画像・プロジェクト・フォルダ
uv run inkdeus ctl get [--name N]                          # 設定JSON
uv run inkdeus ctl presets                                 # 名前付きスタイルの一覧
uv run inkdeus ctl preset "ハッチ(1)" --region background   # スタイルを領域に適用（person / background / both）
uv run inkdeus ctl set regions.background.levels.brightness 0.25
uv run inkdeus ctl patch '{"regions": {"person": {"solid": {"threshold": 0.1}}}}'
uv run inkdeus ctl stats --wait                            # 描画結果の統計（いまの設定に対する物が届くまで待つ）
uv run inkdeus ctl open <画像名>                           # ブラウザで開く画像を切り替える
uv run inkdeus ctl export                                  # ブラウザに書き出しを指示（保存先が返る）
```

- `--name` を省くと「いまブラウザで開いている画像」が対象です。`--port` は既定 8770。
- `patch` は JSON Merge Patch (RFC 7396) です。配列（`layers`）は丸ごと置換になるので、1層だけ変えるときは `set regions.person.layers.0.width 0.4` のように番号で指します。
- 範囲外の値や未知のキーは非0終了になり、何も変わりません。
- `stats` は黒率・平均の濃さ・ベタや各層の面積率・入力の輝度の分布を、全体／人物／背景ごとに返します（どの設定に対する統計かを示す `rev` つき）。画像を見ずに数値で調整するためのものです。
- ブラウザを開いていなくても設定ファイルは更新され、次に開いたとき反映されます（統計と書き出しはブラウザが必要です）。

Claude Code 用のスキル（`.claude/skills/inkdeus/SKILL.md`）に、起動から「言葉での依頼 → ctl での調整 → 統計での確認」までの手順をまとめてあります。

## 透かし

書き出し画像とプレビューには文字の透かしが入ります（文字列・位置・大きさ・濃さは変更可。空の文字列や、下限より小さく・薄くはできません）。

サーバを所有者モードで起動すると「透かしを入れる」スイッチが出ます（既定はオフ）。
次のいずれかで所有者モードになります: 起動引数 `--owner` / 環境変数 `INKDEUS_OWNER=1` /
リポジトリ直下の `inkdeus.local.toml`（`inkdeus.local.toml.example` をコピー。`owner = true`）。

## 設定JSON

```json
{"version": 2, "unit": null, "input": {}, "lines": {},
 "regions": {"person": {"levels": {}, "solid": {}, "layers": []},
             "background": {"mode": "keep", "linked": true, "levels": {}, "solid": {}, "layers": []}},
 "watermark": {}, "output": {}}
```

欠けたキーは既定値で補い、未知のキーや使えない値は警告を表示します。旧形式（version 1: トップレベルに `solid` / `layers` / `background`）は読み込み時に移行します。
キー・既定値・値域・名前付きスタイルの定義の元は `web/schema.json` です（ブラウザとサーバの両方がこれを読みます）。

| キー | 既定 | 内容 |
| --- | --- | --- |
| `unit` | `null` | 基準px に掛ける倍率。`null` = 画像の幅 / 1280 |
| `input.autoLevel` | `true` | 輝度の 1〜99 パーセンタイルでのレベル補正 |
| `input.smoothRadius` / `smoothStrength` | `2.0` / `0.1` | トーン分け用の平滑化の半径（基準px）/ 強さ。0 で無効 |
| `lines.enabled` / `strength` | `true` / `1.0` | 線画（XDoG）を描く / 濃さ |
| `lines.sigma` | `0.8` | ガウスの σ（基準px） |
| `lines.tau` / `epsilon` / `phi` | `0.98` / `-0.01` / `100` | XDoG の τ・しきい値 ε・鋭さ φ |
| `regions.<領域>.levels.brightness` / `contrast` / `gamma` | `0` / `1` / `1` | その領域のトーン分けに使う輝度の補正（-1〜1 / 0〜3 / 0.2〜5） |
| `regions.<領域>.solid.enabled` / `threshold` / `soft` | `true` / `0.15` / `0` | ベタ。しきい値未満を黒に。`soft` は境界のぼかし幅 |
| `regions.<領域>.layers[].type` / `enabled` | `"hatch"` / `true` | 層の種類（現在は `hatch` のみ）/ 表示 |
| `layers[].threshold` / `soft` | `0.5` / `0.03` | 輝度がこれ未満の所に描く / 境界のぼかし幅（線を細らせて表現） |
| `layers[].angle` | `45` | 線の角度（°。0=水平、45=右上がり、90=垂直） |
| `layers[].spacing` / `width` | `2.0` / `0.5` | 線の間隔 / 太さ（基準px） |
| `layers[].widthByDarkness` | `0` | 0=太さ一定、1=しきい値付近は細く暗いほど太く |
| `layers[].opacity` / `phase` | `1.0` / `0` | 濃さ / 位相ずらし（間隔に対する割合） |
| `regions.background.mode` | `"keep"` | `keep`=層で描く / `white`=白 / `lines`=線だけ（マスクが必要） |
| `regions.background.linked` | `true` | `true` の間、背景は人物と同じ levels / solid / layers で描く |
| `regions.background.grow` | `3.0` | マスクの広げ（正）/ 縮め（負）（基準px） |
| `regions.background.silhouetteWidth` | `1.5` | `white` のときのシルエット線の太さ（基準px） |
| `regions.background.lineStrength` | `0.6` | `lines` のときの背景の線の濃さ |
| `watermark.enabled` | `true` | 透かし（所有者モード以外では常にオン。所有者モードの既定は `false`） |
| `watermark.text` / `position` | `"inkdeus"` / `"br"` | 文字列 / 位置（`br` `bl` `tr` `tl` `center` `tile`） |
| `watermark.size` / `opacity` | `14` / `0.35` | 大きさ（基準px、下限 8）/ 濃さ（下限 0.15） |
| `output.binary` | `false` | 純2値（0 と 255 だけ） |

`<領域>` は `person`（人物。マスクの内側。マスクが無ければ全体）と `background`（背景）。層は領域ごとに最大 8。
既定は「重ねがけ4段」（しきい値 0.72 / 0.52 / 0.38 / 0.27、角度 45° / 135° / 0° / 90°）とベタ 0.15 です。

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
