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
- 書き出した画像を、別プロジェクトの漫画エディタへ持ち込んでページに組めます（後述「漫画の構成」）。
- GPL-3.0-or-later のフリーソフトウェアです。

## 起動

[uv](https://docs.astral.sh/uv/) が必要です。最初に `uv sync` を実行してください。
高画質化（拡大）や人物マスクの作成は inkdeus の外で済ませた物を渡します（inkdeus 自体はそれらを行いません）。

### プロジェクトを作って開く（おすすめ）

プロジェクトは「元画像フォルダ・マスクフォルダ・出力先」をまとめた1つのディレクトリです。
元画像とマスクは既定では**参照するだけ**でコピーしません（`--copy` でプロジェクト内にコピー）。

```bash
uv run inkdeus new <名前> --images <画像フォルダ> [--masks <マスクフォルダ>] [--copy] [--note "メモ"]
uv run inkdeus open <名前 or プロジェクトのパス> [--port 8770]
uv run inkdeus projects [--json]        # 一覧（画像・マスク・設定済み・書き出し済みの枚数）
```

- 置き場は既定で `~/inkdeus-projects`。環境変数 `INKDEUS_PROJECTS` か、リポジトリ直下の `inkdeus.local.toml`（`inkdeus.local.toml.example` をコピー）の `projects_root` で変えられます。
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
- 画像の一覧: 左端の「画像」パネルに全画像のサムネイルが並び、押すとその画像を開きます。名前と状態（未設定 / 未書き出し / 書き出しが古い）で絞り込め、
  各サムネイルに「設定」「書出」「古い」「マスク無」の印が付きます。サムネイルは「元画像 / 白黒版」を切り替えられ、右端のドラッグで幅を変えられます。
  `[` と `]` で前後の画像へ移ります（絞り込み中はその結果の中で）。
- スライダー: 数値欄に直接入力できます。ラベルかスライダーをダブルクリックで既定値に戻ります。
- 取り消し / やり直し: Ctrl+Z / Ctrl+Y。
- トーン: 「人物」「背景」のタブで、領域ごとに 明るさ補正・ベタ・層 を持てます（マスクが無い画像は全体が人物扱い）。
  背景は最初は人物と同じ設定で描かれ、「背景だけ別の設定にする」で切り離せます。背景が暗くて黒だらけになる画像は、背景の明るさ補正を上げると救えます。
- 層: 追加・複製・削除・並べ替え・表示のオン/オフ・ソロ表示。名前付きスタイル（重ねがけ4段、単層の細ハッチ、線画のみ など）を領域ごとに適用できます。
- 長さの値は「基準px」です。実際の描画では `unit`（既定 = 画像の幅 / 1280）倍されるので、高解像度の画像でも同じ見た目になります。
- 「PNG を書き出す」でフル解像度の 8bit グレースケール PNG を保存します（「純2値」で白と黒だけ）。
- 「変更を自動で書き出す（漫画の画面へ反映）」（既定オン）の間は、設定を変えて 1.5 秒ほど置くと PNG が自動で保存されます。
  白黒版がまだ無い画像は、開いた時点で書き出されます。ヘッダに「書き出し済み / 書き出し中… / 未反映の変更あり」と出ます。
- 「全画像を書き出す」で、プロジェクトの全画像を順に書き出します。各画像は**その画像に保存済みの設定**で、
  設定がまだ無い画像は**いま画面に出ている設定**で書き出します（その設定がその画像の設定として保存されます）。
  書き出し済みで設定・元画像がその後変わっていない画像は飛ばします（「すべて作り直す」で全部やり直し）。途中で中止できます。
- `http://127.0.0.1:8770/?image=<画像名>` で、その画像を開いた状態で起動します。

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
uv run inkdeus ctl export-all [--force] [--wait]           # 全画像の書き出しを指示（--status で進み具合、--abort で中止）
```

- `--name` を省くと「いまブラウザで開いている画像」が対象です。
- 接続先の `--port`（既定 8770）と `--host` は、サブコマンドの前でも後ろでも書けます（`inkdeus ctl --port 8771 state` と `inkdeus ctl state --port 8771` は同じ）。
- `patch` は JSON Merge Patch (RFC 7396) です。配列（`layers`）は丸ごと置換になるので、1層だけ変えるときは `set regions.person.layers.0.width 0.4` のように番号で指します。
- 範囲外の値や未知のキーは非0終了になり、何も変わりません。
- `stats` は黒率・平均の濃さ・ベタや各層の面積率・入力の輝度の分布を、全体／人物／背景ごとに返します（どの設定に対する統計かを示す `rev` つき）。画像を見ずに数値で調整するためのものです。
- ブラウザを開いていなくても設定ファイルは更新され、次に開いたとき反映されます（統計と書き出しはブラウザが必要です）。
- `export-all` は白黒化の画面を開いているブラウザが実行します。`--wait` を付けると終わるまで待って件数（書き出し / 飛ばし / 失敗）を返します。
  進み具合は `ctl export-all --status` と `ctl state` の `batch` で見られます。

Claude Code 用のスキル（`.claude/skills/inkdeus/SKILL.md`）に、起動から「言葉での依頼 → ctl での調整 → 統計での確認」までの手順をまとめてあります。

## 漫画の構成（Manga Editor Desu! との連携）

書き出した白黒の画像を、コマ割り・吹き出し・文字入れのできる漫画エディタへそのまま持ち込んで、ページに組めます。

漫画エディタは inkdeus の一部ではありません。**new-sankaku 氏による別プロジェクト「Manga Editor Desu! Pro Edition」**
（<https://github.com/new-sankaku/manga-editor-desu>、GPL-3.0）を使います。inkdeus のリポジトリには含まれておらず、
次のコマンドで利用者のPCへ取得します（inkdeus はこのプロジェクトの公式の配布元ではありません）。

```bash
uv run inkdeus manga install            # 取得（git が必要。~/.cache/inkdeus/manga-editor-desu/<コミット>/ に置く）
uv run inkdeus manga status             # 導入済みの版と場所
uv run inkdeus open <プロジェクト>      # 起動して、ブラウザで http://127.0.0.1:8770/manga/ を開く
```

- 取得する版は、動作を確かめてあるコミット `04e0cf3de1f3677682f6d1831f2713db70e441ba` に固定しています（`--ref <コミット>` で変更可。別の版では連携が動かないことがあります）。
- 取得済みのクローンが手元にあれば `--from <クローンのフォルダ>` でネットワークなしに導入できます（クローンが指定の版でないときはエラー。`--allow-mismatch` でそのまま使う）。
- キャッシュの場所は環境変数 `INKDEUS_CACHE` で変えられます。未導入のまま `/manga/` を開くと、導入方法の案内が出ます。
- inkdeus は `/manga/` で配信するときに、ページ（index.html）だけを次のように書き換えます（取得したファイル自体は変えません）:
  **Google Analytics（アクセス解析）と service worker を無効にし、inkdeus の連携スクリプト（`web/manga-bridge/`）を差し込みます。**
- 漫画エディタのフォントや一部の UI（アイコンなど）は外部（Google Fonts、cdnjs、unpkg）から読み込まれるため、オフラインではそれらが欠けます。

### 素材パネル

`/manga/` の画面の右端に、inkdeus の「素材」パネルが出ます（「素材」のタブで開閉）。

- タブは素材の「ソース」です。既定で **元画像**（プロジェクトの元画像）と **白黒**（inkdeus で書き出した PNG）があります。名前での絞り込みもできます。
- サムネイルを押すと、**選択中のコマ**へその画像が入ります（コマの形に切り抜かれます）。コマは、キャンバス上のコマかレイヤー一覧のコマを押して選びます。サムネイルをコマへドラッグしても入れられます。
- コマへ渡す画像の大きさを選べます（長辺 1024 / 2048 / 4096 / 原寸。既定 2048）。画像は漫画データに埋め込まず、inkdeus のサーバ上の場所（URL）で持つので、漫画データは軽いままです。
  そのため、この漫画データは **inkdeus のサーバ経由（`/manga/`）で、同じプロジェクトを開いているとき**にだけ画像が表示されます。元の画像を動かしたり消したりすると表示できなくなります。
- 「プロジェクトに保存」「プロジェクトから開く」で、全ページをまとめた漫画データ（漫画エディタの保存形式 `.lz4`）をプロジェクトの `manga/` フォルダに保存・読み込みします。
  変更から 60 秒後とページを切り替えたときには `manga/autosave.lz4` へ自動保存され、次に開いたとき「前回の続きを開きますか」と聞かれます。漫画エディタ自身の保存・読み込み（ファイルのダウンロード）もそのまま使えます。
- 「このページを書き出す」「全ページを書き出す」で、ページの PNG を `manga/pages/<保存名>/001.png …` に保存します（大きさは漫画エディタの原稿サイズと DPI の設定で決まります）。
- プロジェクトなし（`inkdeus run`）で起動したときの保存先は `<出力フォルダ>/manga/` です。

### 白黒の調整をその場で反映する（2つのタブ）

白黒化の画面（`/`）と漫画の画面（`/manga/`）を**別々のタブで開いておく**と、`/` で調整した結果が `/manga/` にすぐ反映されます。

1. `/manga/` の素材パネルで、サムネイルの「調整」（またはキャンバスで画像を選んで「白黒を調整」）を押す。
   白黒化の画面がその画像を開いた状態で別のタブに出ます（2回目以降は同じタブが使い回されます）。
2. 白黒化の画面で設定を変える。少し置くと PNG が自動で書き出されます（「変更を自動で書き出す」がオンのとき。`ctl` での変更でも同じ）。
3. `/manga/` のタブでは、**いま開いているページでその白黒版を使っている絵がすべて新しい画像に替わります**
   （位置・大きさ・切り抜き・コマとの紐づけはそのまま。取り消しの履歴には積まれません）。ほかのページは、開いたときに最新の白黒版が出ます。

- 素材パネルの「元画像」タブの「入れる画像」で、コマへ入れる物を選べます: 「白黒版があれば白黒」（既定）/「常に元画像」。
- 選択中の絵は「白黒版に差し替え」「元画像に戻す」で切り替えられます（こちらは取り消せます）。白黒版がまだ無い画像は、「調整」で開けば自動で書き出されます。
- 「白黒版ができたら元画像を置き換える」（既定オフ）をオンにすると、元画像のまま置いてある絵が、白黒版が書き出された時点で白黒版に切り替わります。
- 素材パネルは左端をドラッグして幅を変えられます（ダブルクリックで既定の幅。幅は次回も復元）。画面が狭いときは「⧉」で、漫画エディタの画面を押しのけず重ねて表示できます。

### ローカルフォント

このPCにあるフォントファイル（.ttf / .otf など）を、漫画の文字で使えるようにできます。`inkdeus.local.toml`（全プロジェクト共通）か
プロジェクトの `inkdeus.project.toml` に書きます（両方あれば合算）。

```toml
[fonts]
dirs = ["<フォントのあるフォルダ>"]            # 複数可
include = ["Example-Mincho.ttf", "ExampleSerif-*.otf"]   # ファイル名の glob。省略すると dirs 内の全フォント
[fonts.names]                                  # 任意: ファイル名 → 表示名
"Example-Mincho.ttf" = "例の明朝"
```

- `uv run inkdeus fonts list [--project <名前>]` で、見つかったフォントと表示名を確かめられます。表示名はフォントファイルに書かれた名前（日本語名があれば日本語名）で、
  同じ名前が複数あるときはウェイト名を足して区別します。
- `/manga/` のフォント選択の先頭に「inkdeus」の見出しで並びます。本体は使ったときに初めて読み込まれます（選択の一覧には名前だけが出て、その書体での見本は出ません）。
  設定を書き足したときは、素材パネルの「フォント」の「↻」で読み直します。
- フォントファイルは inkdeus のリポジトリには含まれません。利用者が自分で用意した物を、自分のPCで使うだけです。
- 各フォントのライセンス（作品への利用・配布の可否など）は、利用者が確かめてください。
- 漫画データにはフォントの名前と取得先（inkdeus のサーバ上の場所）だけが記録されます。別のPCや漫画エディタ単体で開くと、そのフォントは「見つからない」扱いになります。

### 素材ソースの追加（擬音など）

画像の入ったフォルダを、素材のソースとしてプロジェクトに登録できます。

```bash
uv run inkdeus assets add <プロジェクト> --name 擬音 --path <フォルダ> --mode free --recursive
uv run inkdeus assets list <プロジェクト>
uv run inkdeus assets remove <プロジェクト> --name 擬音
```

- `--mode panel`（既定）はコマにはめ込む画像、`--mode free` は透過 PNG の擬音など、コマにはめずにページ上へ自由に置く素材です（押すとページ幅の 25% ほどの大きさで置かれます）。
- `--recursive` でサブフォルダも含め、フォルダ名で見出し分けします。
- 登録内容はプロジェクトの `inkdeus.project.toml` の `[[assets]]`（`name` / `path` / `mode` / `recursive`）に書かれます。
  `inkdeus.local.toml` に同じ形の `[[assets]]` を書くと、全プロジェクト共通のソースになります。稼働中に登録したときは、パネルの「↻」で一覧を読み直すと反映されます。

## 透かし

透かしは任意の機能で、既定ではオフです。「透かし」の「透かしを入れる」スイッチ（設定JSON の `watermark.enabled`）をオンにすると、
プレビューと書き出し画像に文字の透かしが入ります。文字列・位置・大きさ・濃さを変えられます
（文字列が空のときは `inkdeus` になり、大きさ・濃さには描画が壊れないための最小値があります）。

以前の版にあった起動引数 `--owner`、環境変数 `INKDEUS_OWNER`、`inkdeus.local.toml` の `owner` は廃止しました。指定してあってもエラーにはならず、無視されます。

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
| `watermark.enabled` | `false` | 透かしを入れる |
| `watermark.text` / `position` | `"inkdeus"` / `"br"` | 文字列 / 位置（`br` `bl` `tr` `tl` `center` `tile`） |
| `watermark.size` / `opacity` | `14` / `0.35` | 大きさ（基準px、最小 4）/ 濃さ（最小 0.05） |
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

漫画の構成の結合テスト（`tests/test_manga_bridge.py`）は実物の漫画エディタが必要です。固定している版のクローンの場所を
環境変数 `INKDEUS_MANGA_SRC` で渡します（無ければ skip されます）。

```bash
git clone https://github.com/new-sankaku/manga-editor-desu <どこか>
git -C <どこか> checkout 04e0cf3de1f3677682f6d1831f2713db70e441ba
INKDEUS_MANGA_SRC=<どこか> uv run pytest tests/test_manga_bridge.py
```

## License

Copyright (C) 2026 andam0202

This program is free software: you can redistribute it and/or modify it under the terms of the
GNU General Public License as published by the Free Software Foundation, either version 3 of the
License, or (at your option) any later version.

This program is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY; without
even the implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License along with this program
(see [LICENSE](LICENSE)). If not, see <https://www.gnu.org/licenses/>.

以前の版（最初の公開コミットまで）は PolyForm Noncommercial 1.0.0 で公開していました。この版以降は GPL-3.0-or-later です。
