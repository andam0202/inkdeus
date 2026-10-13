// 消しゴムのマスクとペンの絵（人が画面で描く物）を GPU 上に持ち、筆を打つ。
//
// - 消去マスク: 対象（各トーン層 / ベタ（人物・背景）/ 線画 / すべて）ごとに 1 チャンネル（0 = そのまま、1 = 消す）。
//   RGBA8 のテクスチャの各チャンネルに 1 対象ずつ詰める（最初に描いたとき確保する）。
//   大きさは画像の長辺 2048 相当まで（それ以上は縮小して持ち、合成のとき滑らかに拡大する）。
// - ペン: 原寸の RG8（R = 黒の量、G = 白（修正液）の量。R + G ≤ 1 で、後から描いた方がもう一方を打ち消す）。
// - 筆: 軌跡に沿って円形のスタンプを詰めて打つ（1 区間 = 1 回のインスタンス描画）。
// - 取り消し用に、1 筆で触ったタイル（256px 角）の「変更前の画素」だけを取り出す。

import { VS } from './shaders.js';
import { encodePNG } from './png.js';

export const TILE = 256;
export const MASK_MAX_SIDE = 2048;
const MASK_TEXTURES = 8;                 // RGBA × 8 = 32 対象ぶん
export const PEN = 'pen';
export const FIXED_TARGETS = ['solid-person', 'solid-background', 'lines', 'all'];
// solid-<領域> の領域は person / background / 部分名（部分のベタ）
const TARGET_RE = /^(layer-[a-z0-9]{4,16}|solid-[\p{L}\p{N}_-]{1,32}|lines|all|pen)$/u;

export const isTarget = (name) => typeof name === 'string' && TARGET_RE.test(name);
export const layerTarget = (id) => `layer-${id}`;

const HEAD = `#version 300 es
precision highp float;
precision highp int;
precision highp sampler2D;
`;

// スタンプ: インスタンスごとに、区間 P0→P1 上の点を中心とする正方形を描く
const VS_STAMP = `#version 300 es
precision highp float;
uniform vec3 uP0;         // x, y, 半径（描く先のテクスチャの px）
uniform vec3 uP1;
uniform float uCount;     // この区間に打つスタンプの数（P0 は含めず、P1 で終わる）
uniform vec2 uTarget;     // 描く先の大きさ
out vec2 vLocal;
flat out float vRadius;
void main() {
  vec3 c = mix(uP0, uP1, (float(gl_InstanceID) + 1.0) / uCount);
  vec2 corner = vec2(float(gl_VertexID & 1), float((gl_VertexID >> 1) & 1)) * 2.0 - 1.0;
  vLocal = corner * (c.z + 1.5);
  vRadius = c.z;
  gl_Position = vec4((c.xy + vLocal) / uTarget * 2.0 - 1.0, 0.0, 1.0);
}`;

// 円の内側が 1。硬さ 1 で縁は 1px のなだらかさ、下げるほど縁のぼかしが広い（半径 × (1 − 硬さ)）
const FS_STAMP = HEAD + `
in vec2 vLocal;
flat in float vRadius;
uniform float uHard;
uniform float uStrength;
uniform int uInvert;      // 1: 1 − 量 を出す（MIN 合成で「減らす」側に使う）
out vec4 o;
void main() {
  float edge = max(vRadius * (1.0 - uHard), 1.0);
  float c = clamp((vRadius + 0.5 - length(vLocal)) / edge, 0.0, 1.0);
  if (edge > 1.0) c = c * c * (3.0 - 2.0 * c);
  float a = c * uStrength;
  o = vec4(uInvert == 1 ? 1.0 - a : a);
}`;

const FS_FILL = HEAD + `
uniform vec4 uValue;
out vec4 o;
void main() { o = uValue; }`;

// 写す: uFetch = 1 は画素をそのまま（uOrigin からの相対位置）、0 は大きさを合わせて滑らかに。
// uSpread = 1 は R を全チャンネルへ（書き込むチャンネルは colorMask で選ぶ）
const FS_COPY = HEAD + `
uniform sampler2D uTex;
uniform vec2 uInvSize;
uniform ivec2 uOrigin;
uniform int uFetch;
uniform int uSpread;
out vec4 o;
void main() {
  vec4 s = uFetch == 1 ? texelFetch(uTex, ivec2(gl_FragCoord.xy) - uOrigin, 0)
                       : textureLod(uTex, gl_FragCoord.xy * uInvSize, 0.0);
  o = uSpread == 1 ? vec4(s.r) : s;
}`;

const CHANNEL_MASKS = [[true, false, false, false], [false, true, false, false], [false, false, true, false],
  [false, false, false, true]];

export class Paint {
  constructor(renderer) {
    this.r = renderer;
    this.gl = renderer.gl;
    this.fbo = this.gl.createFramebuffer();
    this.fboRead = this.gl.createFramebuffer();
    this.programs = {};
    this.maskTex = [];
    this.slots = new Map();           // 対象 → スロット（テクスチャ番号 × 4 + チャンネル）
    this.info = new Map();            // 対象 → { rev, used }
    this.penTex = null;
    this.scratch = {};                // 筆の前の状態の写し（mask / pen）
    this.stroke = null;
    this.live = () => true;           // その対象が、いまの設定・履歴から参照されているか（スロットの回収用）
    this.W = 0; this.H = 0; this.mw = 0; this.mh = 0;
  }

  // ---- 確保・解放 ----

  /** 画像が替わった（大きさが変わった）。描いた物はすべて捨てる */
  reset() {
    const gl = this.gl;
    for (const t of this.maskTex) gl.deleteTexture(t);
    for (const t of [this.penTex, ...Object.values(this.scratch)]) if (t) gl.deleteTexture(t);
    this.maskTex = [];
    this.penTex = null;
    this.scratch = {};
    this.slots.clear();
    this.info.clear();
    this.stroke = null;
    this.W = this.r.W;
    this.H = this.r.H;
    const s = Math.min(1, MASK_MAX_SIDE / Math.max(1, this.W, this.H));
    this.mw = Math.max(1, Math.round(this.W * s));
    this.mh = Math.max(1, Math.round(this.H * s));
  }

  /** 描いた物をすべて捨てる（同じ画像の別のバージョンを開くとき。大きさはそのまま） */
  clearAll() {
    this.reset();
  }

  program(name, vs, fs) {
    if (this.programs[name]) return this.programs[name];
    const gl = this.gl;
    const compile = (type, src) => {
      const sh = gl.createShader(type);
      gl.shaderSource(sh, src);
      gl.compileShader(sh);
      if (!gl.getShaderParameter(sh, gl.COMPILE_STATUS)) {
        throw new Error(`シェーダのコンパイルに失敗 (${name}): ${gl.getShaderInfoLog(sh)}`);
      }
      return sh;
    };
    const prog = gl.createProgram();
    gl.attachShader(prog, compile(gl.VERTEX_SHADER, vs));
    gl.attachShader(prog, compile(gl.FRAGMENT_SHADER, fs));
    gl.linkProgram(prog);
    if (!gl.getProgramParameter(prog, gl.LINK_STATUS)) {
      throw new Error(`シェーダのリンクに失敗 (${name}): ${gl.getProgramInfoLog(prog)}`);
    }
    const locs = {};
    this.programs[name] = { prog, loc: (u) => (u in locs ? locs[u] : (locs[u] = gl.getUniformLocation(prog, u))) };
    return this.programs[name];
  }

  note(target) {
    if (!this.info.has(target)) this.info.set(target, { rev: 0, used: false });
    return this.info.get(target);
  }

  /** その対象に描いた物があるか（「戻す」で全部戻したかどうかは、保存のときに確かめる） */
  has(target) {
    const i = this.info.get(target);
    return !!i && i.used;
  }

  rev(target) {
    const i = this.info.get(target);
    return i ? i.rev : 0;
  }

  /** 描いた物のある対象の一覧 */
  targets() {
    return [...this.info.keys()].filter((t) => this.has(t)).sort();
  }

  /** 対象の置き場所を用意する。{ pen } または { tex, ch } */
  place(target) {
    const gl = this.gl;
    if (!isTarget(target)) throw new Error(`消しゴム・ペンの対象として使えません: ${target}`);
    if (!this.W) throw new Error('画像が読み込まれていません');
    if (target === PEN) {
      if (!this.penTex) this.penTex = this.r.createTexture(gl.RG8, this.W, this.H, { linear: true });
      this.note(target);
      return { pen: true, tex: this.penTex, w: this.W, h: this.H, sx: 1, sy: 1 };
    }
    if (!this.slots.has(target)) {
      const taken = new Set(this.slots.values());
      let slot = -1;
      for (let s = 0; s < this.maskTex.length * 4 && slot < 0; s++) if (!taken.has(s)) slot = s;
      if (slot < 0 && this.maskTex.length < MASK_TEXTURES) {
        this.maskTex.push(this.r.createTexture(gl.RGBA8, this.mw, this.mh, { linear: true }));
        slot = (this.maskTex.length - 1) * 4;
      }
      if (slot < 0) slot = this.reclaim();
      this.slots.set(target, slot);
      this.note(target);
    }
    const slot = this.slots.get(target);
    return { pen: false, tex: this.maskTex[slot >> 2], ch: slot & 3, slot, w: this.mw, h: this.mh,
      sx: this.mw / this.W, sy: this.mh / this.H };
  }

  /** 置き場所が尽きたとき、何も描かれていない対象・もう参照されていない対象の場所を空ける */
  reclaim() {
    const order = [...this.slots.keys()].filter((t) => !this.has(t))
      .concat([...this.slots.keys()].filter((t) => this.has(t) && !this.live(t)));
    const victim = order[0];
    if (victim === undefined) throw new Error('消しゴムを使っている対象が多すぎます（使っていない対象の消しゴムを取り消してください）');
    const slot = this.slots.get(victim);
    this.slots.delete(victim);
    this.info.delete(victim);
    this.fill(this.maskTex[slot >> 2], this.mw, this.mh, CHANNEL_MASKS[slot & 3], 0);
    return slot;
  }

  /** 対象を捨てる（描いた物を消して、置き場所を空ける） */
  drop(target) {
    const gl = this.gl;
    if (target === PEN) {
      if (this.penTex) gl.deleteTexture(this.penTex);
      this.penTex = null;
    } else if (this.slots.has(target)) {
      const slot = this.slots.get(target);
      this.fill(this.maskTex[slot >> 2], this.mw, this.mh, CHANNEL_MASKS[slot & 3], 0);
      this.slots.delete(target);
    }
    this.info.delete(target);
  }

  // ---- 低レベルの描画 ----

  begin(tex, w, h, mask) {
    const gl = this.gl;
    gl.bindFramebuffer(gl.FRAMEBUFFER, this.fbo);
    gl.framebufferTexture2D(gl.FRAMEBUFFER, gl.COLOR_ATTACHMENT0, gl.TEXTURE_2D, tex, 0);
    gl.viewport(0, 0, w, h);
    gl.colorMask(...(mask || [true, true, true, true]));
  }

  end() {
    const gl = this.gl;
    gl.disable(gl.BLEND);
    gl.blendEquation(gl.FUNC_ADD);
    gl.colorMask(true, true, true, true);
    gl.framebufferTexture2D(gl.FRAMEBUFFER, gl.COLOR_ATTACHMENT0, gl.TEXTURE_2D, null, 0);
    gl.bindFramebuffer(gl.FRAMEBUFFER, null);
  }

  fill(tex, w, h, mask, value) {
    const gl = this.gl;
    const p = this.program('fill', VS, FS_FILL);
    gl.useProgram(p.prog);
    this.begin(tex, w, h, mask);
    gl.uniform4f(p.loc('uValue'), value, value, value, value);
    gl.drawArrays(gl.TRIANGLES, 0, 3);
    this.end();
  }

  /** src を dst の矩形（x, y, w, h）へ写す。fetch = 画素をそのまま、そうでなければ dst 全体へ大きさを合わせて */
  copy(src, dst, dw, dh, { rect = null, mask = null, spread = false, fetch = false } = {}) {
    const gl = this.gl;
    const p = this.program('copy', VS, FS_COPY);
    gl.useProgram(p.prog);
    this.begin(dst, dw, dh, mask);
    if (rect) gl.viewport(rect.x, rect.y, rect.w, rect.h);
    gl.activeTexture(gl.TEXTURE0);
    gl.bindTexture(gl.TEXTURE_2D, src);
    gl.uniform1i(p.loc('uTex'), 0);
    gl.uniform2f(p.loc('uInvSize'), 1 / dw, 1 / dh);
    gl.uniform2i(p.loc('uOrigin'), rect ? rect.x : 0, rect ? rect.y : 0);
    gl.uniform1i(p.loc('uFetch'), fetch ? 1 : 0);
    gl.uniform1i(p.loc('uSpread'), spread ? 1 : 0);
    gl.drawArrays(gl.TRIANGLES, 0, 3);
    this.end();
  }

  /** テクスチャの矩形を RGBA で読む */
  readRGBA(tex, x, y, w, h) {
    const gl = this.gl;
    gl.bindFramebuffer(gl.FRAMEBUFFER, this.fbo);
    gl.framebufferTexture2D(gl.FRAMEBUFFER, gl.COLOR_ATTACHMENT0, gl.TEXTURE_2D, tex, 0);
    const buf = new Uint8Array(w * h * 4);
    gl.readPixels(x, y, w, h, gl.RGBA, gl.UNSIGNED_BYTE, buf);
    gl.framebufferTexture2D(gl.FRAMEBUFFER, gl.COLOR_ATTACHMENT0, gl.TEXTURE_2D, null, 0);
    gl.bindFramebuffer(gl.FRAMEBUFFER, null);
    return buf;
  }

  /** 対象の絵の大きさと、1 画素のバイト数（マスクは 1、ペンは 2） */
  dims(target) {
    const at = this.place(target);
    return { w: at.w, h: at.h, channels: at.pen ? 2 : 1 };
  }

  /** タイルの並び（x, y, w, h）の、いまの画素 */
  readTiles(target, tiles) {
    const at = this.place(target);
    return tiles.map(({ x, y, w, h }) => ({ x, y, w, h, data: this.readRect(at, at.tex, x, y, w, h) }));
  }

  /** 対象の矩形の画素（マスクは 1 バイト/画素、ペンは 2 バイト/画素） */
  readRect(at, tex, x, y, w, h) {
    const rgba = this.readRGBA(tex, x, y, w, h);
    const n = w * h;
    if (at.pen) {
      const out = new Uint8Array(n * 2);
      for (let i = 0; i < n; i++) { out[i * 2] = rgba[i * 4]; out[i * 2 + 1] = rgba[i * 4 + 1]; }
      return out;
    }
    const out = new Uint8Array(n);
    for (let i = 0; i < n; i++) out[i] = rgba[i * 4 + at.ch];
    return out;
  }

  writeRect(at, x, y, w, h, data) {
    const gl = this.gl;
    const tmp = gl.createTexture();
    gl.bindTexture(gl.TEXTURE_2D, tmp);
    if (at.pen) gl.texImage2D(gl.TEXTURE_2D, 0, gl.RG8, w, h, 0, gl.RG, gl.UNSIGNED_BYTE, data);
    else gl.texImage2D(gl.TEXTURE_2D, 0, gl.R8, w, h, 0, gl.RED, gl.UNSIGNED_BYTE, data);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.NEAREST);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.NEAREST);
    this.copy(tmp, at.tex, at.w, at.h, { rect: { x, y, w, h }, fetch: true, spread: !at.pen,
      mask: at.pen ? null : CHANNEL_MASKS[at.ch] });
    gl.deleteTexture(tmp);
  }

  /** 筆の前の状態を写しておくテクスチャ（最初の筆で確保する） */
  snapshot(at) {
    const gl = this.gl;
    const key = at.pen ? 'pen' : 'mask';
    if (!this.scratch[key]) {
      this.scratch[key] = this.r.createTexture(at.pen ? gl.RG8 : gl.RGBA8, at.w, at.h);
    }
    gl.bindFramebuffer(gl.READ_FRAMEBUFFER, this.fboRead);
    gl.framebufferTexture2D(gl.READ_FRAMEBUFFER, gl.COLOR_ATTACHMENT0, gl.TEXTURE_2D, at.tex, 0);
    gl.bindFramebuffer(gl.DRAW_FRAMEBUFFER, this.fbo);
    gl.framebufferTexture2D(gl.DRAW_FRAMEBUFFER, gl.COLOR_ATTACHMENT0, gl.TEXTURE_2D, this.scratch[key], 0);
    gl.blitFramebuffer(0, 0, at.w, at.h, 0, 0, at.w, at.h, gl.COLOR_BUFFER_BIT, gl.NEAREST);
    gl.framebufferTexture2D(gl.READ_FRAMEBUFFER, gl.COLOR_ATTACHMENT0, gl.TEXTURE_2D, null, 0);
    gl.framebufferTexture2D(gl.DRAW_FRAMEBUFFER, gl.COLOR_ATTACHMENT0, gl.TEXTURE_2D, null, 0);
    gl.bindFramebuffer(gl.READ_FRAMEBUFFER, null);
    gl.bindFramebuffer(gl.DRAW_FRAMEBUFFER, null);
    return this.scratch[key];
  }

  // ---- 筆 ----

  /**
   * 1 筆を始める。
   * @param {string} target
   * @param {{mode: 'erase'|'restore'|'black'|'white', strength?: number, hardness?: number}} brush
   */
  beginStroke(target, brush) {
    const at = this.place(target);
    if (at.pen !== (brush.mode === 'black' || brush.mode === 'white')) throw new Error('筆の種類と対象が合いません');
    this.stroke = {
      target, at, before: this.snapshot(at), tiles: new Set(), last: null,
      mode: brush.mode,
      strength: Math.min(1, Math.max(0, brush.strength ?? 1)),
      hardness: Math.min(1, Math.max(0, brush.hardness ?? 1)),
    };
  }

  /**
   * 画像座標 (x, y) へ、半径 radius（画像の px）で筆を進める。前の点からの区間にスタンプを詰めて打つ。
   * 戻り値は、変わりうる範囲（画像の px の矩形）
   */
  stamp(x, y, radius) {
    const st = this.stroke;
    if (!st) return null;
    const gl = this.gl;
    const { at } = st;
    const p1 = [x * at.sx, y * at.sy, Math.max(0.3, radius * at.sx)];
    const p0 = st.last || p1;
    st.last = p1;
    const dist = Math.hypot(p1[0] - p0[0], p1[1] - p0[1]);
    const count = Math.max(1, Math.min(8192, Math.ceil(dist / Math.max(0.5, 0.15 * Math.min(p0[2], p1[2])))));
    const p = this.program('stamp', VS_STAMP, FS_STAMP);
    gl.useProgram(p.prog);
    gl.uniform3f(p.loc('uP0'), p0[0], p0[1], p0[2]);
    gl.uniform3f(p.loc('uP1'), p1[0], p1[1], p1[2]);
    gl.uniform1f(p.loc('uCount'), count);
    gl.uniform2f(p.loc('uTarget'), at.w, at.h);
    gl.uniform1f(p.loc('uHard'), st.hardness);
    gl.uniform1f(p.loc('uStrength'), at.pen ? 1 : st.strength);
    gl.enable(gl.BLEND);
    gl.blendFunc(gl.ONE, gl.ONE);
    // 増やす側は MAX、減らす側は MIN（1 − 量）。同じ筆の中でスタンプが重なっても濃さは積み上がらない
    const raise = (mask) => {
      this.begin(at.tex, at.w, at.h, mask);
      gl.blendEquation(gl.MAX);
      gl.uniform1i(p.loc('uInvert'), 0);
      gl.drawArraysInstanced(gl.TRIANGLE_STRIP, 0, 4, count);
    };
    const lower = (mask) => {
      this.begin(at.tex, at.w, at.h, mask);
      gl.blendEquation(gl.MIN);
      gl.uniform1i(p.loc('uInvert'), 1);
      gl.drawArraysInstanced(gl.TRIANGLE_STRIP, 0, 4, count);
    };
    if (st.mode === 'erase') raise(CHANNEL_MASKS[at.ch]);
    else if (st.mode === 'restore') lower(CHANNEL_MASKS[at.ch]);
    else if (st.mode === 'black') { raise(CHANNEL_MASKS[0]); lower(CHANNEL_MASKS[1]); }
    else { raise(CHANNEL_MASKS[1]); lower(CHANNEL_MASKS[0]); }
    this.end();

    // 触った範囲（描く先の px）→ タイルの集合と、画像の px の矩形
    const pad = Math.max(p0[2], p1[2]) + 2;
    const x0 = Math.max(0, Math.floor(Math.min(p0[0], p1[0]) - pad));
    const y0 = Math.max(0, Math.floor(Math.min(p0[1], p1[1]) - pad));
    const x1 = Math.min(at.w, Math.ceil(Math.max(p0[0], p1[0]) + pad));
    const y1 = Math.min(at.h, Math.ceil(Math.max(p0[1], p1[1]) + pad));
    if (x1 <= x0 || y1 <= y0) return null;
    for (let ty = Math.floor(y0 / TILE); ty * TILE < y1; ty++) {
      for (let tx = Math.floor(x0 / TILE); tx * TILE < x1; tx++) st.tiles.add(ty * 65536 + tx);
    }
    return this.imageRect(at, x0, y0, x1 - x0, y1 - y0);
  }

  imageRect(at, x, y, w, h) {
    const x0 = Math.max(0, Math.floor(x / at.sx) - 2);
    const y0 = Math.max(0, Math.floor(y / at.sy) - 2);
    const x1 = Math.min(this.W, Math.ceil((x + w) / at.sx) + 2);
    const y1 = Math.min(this.H, Math.ceil((y + h) / at.sy) + 2);
    return { x: x0, y: y0, w: Math.max(0, x1 - x0), h: Math.max(0, y1 - y0) };
  }

  /**
   * 1 筆を終える。戻り値は取り消し用の記録（何も触らなかったら null）:
   * { target, pen, tiles: [{ x, y, w, h, data }], bytes }。data は「取り消したときに戻す画素」
   */
  endStroke() {
    const st = this.stroke;
    this.stroke = null;
    if (!st || !st.tiles.size) return null;
    const { at } = st;
    const tiles = [];
    let bytes = 0;
    for (const key of [...st.tiles].sort((a, b) => a - b)) {
      const x = (key % 65536) * TILE;
      const y = Math.floor(key / 65536) * TILE;
      const w = Math.min(TILE, at.w - x);
      const h = Math.min(TILE, at.h - y);
      const data = this.readRect(at, st.before, x, y, w, h);
      bytes += data.length;
      tiles.push({ x, y, w, h, data });
    }
    const info = this.note(st.target);
    info.rev += 1;
    if (st.mode !== 'restore') info.used = true;
    return { target: st.target, pen: at.pen, tiles, bytes };
  }

  // ---- 取り消し・やり直し・全消去 ----

  /**
   * 記録のタイルを書き戻し、記録の中身を「いまの画素」に入れ替える（取り消しとやり直しで同じ操作）。
   * 戻り値は変わった範囲（画像の px の矩形）
   */
  swap(step) {
    const at = this.place(step.target);
    let rect = null;
    for (const tile of step.tiles) {
      const now = this.readRect(at, at.tex, tile.x, tile.y, tile.w, tile.h);
      this.writeRect(at, tile.x, tile.y, tile.w, tile.h, tile.data);
      tile.data = now;
      rect = unionRect(rect, this.imageRect(at, tile.x, tile.y, tile.w, tile.h));
    }
    const info = this.note(step.target);
    info.rev += 1;
    info.used = true;                 // 空になったかどうかは、保存のときに確かめる
    return rect;
  }

  /** 対象に描いた物を全部消す。戻り値は取り消し用の記録（何も無かったら null） */
  clear(target) {
    const info = this.info.get(target);
    if (!info || (target === PEN ? !this.penTex : !this.slots.has(target))) return null;
    const at = this.place(target);
    const tiles = [];
    let bytes = 0;
    for (let y = 0; y < at.h; y += TILE) {
      for (let x = 0; x < at.w; x += TILE) {
        const w = Math.min(TILE, at.w - x);
        const h = Math.min(TILE, at.h - y);
        const data = this.readRect(at, at.tex, x, y, w, h);
        if (data.some((v) => v !== 0)) { tiles.push({ x, y, w, h, data }); bytes += data.length; }
      }
    }
    this.fill(at.tex, at.w, at.h, at.pen ? null : CHANNEL_MASKS[at.ch], 0);
    info.rev += 1;
    info.used = false;
    return tiles.length ? { target, pen: at.pen, tiles, bytes } : null;
  }

  // ---- 保存・読み込み ----

  /**
   * 対象を PNG にする（マスクは 8bit グレースケール、ペンは RGB で R = 黒・G = 白）。
   * 何も描かれていなければ null（そのとき「描いた物は無い」と覚え直す）
   */
  async encode(target) {
    if (target === PEN ? !this.penTex : !this.slots.has(target)) return null;
    const at = this.place(target);
    const info = this.note(target);
    const rev = info.rev;
    let any = false;
    const channels = at.pen ? 3 : 1;
    const blob = await encodePNG(at.w, at.h, channels, (y0, rows) => {
      // 途中で筆が入ったら（rev が変わる）、呼び出し側が捨ててやり直す
      const rgba = this.readRGBA(at.tex, 0, y0, at.w, rows);
      const n = at.w * rows;
      const out = new Uint8Array(n * channels);
      if (at.pen) {
        for (let i = 0; i < n; i++) {
          const r = rgba[i * 4];
          const g = rgba[i * 4 + 1];
          out[i * 3] = r; out[i * 3 + 1] = g;
          if (r | g) any = true;
        }
      } else {
        for (let i = 0; i < n; i++) { const v = rgba[i * 4 + at.ch]; out[i] = v; if (v) any = true; }
      }
      return out;
    });
    if (info.rev !== rev || !this.info.has(target)) return { stale: true };
    if (!any) { info.used = false; return null; }
    return { blob, rev, width: at.w, height: at.h };
  }

  /** 保存してあった PNG（ImageBitmap など）を対象へ読み込む。大きさが違えば合わせる */
  load(target, source) {
    const gl = this.gl;
    const at = this.place(target);
    const tmp = gl.createTexture();
    gl.bindTexture(gl.TEXTURE_2D, tmp);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA8, gl.RGBA, gl.UNSIGNED_BYTE, source);
    const same = source.width === at.w && source.height === at.h;
    for (const [k, v] of [[gl.TEXTURE_MIN_FILTER, same ? gl.NEAREST : gl.LINEAR],
      [gl.TEXTURE_MAG_FILTER, same ? gl.NEAREST : gl.LINEAR],
      [gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE], [gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE]]) {
      gl.texParameteri(gl.TEXTURE_2D, k, v);
    }
    this.copy(tmp, at.tex, at.w, at.h, { fetch: same, spread: !at.pen, mask: at.pen ? null : CHANNEL_MASKS[at.ch] });
    gl.deleteTexture(tmp);
    const info = this.note(target);
    info.rev += 1;
    info.used = true;
    return info.rev;
  }

  // ---- 合成パスへの受け渡し ----

  /** その対象のスロット（描いた物が無ければ -1） */
  slotOf(target) {
    return this.has(target) && this.slots.has(target) ? this.slots.get(target) : -1;
  }

  penOn() {
    return !!this.penTex && this.has(PEN);
  }

  /**
   * 合成パスのユニフォームを設定する。
   * layerSlots: 合成で使う層・ベタのスロットの並び（1 つでも 0 以上なら、消去マスクを読む）。misc: [-, -, 線画, すべて]
   * （層とベタのスロットそのものは、合成パスの uC / uLevels に入れて渡す）
   */
  bind(p, layerSlots, misc, showPen) {
    const gl = this.gl;
    const r = this.r;
    const used = layerSlots.some((s) => s >= 0) || misc.some((s) => s >= 0);
    for (let i = 0; i < MASK_TEXTURES; i++) {
      if (used && i < this.maskTex.length) r.sampler(p, `uEr${i}`, this.maskTex[i]);
      else gl.uniform1i(p.loc(`uEr${i}`), 0);          // 使わない分は、同じ型のテクスチャが入っている 0 番へ
    }
    gl.uniform1i(p.loc('uErN'), used ? this.maskTex.length : 0);
    gl.uniform4i(p.loc('uErMisc'), misc[0], misc[1], misc[2], misc[3]);
    const pen = showPen && this.penOn();
    if (pen) r.sampler(p, 'uPen', this.penTex);
    else gl.uniform1i(p.loc('uPen'), 0);
    gl.uniform1i(p.loc('uPenOn'), pen ? 1 : 0);
  }
}

export function unionRect(a, b) {
  if (!a) return b;
  if (!b) return a;
  const x = Math.min(a.x, b.x);
  const y = Math.min(a.y, b.y);
  return { x, y, w: Math.max(a.x + a.w, b.x + b.w) - x, h: Math.max(a.y + a.h, b.y + b.h) - y };
}
