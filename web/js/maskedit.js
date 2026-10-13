// 部分マスク・人物マスクの手直し（元のマスクに対する上書き）を、画素の並びとして持つ。
//
// - 対象（'person' または部分名）ごとに 1 バイト/画素の 3 値: 255 = 加える / 0 = 外す / 128 = 触っていない。
//   大きさは画像の長辺 2048 相当まで（それ以上は縮めて持つ。消しゴムのマスクと同じ）。
// - 元のマスクは書き換えない。描画に使うマスクは、元のマスクに手直しを重ねて作る（compose）。
//   決まりはサーバ側（src/inkdeus/maskedit.py）と同じ: 部分どうしは排他、部分は人物の内側だけ、人物から外した所は背景。
// - 取り消し用に、1 筆で触ったタイル（256px 角）の「変更前の画素」だけを持つ（消しゴム・ペンと同じ方式）。

import { encodePNG } from './png.js';

export const NEUTRAL = 128;
export const ADD_MIN = 192;
export const REMOVE_MAX = 63;
export const PERSON = 'person';
const TILE = 256;
const MAX_SIDE = 2048;

export class MaskEdits {
  constructor() {
    this.reset(0, 0);
  }

  /** 画像が替わった。手直しはすべて捨てる（画像ごとに読み直す） */
  reset(W, H) {
    this.W = W;
    this.H = H;
    const s = Math.min(1, MAX_SIDE / Math.max(1, W, H));
    this.mw = Math.max(1, Math.round(W * s));
    this.mh = Math.max(1, Math.round(H * s));
    this.data = new Map();            // 対象 → Uint8Array(mw * mh)
    this.info = new Map();            // 対象 → { rev, used }
    this.stroke = null;
  }

  note(target) {
    if (!this.info.has(target)) this.info.set(target, { rev: 0, used: false });
    return this.info.get(target);
  }

  ensure(target) {
    if (!this.data.has(target)) this.data.set(target, new Uint8Array(this.mw * this.mh).fill(NEUTRAL));
    this.note(target);
    return this.data.get(target);
  }

  /** その対象に手直しがあるか（全部戻したかどうかは、保存のときに確かめる） */
  has(target) {
    const i = this.info.get(target);
    return !!i && i.used;
  }

  rev(target) {
    const i = this.info.get(target);
    return i ? i.rev : 0;
  }

  targets() {
    return [...this.info.keys()].filter((t) => this.has(t)).sort();
  }

  // ---- 筆 ----

  /**
   * 1 筆を始める。mode は 'add'（その対象に加える）/ 'remove'（その対象から外す）。
   * others: 部分に加えるとき、ほかの部分の「加える」をその場所から消す（塗った部分が必ず勝つように）
   */
  begin(target, { mode, hardness = 1, others = [] }) {
    this.ensure(target);
    this.stroke = { target, mode, hardness: Math.min(1, Math.max(0, hardness)), last: null,
      others: mode === 'add' ? others.filter((t) => t !== target && this.data.has(t)) : [],
      before: new Map() };            // `${対象}|${タイル}` → { target, x, y, w, h, data }
  }

  /** タイルに初めて触る前に、いまの画素を控える */
  keep(st, target, x0, y0, x1, y1) {
    const data = this.data.get(target);
    for (let ty = Math.floor(y0 / TILE); ty * TILE < y1; ty++) {
      for (let tx = Math.floor(x0 / TILE); tx * TILE < x1; tx++) {
        const key = `${target}|${ty * 65536 + tx}`;
        if (st.before.has(key)) continue;
        const x = tx * TILE;
        const y = ty * TILE;
        const w = Math.min(TILE, this.mw - x);
        const h = Math.min(TILE, this.mh - y);
        const copy = new Uint8Array(w * h);
        for (let r = 0; r < h; r++) copy.set(data.subarray((y + r) * this.mw + x, (y + r) * this.mw + x + w), r * w);
        st.before.set(key, { target, x, y, w, h, data: copy });
      }
    }
  }

  /** 画像座標 (x, y) へ、半径 radius（画像の px）で筆を進める。戻り値は、変わりうる範囲（画像の px の矩形） */
  stamp(x, y, radius) {
    const st = this.stroke;
    if (!st) return null;
    const sx = this.mw / this.W;
    const sy = this.mh / this.H;
    const p1 = [x * sx, y * sy, Math.max(0.5, radius * sx)];
    const p0 = st.last || p1;
    st.last = p1;
    const pad = Math.max(p0[2], p1[2]) + 2;
    const x0 = Math.max(0, Math.floor(Math.min(p0[0], p1[0]) - pad));
    const y0 = Math.max(0, Math.floor(Math.min(p0[1], p1[1]) - pad));
    const x1 = Math.min(this.mw, Math.ceil(Math.max(p0[0], p1[0]) + pad));
    const y1 = Math.min(this.mh, Math.ceil(Math.max(p0[1], p1[1]) + pad));
    if (x1 <= x0 || y1 <= y0) return null;
    for (const t of [st.target, ...st.others]) this.keep(st, t, x0, y0, x1, y1);
    const data = this.data.get(st.target);
    const others = st.others.map((t) => this.data.get(t));
    const dx = p1[0] - p0[0];
    const dy = p1[1] - p0[1];
    const len2 = dx * dx + dy * dy;
    const add = st.mode === 'add';
    for (let py = y0; py < y1; py++) {
      for (let px = x0; px < x1; px++) {
        // 区間 P0→P1 への最短距離（半径は区間に沿って線形に変える）
        const cx = px + 0.5;
        const cy = py + 0.5;
        const t = len2 > 0 ? Math.min(1, Math.max(0, ((cx - p0[0]) * dx + (cy - p0[1]) * dy) / len2)) : 0;
        const r = p0[2] + (p1[2] - p0[2]) * t;
        const d = Math.hypot(cx - (p0[0] + dx * t), cy - (p0[1] + dy * t));
        const edge = Math.max(r * (1 - st.hardness), 1);
        let c = Math.min(1, Math.max(0, (r + 0.5 - d) / edge));
        if (edge > 1) c = c * c * (3 - 2 * c);
        // 手直しは 3 値で持つ（半分より内側だけを塗る）。あとから描いた筆が、その場所では勝つ
        if (c < 0.5) continue;
        const i = py * this.mw + px;
        data[i] = add ? 255 : 0;
        if (add) for (const o of others) if (o[i] > NEUTRAL) o[i] = NEUTRAL;
      }
    }
    return this.imageRect(x0, y0, x1 - x0, y1 - y0);
  }

  imageRect(x, y, w, h) {
    const sx = this.mw / this.W;
    const sy = this.mh / this.H;
    const x0 = Math.max(0, Math.floor(x / sx) - 1);
    const y0 = Math.max(0, Math.floor(y / sy) - 1);
    return { x: x0, y: y0, w: Math.min(this.W, Math.ceil((x + w) / sx) + 1) - x0,
      h: Math.min(this.H, Math.ceil((y + h) / sy) + 1) - y0 };
  }

  /**
   * 1 筆を終える。戻り値は取り消し用の記録（何も変わらなかったら null）:
   * { maskedit: true, target, parts: [{ target, tiles: [{ x, y, w, h, data }] }], bytes }
   */
  end() {
    const st = this.stroke;
    this.stroke = null;
    if (!st || !st.before.size) return null;
    const byTarget = new Map();
    let bytes = 0;
    for (const tile of st.before.values()) {
      // 実際に変わったタイルだけを残す
      const now = this.readTile(tile.target, tile);
      if (now.every((v, i) => v === tile.data[i])) continue;
      if (!byTarget.has(tile.target)) byTarget.set(tile.target, []);
      byTarget.get(tile.target).push(tile);
      bytes += tile.data.length;
    }
    if (!byTarget.size) return null;
    for (const target of byTarget.keys()) this.note(target).rev += 1;
    if (byTarget.has(st.target)) this.note(st.target).used = true;
    return { maskedit: true, target: st.target,
      parts: [...byTarget].map(([target, tiles]) => ({ target, tiles })), bytes };
  }

  readTile(target, { x, y, w, h }) {
    const data = this.data.get(target);
    const out = new Uint8Array(w * h);
    for (let r = 0; r < h; r++) out.set(data.subarray((y + r) * this.mw + x, (y + r) * this.mw + x + w), r * w);
    return out;
  }

  /** 記録のタイルを書き戻し、記録の中身を「いまの画素」に入れ替える（取り消しとやり直しで同じ操作） */
  swap(step) {
    for (const part of step.parts) {
      const data = this.ensure(part.target);
      for (const tile of part.tiles) {
        const now = this.readTile(part.target, tile);
        for (let r = 0; r < tile.h; r++) {
          data.set(tile.data.subarray(r * tile.w, (r + 1) * tile.w), (tile.y + r) * this.mw + tile.x);
        }
        tile.data = now;
      }
      const info = this.note(part.target);
      info.rev += 1;
      info.used = true;               // 空になったかどうかは、保存のときに確かめる
    }
  }

  /** 対象の手直しを全部取り消す（targets を省くと全部の対象）。戻り値は取り消し用の記録（何も無ければ null） */
  clear(targets = null) {
    const parts = [];
    let bytes = 0;
    for (const target of targets || [...this.data.keys()]) {
      const data = this.data.get(target);
      if (!data) continue;
      const tiles = [];
      for (let y = 0; y < this.mh; y += TILE) {
        for (let x = 0; x < this.mw; x += TILE) {
          const tile = { x, y, w: Math.min(TILE, this.mw - x), h: Math.min(TILE, this.mh - y) };
          tile.data = this.readTile(target, tile);
          if (tile.data.some((v) => v !== NEUTRAL)) { tiles.push(tile); bytes += tile.data.length; }
        }
      }
      data.fill(NEUTRAL);
      const info = this.note(target);
      info.rev += 1;
      info.used = false;
      if (tiles.length) parts.push({ target, tiles });
    }
    return parts.length ? { maskedit: true, target: parts[0].target, parts, bytes } : null;
  }

  // ---- 保存・読み込み ----

  /** 対象を PNG（8bit グレースケール）にする。全部「触っていない」なら null */
  async encode(target) {
    const data = this.data.get(target);
    const info = this.note(target);
    if (!data || !data.some((v) => v !== NEUTRAL)) { info.used = false; return null; }
    const rev = info.rev;
    const { mw } = this;
    const blob = await encodePNG(mw, this.mh, 1, (y0, rows) => data.slice(y0 * mw, (y0 + rows) * mw));
    if (info.rev !== rev) return { stale: true };
    return { blob, rev };
  }

  /** 保存してあった PNG（ImageBitmap）を対象へ読み込む。大きさが違えば合わせる（最近傍） */
  load(target, bitmap) {
    const canvas = new OffscreenCanvas(this.mw, this.mh);
    const c = canvas.getContext('2d', { willReadFrequently: true });
    c.imageSmoothingEnabled = false;
    c.drawImage(bitmap, 0, 0, this.mw, this.mh);
    const rgba = c.getImageData(0, 0, this.mw, this.mh).data;
    const data = this.ensure(target);
    for (let i = 0; i < data.length; i++) data[i] = rgba[i * 4];
    const info = this.note(target);
    info.rev += 1;
    info.used = true;
    return info.rev;
  }

  // ---- 合成（元のマスク ＋ 手直し → 描画に使うマスク）----

  /**
   * @param {Uint8Array|null} person  元の人物マスク（画像と同じ大きさ。0 / 255）。無ければ null（全体が人物）
   * @param {Uint8Array|null} codes   元の部分の番号の地図（0 = どの部分でもない、1〜 = names の番号）
   * @param {string[]} names          部分名（番号 1〜 の順）
   * @returns {{ person: Uint8Array|null, codes: Uint8Array|null }}
   */
  compose(person, codes, names) {
    const { W, H, mw, mh } = this;
    const n = W * H;
    const editP = this.has(PERSON) ? this.data.get(PERSON) : null;
    const edits = names.map((name) => (this.has(name) ? this.data.get(name) : null));
    const anyPart = edits.some(Boolean);
    const outP = person || editP ? new Uint8Array(n) : null;
    const outC = names.length && (codes || anyPart) ? new Uint8Array(n) : null;
    // 画像の列 → 手直しの列（最近傍）
    const col = new Int32Array(W);
    for (let x = 0; x < W; x++) col[x] = Math.min(mw - 1, Math.floor(((x + 0.5) * mw) / W));
    for (let y = 0; y < H; y++) {
      const row = Math.min(mh - 1, Math.floor(((y + 0.5) * mh) / H)) * mw;
      for (let x = 0; x < W; x++) {
        const i = y * W + x;
        const j = row + col[x];
        let inside = person ? person[i] >= 128 : true;
        if (editP) {
          const e = editP[j];
          if (e >= ADD_MIN) inside = true;
          else if (e <= REMOVE_MAX) inside = false;
        }
        if (outP) outP[i] = inside ? 255 : 0;
        if (!outC) continue;
        let code = codes ? codes[i] : 0;
        if (code > 0 && edits[code - 1] && edits[code - 1][j] <= REMOVE_MAX) code = 0;
        if (anyPart) {
          for (let k = 0; k < edits.length; k++) if (edits[k] && edits[k][j] >= ADD_MIN) code = k + 1;
        }
        outC[i] = inside ? code : 0;       // 部分は人物の内側だけ
      }
    }
    return { person: outP, codes: outC };
  }
}

/** ImageBitmap（マスクの画像）を、1 バイト/画素の並び（0 / 255）にする。帯に分けて読む（大きな画像でもメモリを食わないように） */
export function bitmapToMask(bitmap, W, H) {
  const out = new Uint8Array(W * H);
  const STRIP = 512;
  const canvas = new OffscreenCanvas(W, Math.min(STRIP, H));
  const c = canvas.getContext('2d', { willReadFrequently: true });
  c.imageSmoothingEnabled = false;
  for (let y0 = 0; y0 < H; y0 += STRIP) {
    const rows = Math.min(STRIP, H - y0);
    c.clearRect(0, 0, W, STRIP);
    c.drawImage(bitmap, 0, (y0 * bitmap.height) / H, bitmap.width, (rows * bitmap.height) / H, 0, 0, W, rows);
    const rgba = c.getImageData(0, 0, W, rows).data;
    for (let i = 0; i < W * rows; i++) out[y0 * W + i] = rgba[i * 4] >= 128 ? 255 : 0;
  }
  return out;
}
