---
name: inkdeus
description: inkdeus（カラーイラストを白黒の漫画原稿風にするエディタ）を起動し、開いている画像の線画・ベタ・ハッチングを言葉の依頼どおりに調整する。「inkdeus を起動して」「背景の黒が多い」「背景はハッチ(1)、人物はクロスハッチ極細に」「髪はベタに」などの依頼で使う。
---

# inkdeus を起動して、依頼どおりに調整する

inkdeus はブラウザで動くエディタで、設定ファイルを書き換えると開いている画面にすぐ反映される。
ここでは `inkdeus ctl` で設定を変え、**描画結果の統計（数値）**で結果を確かめながら調整する。

前提: 高画質化（拡大）や人物マスク・部分マスク（髪・肌・服など）の作成は inkdeus の外で済ませた物を渡す。inkdeus 自体はそれらを行わない。

## 守ること

- **画像そのものは開いて見ない**（入力画像・マスク・部分マスク・書き出した PNG・消しゴムやペンの PNG のどれも）。判断は `ctl stats` の数値で行い、見た目の良し悪しは利用者が画面で確かめる。
- **消しゴム・ペンは人が画面で行う物**。Claude Code は画像を見て描かない（`ctl` に描く操作は無い。できるのは、保存されている対象の一覧と消去だけ）。
- サーバの停止に `pkill -f` は使わない。`ss -ltnp | grep :8770` で PID を調べて `kill <PID>`。
- 設定の全体を書き直さない。変えたい項目だけを `ctl preset` / `ctl set` / `ctl patch` で変える。

## 1. 起動

まず既存のプロジェクトを確かめる。

```bash
uv run inkdeus projects
```

- 利用者が既存のプロジェクト名を言った → `uv run inkdeus open <名前>`
- 画像フォルダを示された → プロジェクトを作ってから開く

```bash
uv run inkdeus new <名前> --images <画像フォルダ> [--masks <マスクフォルダ>] [--parts <部分マスクの置き場>]
uv run inkdeus open <名前>
```

- プロジェクトを作らず一時的に開くだけなら `uv run inkdeus run <画像 or フォルダ> [--mask-dir DIR]`

サーバは終了しないので**バックグラウンドで起動**し、表示された URL（既定 `http://127.0.0.1:8770/`）を利用者に伝えてブラウザで開いてもらう。
別のポートで起動したときは、以下の `ctl` すべてに `--port <番号>` を付ける（サブコマンドの前でも後ろでもよい: `ctl --port 8771 state` / `ctl state --port 8771`）。

画面の左端には画像の一覧パネルがあり、利用者はそこから画像を選べる（`ctl open <画像名>` で切り替えても一覧の強調が追従する。
各画像の「設定あり・書き出し済み・書き出しが古い」は `GET /api/images` の `hasParams` / `hasExport` / `exportFresh` でも分かる）。

起動できたかは `uv run inkdeus ctl state` で確かめる（`clients` が 1 以上ならブラウザが開いている。`current` がいま開いている画像）。

### 漫画の構成（コマ割り・吹き出し）も使うとき

書き出した画像をページに組む画面（`/manga/`）は、別プロジェクトの漫画エディタ（Manga Editor Desu! Pro Edition、GPL-3.0）を
利用者のPCへ取得して使う。inkdeus のリポジトリには含まれていない。

```bash
uv run inkdeus manga status            # 導入済みか確かめる
uv run inkdeus manga install           # 未導入なら取得（初回だけ。git とネットワークが要る）
uv run inkdeus open <名前>             # 起動（既に起動中ならそのままでよい）
```

起動後、`http://127.0.0.1:8770/manga/`（別ポートならその番号）を利用者に開いてもらう。白黒化の画面のヘッダの「漫画を構成」からも行ける。
右端の「素材」パネルで、元画像・白黒（書き出した PNG）をコマへ入れ、プロジェクトの `manga/` へ保存・書き出しできる。

擬音などの素材フォルダを使いたいと言われたら、素材ソースとして登録する（稼働中でもよい。登録後、素材パネルの「↻」を押してもらう）。

```bash
uv run inkdeus assets add <名前> --name <ソース名> --path <フォルダ> --mode free --recursive
uv run inkdeus assets list <名前>
uv run inkdeus assets remove <名前> --name <ソース名>
```

`--mode panel`（既定）はコマにはめ込む画像、`--mode free` はコマにはめずページ上へ自由に置く素材。`--recursive` はサブフォルダも含める。
`/manga/` の画面の操作（コマ割り・画像の配置・吹き出し）は、いまは利用者が画面で行う（`ctl` からの操作には未対応）。

白黒の調整は `/manga/` にその場で反映される。白黒化の画面（`/`）と `/manga/` を別々のタブで開いておいてもらい、
`ctl set` / `ctl patch` / `ctl preset` で設定を変えると、白黒化の画面が PNG を自動で書き出し（「変更を自動で書き出す」がオンのとき）、
`/manga/` で使っている白黒版が新しい画像に替わる。対象の画像を白黒化の画面で開くには `ctl open <画像名>`（または `/?image=<画像名>`）。

### 漫画の文字にローカルフォントを足す

`inkdeus.local.toml`（共通）かプロジェクトの `inkdeus.project.toml` に `[fonts]`（`dirs` = フォルダの並び、`include` = ファイル名の glob、
`[fonts.names]` = ファイル名 → 表示名）を書く。`uv run inkdeus fonts list [--project <名前>]` で結果を確かめ、利用者に `/manga/` の素材パネルの
「フォント」の「↻」を押してもらう（フォント選択の先頭の「inkdeus」に並ぶ）。
フォントファイルはリポジトリに含まれず、利用者が自分で用意した物を自分のPCで使うだけ。ファイルをリポジトリへコピーしないこと。各フォントのライセンスは利用者が確かめる。

### 全画像をまとめて書き出す

```bash
uv run inkdeus ctl export-all --wait            # 終わるまで待って件数を返す（書き出し / 飛ばし / 失敗）
uv run inkdeus ctl export-all --force --wait    # 書き出し済みの画像も作り直す
uv run inkdeus ctl export-all --status          # 進み具合（ctl state の batch でも見られる）
uv run inkdeus ctl export-all --abort           # 中止
```

いま開いている画像フォルダの、全画像の全バージョンが対象（`--default-only` で既定のバージョンだけ、`--all-folders` で全フォルダ）。各画像はその画像に保存済みの設定で書き出される。設定の無い画像には「いまブラウザに出ている設定」が使われ、その画像の設定として保存される。
書き出し済みで設定が変わっていない画像は飛ばす。白黒化の画面を開いているブラウザが必要（`clients` が 1 以上）。枚数が多いと時間がかかるので、
長く待つときは `--wait` を付けずに始めて `--status` で確かめる。

## 2. 依頼を受けたときの進め方

1. **対象を確かめる**: `uv run inkdeus ctl state` → `current`（いま開いている画像）。別の画像なら `ctl open <画像名>`。
2. **現状を知る**: `uv run inkdeus ctl get`（設定）と `uv run inkdeus ctl stats`（統計）。
3. **変える**: 大きな方針は `ctl preset`、細かい値は `ctl set` / `ctl patch`。
4. **確かめる**: `uv run inkdeus ctl stats --wait`（いまの設定に対する統計が届くまで待つ）。`stats.rev` が直前の変更の `rev` と同じことを確かめてから数値を読む。
5. 目標に届くまで 3〜4 を数回くり返す。1回に変えるのは1〜2項目にして、どの変更が効いたか分かるようにする。
6. **報告**: 何をどの値からどの値に変えたか、統計がどう変わったか（例: 背景の黒率 0.61 → 0.22）。最終的な見た目の確認は利用者に頼む。

まだ設定が保存されていない画像をブラウザで開くと、直前に開いていた画像の設定を引き継ぐ（同じ調子で続けて作業するため）。開いた直後に `ctl get` で実際の値を確かめること。

### バージョン（同じ画像から、設定の違う白黒版を複数作る）

「背景を白にした版も作って」「背景をハッチにした別の版がほしい」のような依頼は、いまの設定を壊さず**バージョン**を足す。

```bash
uv run inkdeus ctl versions                              # 一覧（既定は name が ""）。ctl state の currentVersion がいま開いている物
uv run inkdeus ctl version new 白背景                    # 既定の設定を複製して作る（--from <元> で複製元を選ぶ）
uv run inkdeus ctl open <画像名> --version 白背景        # ブラウザをそのバージョンへ切り替える（統計を取るにはこれが要る）
uv run inkdeus ctl preset 白 --region background         # 以降の get / set / patch / preset / stats / export は、開いているバージョンが対象
uv run inkdeus ctl stats --wait
uv run inkdeus ctl version rename 白背景 bg-white
uv run inkdeus ctl version delete bg-white               # 設定と PNG を消す（既定は消せない）
```

- 名前は 1〜32 文字の英数字・かな漢字・`_` `-`。ファイルは `<out>/<名前>@<バージョン>.inkdeus.json` / `.png`（既定は従来どおり `@` なし）。
- どのコマンドも `--version <名前>` で対象を明示できる（既定のバージョンは `--version ""`）。開いていないバージョンは設定ファイルだけが変わり、統計は届かない。
- `/manga/` では各バージョンが別の素材として並ぶ。名前を変える・削除すると、漫画のページで使っている絵に影響する
  （削除 → 既定へ切り替わる。名前の変更 → 開いているページだけ追従）ので、利用者に確かめてから行う。

### 部分ごとの塗り分け（「髪はベタに」「肌は白く」など）

髪・肌・服などの部分ごとに、別の層やベタを当てられる。前提は**部分マスク**で、これは inkdeus の外で作った物を登録する
（inkdeus も Claude Code も作らない。無ければ、利用者に用意してもらう）。置き方は `<置き場>/<部分名>/<画像と同じ名前>.png`
（8bit・元画像と同じ縦横・255 = その部分）。部分名は置き場の直下のフォルダ名（6 個まで。`person` / `background` / `all` は使えない）。

```bash
uv run inkdeus parts set <プロジェクト> [--folder <フォルダ名>] --path <置き場>   # 登録（稼働中でもよい。画面は読み込み直してもらう）
uv run inkdeus parts list <プロジェクト> [--folder <フォルダ名>]                  # 部分名と、マスクのある画像の数
uv run inkdeus ctl folders                                                       # partNames = そのフォルダの部分名
```

その画像にマスクのある部分は、`ctl stats` の `stats.parts` と `GET /api/images` の各画像の `parts` で分かる。
部分は最初は人物と同じ設定で描かれ（`regions.<部分名>` が無い、または `linked` が `true`）、スタイルを当てるか `linked` を `false` にすると、
その部分だけの levels / solid / layers が有効になる。

例: 「髪はベタに」

```bash
uv run inkdeus ctl stats                                      # stats.parts に hair があること、output.hair の現状を見る
uv run inkdeus ctl preset ベタ --region hair                  # 髪を全面ベタに（その部分は人物との連動が切れる）
uv run inkdeus ctl set regions.hair.solid.lineKnockout 0.8    # ベタの中の線画を白で残す（髪の流れの線がつぶれないように）
uv run inkdeus ctl stats --wait                               # output.hair.solidRatio ≈ 1、blackRatio（白抜きの分だけ 1 より下がる）を確かめる
```

- 「肌は白く」→ `ctl preset 白抜き --region skin`（層もベタも無し。線画だけ残る）。少しだけ影を残すなら、スタイルは当てずに
  `ctl set regions.skin.linked false` のあと `regions.skin.levels.brightness` を上げる・`regions.skin.solid.enabled` を `false` にする。
- 部分に別の層を当てる → `ctl preset "ハッチ(1)" --region clothes` など。細かい値は `ctl set regions.clothes.layers.0.angle 20`。
- `ctl set regions.<部分名>.…` は、`linked` が `true` の間は絵に効かない（人物と同じ設定で描かれている）。先に `ctl preset` か
  `ctl set regions.<部分名>.linked false`（まだ設定の無い部分なら、いまの人物の設定を写した項目ができる）。
- 元へ戻す → `ctl set regions.<部分名>.linked true`。
- 部分名の打ち間違い（部分マスクの置き場に無い名前）は非0終了になり、何も変わらない。
- 部分マスクの出来（はみ出し・欠け）は数値では分からない。利用者に、部分のタブの「この領域を強調表示」で確かめてもらう
  （プレビューの Alt+クリックで、その場所の領域のタブへ切り替わる）。
- `lineKnockout` を上げると、ベタの輪郭のすぐ内側にも白い線が出ることがある。気になると言われたら値を下げる。

### 自分用スタイル（気に入った設定に名前を付けて使い回す）

「この髪の感じを保存しておいて」「さっきの『細かい影』をこの画像の服にも」のような依頼は、自分用スタイルで行う。
中身は、ある領域の**ベタと層**（`--with-levels` で明るさ補正も。背景から保存すると背景の描き方も）。

```bash
uv run inkdeus ctl presets                                          # 一覧。origin が builtin（組み込み）/ user（全プロジェクト共通）/ project（このプロジェクト専用）
uv run inkdeus ctl style save 細かい影 --region person             # 開いている画像の、その領域の設定を保存（既定は user）
uv run inkdeus ctl style save 髪の艶 --region hair --scope project --with-levels --description "ベタ＋白抜き 0.8"
uv run inkdeus ctl preset 細かい影 --region clothes                 # 別の画像・別の領域へ当てる（組み込みのスタイルと同じ）
uv run inkdeus ctl style rename 細かい影 細かい影2
uv run inkdeus ctl style delete 細かい影2 [--scope user|project]
uv run inkdeus ctl style export 髪の艶 > style.json                 # ほかのPC・プロジェクトへ持っていく
uv run inkdeus ctl style import style.json [--scope user|project] [--overwrite]
```

- 名前は 1〜32 文字の英数字・かな漢字・`_` `-` `(` `)`。組み込みのスタイルの名前・別名（`ベタ` `白` `hatch1` など）とは重ねられない（非0終了）。
- 保存先: user = `~/.config/inkdeus/styles.json`（`INKDEUS_CONFIG` で変更可）、project = プロジェクトのフォルダ直下の `styles.json`。
  同じ名前が両方にあれば project が使われる（`presets` で user 側に `shadowed: true`）。
- `style save` は同じ名前があれば上書きする（`replaced: true`）。利用者が作った物を上書き・削除するときは、先に確かめる。
- 当てても、`--with-levels` で保存した物でなければ明るさ補正（levels）は変わらない。背景の描き方が「白」「線だけ」の背景から保存した物は
  背景向け（`target: background`）で、人物・部分には当てられない。
- 利用者は画面でも同じことができる（各タブの「スタイルを選ぶ…」の下の「いまの設定をスタイルとして保存…」「自分用スタイルを管理…」）。
  `ctl` で保存・削除した物は、開いている画面の選択欄にもすぐ出る・消える。

### 消しゴム・ペン（利用者が画面で手を入れた所）

利用者は、プレビューの上のツールバーで、消しゴム（層ごと・ベタ・線画・すべて）とペン（黒・白）を使って仕上げる。これは**利用者が画面で行う作業**で、
`ctl` からは描けない。`ctl` でできるのは、保存されている対象の一覧と、消去だけ。

```bash
uv run inkdeus ctl paint list                      # 保存されている対象（targets）。省略時は、いまブラウザで開いている画像・バージョン
uv run inkdeus ctl paint clear lines               # その対象の消しゴムを消す（開いている画面にもすぐ反映。その画面の取り消し履歴は消える）
uv run inkdeus ctl paint clear all-targets         # 全部消す
```

- 対象は `layer-<層の id>`（id は `ctl get` の `regions.<領域>.layers[].id`）/ `solid-person` / `solid-background` / `solid-<部分名>` / `lines` / `all`（その場所を白にする）/ `pen`。
- 消すのは利用者の手作業を捨てる操作なので、**頼まれたときだけ**行う（消す前に `paint list` で何があるかを伝える）。
- `ctl stats` の数値は、消しゴム・ペンを反映した最終出力に対する物。`stats.paint.targets` / `penUsed` が空でなければ、
  設定を変えても数値が思ったほど動かないことがある（その場所が手で消されている・描き足されている）。
- 消しゴムは層の `id` で結び付いている。`ctl set regions.person.layers.0.width 0.4` のように1項目ずつ変えれば保たれる。
  `ctl preset`（スタイルの適用）は層を入れ替えるので、**前の層の消しゴムは保存先からも消える**。`paint list` に `layer-…` があるときは、先に利用者に確かめる。
  `ctl patch` で層を丸ごと書き直すときは、`id` を省けば同じ位置の層の id（と消しゴム）を引き継ぐ。
- 置き場所は `<out>/<名前>[@<バージョン>].paint/`（対象ごとの PNG と `index.json`）。バージョンの複製で写り、削除で消える。

### 画像フォルダが複数あるプロジェクト

プロジェクトは画像フォルダ（画像セット）をいくつも持てる。保存先は、既定のフォルダが `out/` 直下、足したフォルダが `out/@<名前>/`。

```bash
uv run inkdeus folders add <プロジェクト> --name <名前> --images <dir> [--masks <dir>]   # 足す（稼働中でもよい。画面は読み込み直してもらう）
uv run inkdeus folders list <プロジェクト>
uv run inkdeus ctl folders                                 # 稼働中のサーバのフォルダ一覧。current / ctl state の currentFolder がいま開いている物
uv run inkdeus ctl open <画像名> --folder <名前>           # ブラウザを別のフォルダの画像へ切り替える
```

`ctl` の各コマンドは、いまブラウザで開いているフォルダが対象。別のフォルダを指すときは `--folder <名前> --name <画像名>`（既定のフォルダは `default`）。
同じ名前の画像が別のフォルダにあっても別物なので、作業の前に `ctl state` で `currentFolder` と `current` を確かめる。
`folders remove` は登録を外すだけ（`--delete-out` は設定と書き出しを消すので、利用者に確かめてから）。

`--name <画像名>` を付けると、開いていない画像の設定も変えられる（ファイルだけ更新され、次に開いたとき反映される。統計はブラウザで開いている画像の分しか届かない）。

### コマンド

```bash
uv run inkdeus ctl state                       # 現在の画像・プロジェクト・フォルダ
uv run inkdeus ctl get                         # 設定JSON（params）と rev
uv run inkdeus ctl presets                     # 名前付きスタイルの一覧（組み込みと自分用）
uv run inkdeus ctl style save|delete|rename|export|import …   # 自分用スタイル（上の節）
uv run inkdeus ctl preset <スタイル> --region person|background|both|<部分名>
uv run inkdeus ctl set <ドット区切りのキー> <値>   # 例: regions.background.levels.brightness 0.25
uv run inkdeus ctl patch '<JSON Merge Patch>'  # 複数項目をまとめて。配列(layers)は丸ごと置換になる
uv run inkdeus ctl stats --wait                # 統計
uv run inkdeus ctl open <画像名> [--version V] # 開く画像（とバージョン）を切り替える
uv run inkdeus ctl export                      # ブラウザに PNG 書き出しを指示（保存先が返る）
uv run inkdeus ctl export-all [--force] [--default-only] [--wait]   # 全画像・全バージョンの書き出し（--status / --abort）
uv run inkdeus ctl versions / version new|rename|delete   # バージョン（上の節）
uv run inkdeus ctl paint list / paint clear <対象|all-targets>   # 消しゴム・ペンの一覧と消去（上の節。描くのは利用者）
```

値が範囲外・キーの打ち間違いなどは非0終了になり、何も変わらない（`problems` に理由）。
1層だけ変えるときは `ctl set regions.person.layers.0.width 0.4` のように番号で指す（`patch` だと配列全体の置換になる）。

### 名前付きスタイル

| 名前 | 別名の例 | 内容 |
| --- | --- | --- |
| `hatch1` | ハッチ(1)、ハッチ1 | 平行線1層 |
| `hatch1-fine` | 単層の細ハッチ | 平行線1層・間隔 1.5 |
| `crosshatch` | クロスハッチ、重ねがけ4段 | 4方向の重ねがけ・間隔 2.0（既定） |
| `crosshatch-fine` | クロスハッチ細 | 間隔 1.5 |
| `crosshatch-ultrafine` | クロスハッチ極細 | 間隔 1.0 |
| `lines-only` | 線だけ | トーンとベタ無し（背景に当てると「線だけ」） |
| `white` | 白 | 背景に当てると完全な白 |
| `solid-black` | ベタ、黒ベタ、塗りつぶし | その領域を全面ベタ（真っ黒）に。層なし |
| `blank` | 白抜き（人物・部分に当てる「白」「白抜き」はこれ） | 層もベタも無し。線画だけ残る |

スタイルは**層とベタ**（背景なら描き方も）を差し替える。明るさ補正（levels）は変えない。
背景・部分にスタイルを当てると、その領域は人物との連動（`linked`）が切れて独自の設定になる。
「白」「白抜き」は、背景に当てると `white`（完全な白）、人物・部分に当てると `blank`（線画だけ残す）になる。

## 3. 用語と設定キーの対応

設定は「人物」（`regions.person`。マスクの内側。マスクが無い画像は全体）と「背景」（`regions.background`）で別々に持てる。
部分マスクのある画像では、部分（`regions.hair` など）も別々に持てる（上の「部分ごとの塗り分け」）。
背景は `regions.background.linked` が `true` の間は人物と同じ設定で描かれる。**背景だけ変えるには `linked` を `false` にする**（背景へ `ctl preset` すると自動で切れる）。長さの値は「基準px」（画像の幅 1280px 相当での px）。

| 言い方 | キー | 向き |
| --- | --- | --- |
| 背景/人物が暗い・黒が多い | `regions.<領域>.levels.brightness`（-1〜1）、`gamma`（0.2〜5） | 上げると明るくなり、ベタと層の面積が減る |
| ベタ（真っ黒）が多い | `regions.<領域>.solid.threshold`（0〜1） | 下げるとベタが減る。`solid.enabled` を `false` で無し。1 で全面ベタ |
| ベタの中の線がつぶれる | `regions.<領域>.solid.lineKnockout`（0〜1） | 上げると、ベタで塗られた所の線画を白で描く |
| 影のメリハリ | `regions.<領域>.levels.contrast`（0〜3） | 上げると明暗がはっきり分かれる |
| ハッチの範囲が広い/狭い | `regions.<領域>.layers.<番号>.threshold` | 下げるとその層が描かれる範囲が狭くなる |
| 線が細い/太い（ハッチ） | `layers.<番号>.width` | 基準px |
| 目が細かい/粗い | `layers.<番号>.spacing` | 小さいほど細かい |
| 影の中の濃淡 | `layers.<番号>.widthByDarkness`（0〜1） | 上げると暗いほど線が太くなる |
| 線の向き | `layers.<番号>.angle` | 0=水平、45=右上がり、90=垂直 |
| 輪郭線が薄い/濃い・多い/少ない | `lines.strength`、`lines.epsilon`、`lines.tau` | ε を下げる（負に大きく）と弱い線が消える。τ を上げると線が増える |
| 輪郭線の太さ | `lines.sigma` | 基準px |
| 背景を白/線だけに | `regions.background.mode` | `keep`（層で描く）/ `white` / `lines` |
| 人物の縁に背景がはみ出す | `regions.background.grow` | 正で人物の範囲を広げる |
| ざらつき・細かい斑点 | `input.smoothRadius`、`input.smoothStrength` | 上げるとトーン分けが滑らかになる |
| 白と黒だけにしたい | `output.binary` | `true` |
| 透かしを入れる/外す | `watermark.enabled` | `true` / `false`（既定は `false`）。`watermark.text` / `position` / `size` / `opacity` で調整 |

### 定石: 「背景の黒が多すぎる」

1. `ctl stats` で `output.background.blackRatio`（黒率）、`solidRatio`（ベタの面積率）、`input.background.luminance`（背景の輝度の分布）を見る。
2. `solidRatio` が大きい → まず `regions.background.solid.threshold` を背景の輝度の `p10`〜`p25` より下へ。まだ多ければ `solid.enabled` を `false`。
3. 輝度の `median` が低く（0.3 以下など）層がほぼ全面に掛かっている → `regions.background.levels.brightness` を 0.1〜0.3 ずつ上げる（または `gamma` を 1.3〜2.0）。`input.background.adjusted` が補正後の分布。
4. 毎回 `ctl stats --wait` で黒率の変化を確かめる。人物側（`output.person`）の数値が変わっていないことも見る。

例: 「背景は黒が多すぎるので調整したうえでハッチ(1)、背景以外はクロスハッチ極細」

```bash
uv run inkdeus ctl state && uv run inkdeus ctl stats
uv run inkdeus ctl preset "ハッチ(1)" --region background
uv run inkdeus ctl preset "クロスハッチ極細" --region person
uv run inkdeus ctl stats --wait                 # 背景の blackRatio / solidRatio / 輝度を見る
uv run inkdeus ctl set regions.background.levels.brightness 0.2
uv run inkdeus ctl set regions.background.solid.threshold 0.08
uv run inkdeus ctl stats --wait                 # 目標に届くまで調整をくり返す
```

## 4. 統計の読み方（`ctl stats` の `stats`）

- `rev`: どの設定に対する統計か。`fresh` が `true` なら、いまの設定ファイルに対する物。
- `output.all` / `output.person` / `output.background`:
  - `blackRatio`: 出力が黒い画素（<128）の割合
  - `meanDensity`: 平均の濃さ（0=白、1=黒）
  - `solidRatio`: ベタの面積率
  - `lineRatio`: 線画の面積率
  - `layers[]`: 各トーン層が描かれている範囲の割合（`layer` は設定上の層番号）
  - `areaRatio`: その領域が画像に占める割合
- `input.<領域>.luminance`: 入力の輝度（0=黒、1=白）の `mean` / `median` / `p10` / `p25` / `p75` / `p90`。`adjusted` は明るさ補正後。
- `parts`: その画像にマスクのある部分名。部分ごとに `output.<部分名>` / `input.<部分名>` がある（中身は人物・背景と同じ項目＋
  `linked` = 人物と同じ設定で描いているか）。別の設定にした部分は `output.person` の集計から外れる（`person` = 人物の設定で描かれている所）。
  `droppedParts` があれば、層の数が多すぎて人物と同じ設定で描かれた部分（部分や層を減らす）。
- `backgroundMode`、`hasMask`、`renderMs`（1回の再描画にかかった時間）。
- `paint.targets`（消しゴム・ペンで手が入っている対象）と `paint.penUsed`。上の数値は、それらを反映した最終出力に対する物。

数値は画素を間引いて数えた推定値（誤差は 1〜2% ほど）。`hasMask` が `false` の画像は全体が人物扱いで、背景の設定は効かない。
