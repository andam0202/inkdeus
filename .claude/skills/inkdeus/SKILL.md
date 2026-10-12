---
name: inkdeus
description: inkdeus（カラーイラストを白黒の漫画原稿風にするエディタ）を起動し、開いている画像の線画・ベタ・ハッチングを言葉の依頼どおりに調整する。「inkdeus を起動して」「背景の黒が多い」「背景はハッチ(1)、人物はクロスハッチ極細に」などの依頼で使う。
---

# inkdeus を起動して、依頼どおりに調整する

inkdeus はブラウザで動くエディタで、設定ファイルを書き換えると開いている画面にすぐ反映される。
ここでは `inkdeus ctl` で設定を変え、**描画結果の統計（数値）**で結果を確かめながら調整する。

前提: 高画質化（拡大）や人物マスクの作成は inkdeus の外で済ませた物を渡す。inkdeus 自体はそれらを行わない。

## 守ること

- **画像そのものは開いて見ない**（入力画像・マスク・書き出した PNG のどれも）。判断は `ctl stats` の数値で行い、見た目の良し悪しは利用者が画面で確かめる。
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
uv run inkdeus new <名前> --images <画像フォルダ> [--masks <マスクフォルダ>]
uv run inkdeus open <名前>
```

- プロジェクトを作らず一時的に開くだけなら `uv run inkdeus run <画像 or フォルダ> [--mask-dir DIR]`

サーバは終了しないので**バックグラウンドで起動**し、表示された URL（既定 `http://127.0.0.1:8770/`）を利用者に伝えてブラウザで開いてもらう。
別のポートで起動したときは、以下の `ctl` すべてに `--port <番号>` を付ける（サブコマンドの前でも後ろでもよい: `ctl --port 8771 state` / `ctl state --port 8771`）。

起動できたかは `uv run inkdeus ctl state` で確かめる（`clients` が 1 以上ならブラウザが開いている。`current` がいま開いている画像）。

## 2. 依頼を受けたときの進め方

1. **対象を確かめる**: `uv run inkdeus ctl state` → `current`（いま開いている画像）。別の画像なら `ctl open <画像名>`。
2. **現状を知る**: `uv run inkdeus ctl get`（設定）と `uv run inkdeus ctl stats`（統計）。
3. **変える**: 大きな方針は `ctl preset`、細かい値は `ctl set` / `ctl patch`。
4. **確かめる**: `uv run inkdeus ctl stats --wait`（いまの設定に対する統計が届くまで待つ）。`stats.rev` が直前の変更の `rev` と同じことを確かめてから数値を読む。
5. 目標に届くまで 3〜4 を数回くり返す。1回に変えるのは1〜2項目にして、どの変更が効いたか分かるようにする。
6. **報告**: 何をどの値からどの値に変えたか、統計がどう変わったか（例: 背景の黒率 0.61 → 0.22）。最終的な見た目の確認は利用者に頼む。

まだ設定が保存されていない画像をブラウザで開くと、直前に開いていた画像の設定を引き継ぐ（同じ調子で続けて作業するため）。開いた直後に `ctl get` で実際の値を確かめること。

`--name <画像名>` を付けると、開いていない画像の設定も変えられる（ファイルだけ更新され、次に開いたとき反映される。統計はブラウザで開いている画像の分しか届かない）。

### コマンド

```bash
uv run inkdeus ctl state                       # 現在の画像・プロジェクト・フォルダ
uv run inkdeus ctl get                         # 設定JSON（params）と rev
uv run inkdeus ctl presets                     # 名前付きスタイルの一覧
uv run inkdeus ctl preset <スタイル> --region person|background|both
uv run inkdeus ctl set <ドット区切りのキー> <値>   # 例: regions.background.levels.brightness 0.25
uv run inkdeus ctl patch '<JSON Merge Patch>'  # 複数項目をまとめて。配列(layers)は丸ごと置換になる
uv run inkdeus ctl stats --wait                # 統計
uv run inkdeus ctl open <画像名>               # 開く画像を切り替える
uv run inkdeus ctl export                      # ブラウザに PNG 書き出しを指示（保存先が返る）
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

スタイルは**層とベタ**（背景なら描き方も）を差し替える。明るさ補正（levels）は変えない。
背景にスタイルを当てると、背景は人物との連動（`linked`）が切れて独自の設定になる。

## 3. 用語と設定キーの対応

設定は「人物」（`regions.person`。マスクの内側。マスクが無い画像は全体）と「背景」（`regions.background`）で別々に持てる。
背景は `regions.background.linked` が `true` の間は人物と同じ設定で描かれる。**背景だけ変えるには `linked` を `false` にする**（背景へ `ctl preset` すると自動で切れる）。長さの値は「基準px」（画像の幅 1280px 相当での px）。

| 言い方 | キー | 向き |
| --- | --- | --- |
| 背景/人物が暗い・黒が多い | `regions.<領域>.levels.brightness`（-1〜1）、`gamma`（0.2〜5） | 上げると明るくなり、ベタと層の面積が減る |
| ベタ（真っ黒）が多い | `regions.<領域>.solid.threshold`（0〜1） | 下げるとベタが減る。`solid.enabled` を `false` で無し |
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
- `backgroundMode`、`hasMask`、`renderMs`（1回の再描画にかかった時間）。

数値は画素を間引いて数えた推定値（誤差は 1〜2% ほど）。`hasMask` が `false` の画像は全体が人物扱いで、背景の設定は効かない。
