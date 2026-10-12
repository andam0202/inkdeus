// WebGL2 の描画パイプライン。プレビューと書き出しは同じ出力テクスチャを使う（WYSIWYG）。
// 依存する設定が変わったパスだけを再計算する（各パスが「何から作ったか」のキーを覚えている）。

import * as S from './shaders.js';
import { effectiveUnit, effectiveRegions, effectiveWatermark, MAX_LAYERS } from './schema.js';

const MAX_SMOOTH_RADIUS = 64;
const MAX_BLUR_RADIUS = 128;
const XDOG_K = 1.6;
const STAT_SIZE = 512;        // 自動レベル補正のヒストグラムを取る縮小サイズ（長辺）
const WM_ANGLE_PAD = 3.0;     // タイル状の透かしの横の空き（文字の高さの何倍か）

export class Renderer {
  constructor(canvas) {
    this.canvas = canvas;
    const gl = canvas.getContext('webgl2', {
      antialias: false, alpha: false, depth: false, stencil: false,
      premultipliedAlpha: false, preserveDrawingBuffer: false, powerPreference: 'high-performance',
    });
    if (!gl) throw new Error('WebGL2 が使えません（ブラウザまたは GPU が未対応です）');
    if (!gl.getExtension('EXT_color_buffer_float') && !gl.getExtension('EXT_color_buffer_half_float')) {
      throw new Error('浮動小数テクスチャへの描画（EXT_color_buffer_float）が使えません');
    }
    this.gl = gl;
    this.maxTextureSize = gl.getParameter(gl.MAX_TEXTURE_SIZE);
    this.lost = false;
    canvas.addEventListener('webglcontextlost', (e) => { e.preventDefault(); this.lost = true; });
    this.vao = gl.createVertexArray();
    gl.bindVertexArray(this.vao);
    this.fbo = gl.createFramebuffer();
    gl.pixelStorei(gl.UNPACK_ALIGNMENT, 1);
    gl.pixelStorei(gl.PACK_ALIGNMENT, 1);
    gl.pixelStorei(gl.UNPACK_PREMULTIPLY_ALPHA_WEBGL, false);
    gl.pixelStorei(gl.UNPACK_COLORSPACE_CONVERSION_WEBGL, gl.NONE);
    gl.disable(gl.BLEND);
    gl.disable(gl.DEPTH_TEST);
    this.programs = {};
    this.tex = {};            // 名前 → テクスチャ
    this.keys = {};           // パス名 → 作ったときのキー
    this.W = 0;
    this.H = 0;
    this.imageId = 0;
    this.hasMask = false;
    this.level = null;        // 自動レベル補正の [lo, hi]（幅が無い画像は null）
    this.wmRect = null;
    this.lastPasses = [];
  }

  // ---- 低レベルの補助 ----

  program(name, fs) {
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
    gl.attachShader(prog, compile(gl.VERTEX_SHADER, S.VS));
    gl.attachShader(prog, compile(gl.FRAGMENT_SHADER, fs));
    gl.linkProgram(prog);
    if (!gl.getProgramParameter(prog, gl.LINK_STATUS)) {
      throw new Error(`シェーダのリンクに失敗 (${name}): ${gl.getProgramInfoLog(prog)}`);
    }
    const locs = {};
    const p = { prog, loc: (u) => (u in locs ? locs[u] : (locs[u] = gl.getUniformLocation(prog, u))) };
    this.programs[name] = p;
    return p;
  }

  createTexture(internalFormat, w, h, { mips = false, linear = false } = {}) {
    const gl = this.gl;
    const t = gl.createTexture();
    gl.bindTexture(gl.TEXTURE_2D, t);
    const levels = mips ? Math.floor(Math.log2(Math.max(w, h))) + 1 : 1;
    gl.texStorage2D(gl.TEXTURE_2D, levels, internalFormat, w, h);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER,
      mips ? gl.LINEAR_MIPMAP_LINEAR : (linear ? gl.LINEAR : gl.NEAREST));
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, linear ? gl.LINEAR : gl.NEAREST);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    return t;
  }

  /** 画像サイズの作業テクスチャを必要になったときに確保する */
  work(name, internalFormat, opts) {
    if (!this.tex[name]) this.tex[name] = this.createTexture(internalFormat, this.W, this.H, opts);
    return this.tex[name];
  }

  drop(...names) {
    for (const n of names) {
      if (this.tex[n]) { this.gl.deleteTexture(this.tex[n]); delete this.tex[n]; }
    }
  }

  /** target テクスチャへ1パス描く。setup(p) でユニフォームを設定する */
  pass(name, fs, target, w, h, setup) {
    const gl = this.gl;
    const p = this.program(name, fs);
    gl.useProgram(p.prog);
    gl.bindFramebuffer(gl.FRAMEBUFFER, this.fbo);
    gl.framebufferTexture2D(gl.FRAMEBUFFER, gl.COLOR_ATTACHMENT0, gl.TEXTURE_2D, target, 0);
    gl.viewport(0, 0, w, h);
    this.unit = 0;
    setup(p);
    gl.drawArrays(gl.TRIANGLES, 0, 3);
  }

  sampler(p, uniform, texture) {
    const gl = this.gl;
    gl.activeTexture(gl.TEXTURE0 + this.unit);
    gl.bindTexture(gl.TEXTURE_2D, texture);
    gl.uniform1i(p.loc(uniform), this.unit);
    this.unit += 1;
  }

  // ---- 入力 ----

  /** 元画像を設定する（ImageBitmap / canvas など）。マスクと中間結果は捨てる */
  setImage(source, width, height) {
    const gl = this.gl;
    if (width > this.maxTextureSize || height > this.maxTextureSize) {
      throw new Error(`画像が大きすぎます（${width}x${height}）。この環境の上限は ${this.maxTextureSize}px です`);
    }
    this.drop(...Object.keys(this.tex));
    this.keys = {};
    this.W = width;
    this.H = height;
    this.imageId += 1;
    this.hasMask = false;
    this.wmRect = null;
    this.tex.src = this.createTexture(gl.RGBA8, width, height, { mips: true });
    gl.texSubImage2D(gl.TEXTURE_2D, 0, 0, 0, gl.RGBA, gl.UNSIGNED_BYTE, source);
    gl.generateMipmap(gl.TEXTURE_2D);
    this.tex.out = this.createTexture(gl.R8, width, height, { mips: true });
    this.level = this.measureLevel();
  }

  /** 縮小した輝度のヒストグラムから 1〜99 パーセンタイルを求める */
  measureLevel() {
    const gl = this.gl;
    const s = Math.min(1, STAT_SIZE / Math.max(this.W, this.H));
    const sw = Math.max(1, Math.round(this.W * s));
    const sh = Math.max(1, Math.round(this.H * s));
    const tmp = this.createTexture(gl.RGBA8, sw, sh);
    this.pass('lum', S.FS_LUM, tmp, sw, sh, (p) => {
      this.sampler(p, 'uSrc', this.tex.src);
      gl.uniform2f(p.loc('uLevel'), 0, 1);
      gl.uniform2f(p.loc('uInvSize'), 1 / sw, 1 / sh);
      gl.uniform1i(p.loc('uPack'), 1);
    });
    const buf = new Uint8Array(sw * sh * 4);
    gl.readPixels(0, 0, sw, sh, gl.RGBA, gl.UNSIGNED_BYTE, buf);
    gl.framebufferTexture2D(gl.FRAMEBUFFER, gl.COLOR_ATTACHMENT0, gl.TEXTURE_2D, null, 0);
    gl.deleteTexture(tmp);
    const BINS = 4096;
    const hist = new Uint32Array(BINS);
    const n = sw * sh;
    for (let i = 0; i < n; i++) hist[(buf[i * 4] * 256 + buf[i * 4 + 1]) >> 4] += 1;
    const at = (frac) => {
      const want = frac * n;
      let acc = 0;
      for (let b = 0; b < BINS; b++) {
        acc += hist[b];
        if (acc >= want) return (b + 0.5) / BINS;
      }
      return 1;
    };
    const lo = at(0.01);
    const hi = at(0.99);
    // 全面ほぼ同色の画像は補正しない（真っ白の画像を勝手に黒くしない）
    return hi - lo > 0.05 ? [lo, hi] : null;
  }

  /** 人物マスク（白=人物）から符号付き距離場を作る。null で解除 */
  setMask(source) {
    const gl = this.gl;
    this.drop('sdf');
    this.hasMask = false;
    if (!source || !this.W) return;
    const mask = gl.createTexture();
    gl.bindTexture(gl.TEXTURE_2D, mask);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA8, gl.RGBA, gl.UNSIGNED_BYTE, source);
    for (const [k, v] of [[gl.TEXTURE_MIN_FILTER, gl.LINEAR], [gl.TEXTURE_MAG_FILTER, gl.LINEAR],
      [gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE], [gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE]]) {
      gl.texParameteri(gl.TEXTURE_2D, k, v);
    }
    const { W, H } = this;
    const makeSeedTex = () => this.createTexture(gl.RG16UI, W, H);
    let a = makeSeedTex();
    let b = makeSeedTex();
    this.pass('seed', S.FS_SEED, a, W, H, (p) => {
      this.sampler(p, 'uMask', mask);
      gl.uniform2f(p.loc('uSize'), W, H);
    });
    const steps = [];
    for (let s = 2 ** Math.ceil(Math.log2(Math.max(W, H))) / 2; s >= 1; s /= 2) steps.push(s);
    steps.push(1);                               // 仕上げにもう一度（JFA+1）
    for (const step of steps) {
      this.pass('jfa', S.FS_JFA, b, W, H, (p) => {
        this.sampler(p, 'uTex', a);
        gl.uniform1i(p.loc('uStep'), step);
      });
      [a, b] = [b, a];
    }
    const sdf = this.work('sdf', gl.R16F);
    this.pass('sdf', S.FS_SDF, sdf, W, H, (p) => {
      this.sampler(p, 'uSeed', a);
      this.sampler(p, 'uMask', mask);
      gl.uniform2f(p.loc('uSize'), W, H);
    });
    gl.framebufferTexture2D(gl.FRAMEBUFFER, gl.COLOR_ATTACHMENT0, gl.TEXTURE_2D, null, 0);
    gl.deleteTexture(a);
    gl.deleteTexture(b);
    gl.deleteTexture(mask);
    this.hasMask = true;
  }

  // ---- 各パス（キーが変わったときだけ計算） ----

  stale(name, key) {
    if (this.keys[name] === key) return false;
    this.keys[name] = key;
    this.lastPasses.push(name);
    return true;
  }

  ensureLum(autoLevel) {
    const gl = this.gl;
    const level = autoLevel && this.level ? this.level : [0, 1];
    const key = `${this.imageId}|${level[0]}|${level[1]}`;
    const lum = this.work('lum', gl.R16F);
    if (this.stale('lum', key)) {
      this.pass('lum', S.FS_LUM, lum, this.W, this.H, (p) => {
        this.sampler(p, 'uSrc', this.tex.src);
        gl.uniform2f(p.loc('uLevel'), level[0], level[1]);
        gl.uniform2f(p.loc('uInvSize'), 0, 0);
        gl.uniform1i(p.loc('uPack'), 0);
      });
    }
    return key;
  }

  /** 平滑化した輝度のテクスチャを返す（無効なら輝度そのもの） */
  ensureSmooth(lumKey, radiusPx, strength) {
    const gl = this.gl;
    if (radiusPx < 0.5 || strength <= 0) return this.tex.lum;
    const radius = Math.min(MAX_SMOOTH_RADIUS, Math.max(1, Math.round(radiusPx)));
    const smooth = this.work('smooth', gl.R16F);
    if (this.stale('smooth', `${lumKey}|${radiusPx}|${strength}`)) {
      const tmp = this.work('tmp', gl.RG16F);
      const run = (src, dst, dx, dy) => this.pass('bilateral', S.FS_BILATERAL, dst, this.W, this.H, (p) => {
        this.sampler(p, 'uTex', src);
        gl.uniform2i(p.loc('uDir'), dx, dy);
        gl.uniform1i(p.loc('uRadius'), radius);
        gl.uniform1f(p.loc('uInvS2'), 1 / (2 * radiusPx * radiusPx));
        gl.uniform1f(p.loc('uInvR2'), 1 / (2 * strength * strength));
      });
      run(this.tex.lum, tmp, 1, 0);
      run(tmp, smooth, 0, 1);
    }
    return smooth;
  }

  ensureBlur(lumKey, sigmaPx) {
    const gl = this.gl;
    const blur = this.work('blur', gl.RG16F);
    if (this.stale('blur', `${lumKey}|${sigmaPx}`)) {
      const tmp = this.work('tmp', gl.RG16F);
      const s2 = sigmaPx * XDOG_K;
      const radius = Math.min(MAX_BLUR_RADIUS, Math.max(1, Math.ceil(3 * s2)));
      const run = (src, dst, dx, dy, first) => this.pass('blur', S.FS_BLUR, dst, this.W, this.H, (p) => {
        this.sampler(p, 'uTex', src);
        gl.uniform2i(p.loc('uDir'), dx, dy);
        gl.uniform1i(p.loc('uRadius'), radius);
        gl.uniform2f(p.loc('uInv'), 1 / (2 * sigmaPx * sigmaPx), 1 / (2 * s2 * s2));
        gl.uniform1i(p.loc('uFirst'), first);
      });
      run(this.tex.lum, tmp, 1, 0, 1);
      run(tmp, blur, 0, 1, 0);
    }
    return blur;
  }

  /** 透かしの文字を 2D キャンバスで描いてテクスチャにする（R=文字、G=縁取り） */
  ensureWatermark(textValue, sizePx) {
    const gl = this.gl;
    const px = Math.max(4, Math.min(1024, Math.round(sizePx)));
    if (this.stale('wm', `${textValue}|${px}`)) {
      const c = document.createElement('canvas');
      const ctx = c.getContext('2d');
      const font = `700 ${px}px system-ui, -apple-system, "Segoe UI", sans-serif`;
      ctx.font = font;
      const pad = Math.ceil(px * 0.35);
      c.width = Math.min(this.maxTextureSize, 8192, Math.ceil(ctx.measureText(textValue).width) + 2 * pad);
      c.height = Math.ceil(px * 1.3) + 2 * pad;
      ctx.fillStyle = '#000';
      ctx.fillRect(0, 0, c.width, c.height);
      ctx.font = font;
      ctx.textBaseline = 'middle';
      ctx.lineJoin = 'round';
      ctx.lineWidth = Math.max(2, px * 0.22);
      ctx.strokeStyle = '#00ff00';
      ctx.strokeText(textValue, pad, c.height / 2);
      ctx.globalCompositeOperation = 'lighter';
      ctx.fillStyle = '#ff0000';
      ctx.fillText(textValue, pad, c.height / 2);
      this.drop('wm');
      const t = gl.createTexture();
      gl.bindTexture(gl.TEXTURE_2D, t);
      gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA8, gl.RGBA, gl.UNSIGNED_BYTE, c);
      for (const [k, v] of [[gl.TEXTURE_MIN_FILTER, gl.LINEAR], [gl.TEXTURE_MAG_FILTER, gl.LINEAR],
        [gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE], [gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE]]) {
        gl.texParameteri(gl.TEXTURE_2D, k, v);
      }
      this.tex.wm = t;
      this.wmSize = [c.width, c.height, px];
    }
    return this.tex.wm;
  }

  // ---- 描画 ----

  /**
   * 設定から合成パスのユニフォーム一式を作り、必要な前段のパスを用意する。
   * opts.solo: { region, index } でそのトーン層だけを表示（ベタ・線画も隠す）
   */
  prepare(params, { solo = null } = {}) {
    const { W, H } = this;
    const unit = effectiveUnit(params, W);
    const regions = effectiveRegions(params, this.hasMask);
    const bg = params.regions.background;
    const soloing = !!solo;
    const mode = soloing ? 0 : { keep: 0, white: 1, lines: 2 }[regions.mode] || 0;
    const lineStrength = params.lines.enabled && !soloing ? params.lines.strength : 0;
    const needLines = lineStrength > 0 || mode === 2;

    const sets = ['person', 'background'].map((name, r) => {
      const reg = regions[name];
      // ソロ表示は、連動中の背景なら人物の層として扱う
      const soloHere = soloing && (solo.region === name || (name === 'background' && reg.linked && solo.region === 'person'));
      const layers = reg.layers
        .map((l, i) => ({ l, i }))
        .filter(({ l, i }) => l.type === 'hatch' && (soloing ? soloHere && i === solo.index : l.enabled))
        .slice(0, MAX_LAYERS);
      const active = r === 0 || mode === 0;        // 背景の層とベタは「層で描く」ときだけ
      return {
        layers: active ? layers : [],
        solidOn: active && reg.solid.enabled && !soloing,
        solid: reg.solid,
        levels: reg.levels,
        // 統計用: 設定上の層番号（無効な層は飛ばして詰めてあるため）
        index: (active ? layers : []).map(({ i }) => i),
      };
    });

    const lumKey = this.ensureLum(params.input.autoLevel);
    const needTone = sets.some((x) => x.layers.length || x.solidOn);
    const tone = needTone
      ? this.ensureSmooth(lumKey, params.input.smoothRadius * unit, params.input.smoothStrength)
      : this.tex.lum;
    const blur = needLines ? this.ensureBlur(lumKey, params.lines.sigma * unit) : null;

    const wm = effectiveWatermark(params);
    let wmMode = 0;
    let wmTex = null;
    let rect = [0, 0, 1, 1];
    let period = [1, 1];
    this.wmRect = null;
    if (wm.enabled) {
      wmTex = this.ensureWatermark(wm.text, wm.size * unit);
      const [tw, th, px] = this.wmSize;
      if (wm.position === 'tile') {
        wmMode = 2;
        rect = [0, 0, tw, th];
        period = [tw + Math.round(px * WM_ANGLE_PAD), th + Math.round(px * 2.5)];
        this.wmRect = { x: 0, y: 0, w: W, h: H, tile: true };
      } else {
        wmMode = 1;
        const margin = Math.round(px * 0.6);
        const left = Math.max(0, Math.min(margin, W - tw));
        const right = Math.max(0, W - tw - margin);
        const top = Math.max(0, Math.min(margin, H - th));
        const bottom = Math.max(0, H - th - margin);
        const pos = {
          tl: [left, top], tr: [right, top], bl: [left, bottom], br: [right, bottom],
          center: [Math.max(0, Math.round((W - tw) / 2)), Math.max(0, Math.round((H - th) / 2))],
        }[wm.position] || [right, bottom];
        rect = [pos[0], pos[1], tw, th];
        this.wmRect = { x: pos[0], y: pos[1], w: tw, h: th, tile: false };
      }
    }

    const A = new Float32Array(MAX_LAYERS * 8);
    const B = new Float32Array(MAX_LAYERS * 8);
    const C = new Float32Array(MAX_LAYERS * 8);
    const levels = new Float32Array(8);
    const solid = new Float32Array(8);
    sets.forEach((set, r) => {
      set.layers.forEach(({ l }, k) => {
        const a = (l.angle * Math.PI) / 180;
        const spacing = Math.max(0.25, l.spacing * unit);
        const at = (r * MAX_LAYERS + k) * 4;
        A.set([l.threshold, l.soft, Math.sin(a), Math.cos(a)], at);
        B.set([spacing, l.width * unit, l.widthByDarkness, l.opacity], at);
        C.set([l.phase, 0, 0, 0], at);
      });
      levels.set([set.levels.brightness, set.levels.contrast, 1 / Math.max(0.01, set.levels.gamma), 0], r * 4);
      solid.set([set.solidOn ? 1 : 0, set.solid.threshold, set.solid.soft, 0], r * 4);
    });

    const gl = this.gl;
    const apply = (p, statMode, sw, sh) => {
      this.sampler(p, 'uLum', tone);
      this.sampler(p, 'uBlur', blur || this.tex.lum);
      this.sampler(p, 'uSdf', this.tex.sdf || this.tex.lum);
      this.sampler(p, 'uWm', wmTex || this.tex.lum);
      gl.uniform2i(p.loc('uCount'), sets[0].layers.length, sets[1].layers.length);
      gl.uniform4fv(p.loc('uA'), A);
      gl.uniform4fv(p.loc('uB'), B);
      gl.uniform4fv(p.loc('uC'), C);
      gl.uniform4fv(p.loc('uLevels'), levels);
      gl.uniform4fv(p.loc('uSolid'), solid);
      gl.uniform4f(p.loc('uLine'), lineStrength, params.lines.tau, params.lines.epsilon, params.lines.phi);
      gl.uniform1i(p.loc('uLineOn'), blur ? 1 : 0);
      gl.uniform1i(p.loc('uHasMask'), this.hasMask ? 1 : 0);
      gl.uniform4f(p.loc('uBg'), mode, bg.grow * unit, bg.silhouetteWidth * unit, bg.lineStrength);
      gl.uniform4f(p.loc('uWmRect'), rect[0], rect[1], rect[2], rect[3]);
      gl.uniform4f(p.loc('uWmP'), wmMode, wm.opacity, period[0], period[1]);
      gl.uniform1i(p.loc('uBinary'), params.output.binary ? 1 : 0);
      gl.uniform1i(p.loc('uMode'), statMode);
      gl.uniform2f(p.loc('uSampleScale'), statMode ? W / sw : 1, statMode ? H / sh : 1);
      gl.uniform2i(p.loc('uSize'), W, H);
    };
    return { apply, unit, sets, mode };
  }

  /** 設定どおりに出力テクスチャを作り直す */
  render(params, opts = {}) {
    if (!this.W) return { ms: 0, passes: [] };
    const gl = this.gl;
    const t0 = performance.now();
    this.lastPasses = [];
    const { W, H } = this;
    const prep = this.prepare(params, opts);
    this.pass('composite', S.FS_COMPOSITE, this.tex.out, W, H, (p) => prep.apply(p, 0, W, H));
    this.lastPasses.push('composite');
    // 1画素読んで GPU の完了を待つ（所要時間を正しく測るため）
    gl.readPixels(0, 0, 1, 1, gl.RGBA, gl.UNSIGNED_BYTE, new Uint8Array(4));
    gl.framebufferTexture2D(gl.FRAMEBUFFER, gl.COLOR_ATTACHMENT0, gl.TEXTURE_2D, null, 0);
    gl.bindTexture(gl.TEXTURE_2D, this.tex.out);
    gl.generateMipmap(gl.TEXTURE_2D);
    return { ms: performance.now() - t0, passes: this.lastPasses.slice(), unit: prep.unit };
  }

  /**
   * 描画結果の統計（画像を見ずに数値で調整するためのもの）。
   * 縮小した格子で画素を抜き出して数える（出力そのものは読まない）。
   */
  stats(params, opts = {}) {
    if (!this.W) return null;
    const gl = this.gl;
    const { W, H } = this;
    const s = Math.min(1, STAT_SIZE / Math.max(W, H));
    const sw = Math.max(1, Math.round(W * s));
    const sh = Math.max(1, Math.round(H * s));
    const before = this.lastPasses;
    this.lastPasses = [];
    const prep = this.prepare(params, opts);
    this.lastPasses = before;
    const tmp = this.createTexture(gl.RGBA8, sw, sh);
    const read = (mode) => {
      this.pass('composite', S.FS_COMPOSITE, tmp, sw, sh, (p) => prep.apply(p, mode, sw, sh));
      const buf = new Uint8Array(sw * sh * 4);
      gl.readPixels(0, 0, sw, sh, gl.RGBA, gl.UNSIGNED_BYTE, buf);
      return buf;
    };
    const a = read(1);
    const b = read(2);
    gl.framebufferTexture2D(gl.FRAMEBUFFER, gl.COLOR_ATTACHMENT0, gl.TEXTURE_2D, null, 0);
    gl.deleteTexture(tmp);

    const make = () => ({
      n: 0, black: 0, density: 0, solid: 0, line: 0, layers: new Float64Array(MAX_LAYERS),
      lum: new Uint32Array(256), adjusted: new Uint32Array(256),
    });
    const acc = { all: make(), person: make(), background: make() };
    const n = sw * sh;
    for (let i = 0; i < n; i++) {
      const v = a[i * 4];
      const person = a[i * 4 + 1] >= 128;
      const bits = b[i * 4 + 2];
      for (const t of [acc.all, person ? acc.person : acc.background]) {
        t.n += 1;
        if (v < 128) t.black += 1;
        t.density += 255 - v;
        if (a[i * 4 + 3] >= 128) t.solid += 1;
        if (a[i * 4 + 2] >= 128) t.line += 1;
        t.lum[b[i * 4]] += 1;
        t.adjusted[b[i * 4 + 1]] += 1;
        if (t !== acc.all) for (let k = 0; k < MAX_LAYERS; k++) if (bits & (1 << k)) t.layers[k] += 1;
      }
    }
    const round = (x) => Math.round(x * 10000) / 10000;
    const summary = (hist, count) => {
      if (!count) return null;
      let sum = 0;
      for (let k = 0; k < 256; k++) sum += hist[k] * k;
      const at = (frac) => {
        let c = 0;
        for (let k = 0; k < 256; k++) { c += hist[k]; if (c >= frac * count) return round(k / 255); }
        return 1;
      };
      return { mean: round(sum / count / 255), p10: at(0.1), p25: at(0.25), median: at(0.5), p75: at(0.75), p90: at(0.9) };
    };
    const output = {};
    const input = {};
    ['all', 'person', 'background'].forEach((name) => {
      const t = acc[name];
      const o = {
        areaRatio: round(t.n / n),
        blackRatio: t.n ? round(t.black / t.n) : null,
        meanDensity: t.n ? round(t.density / t.n / 255) : null,
        solidRatio: t.n ? round(t.solid / t.n) : null,
        lineRatio: t.n ? round(t.line / t.n) : null,
      };
      if (name !== 'all') {
        const set = prep.sets[name === 'person' ? 0 : 1];
        // 層ごとの「描かれている画素の割合」（その領域の中で）。layer は設定上の層番号
        o.layers = t.n ? set.index.map((layerIndex, k) => ({ layer: layerIndex, coverage: round(t.layers[k] / t.n) })) : [];
      }
      output[name] = o;
      input[name] = { luminance: summary(t.lum, t.n), adjusted: summary(t.adjusted, t.n) };
    });
    return {
      width: W, height: H, unit: round(prep.unit), hasMask: this.hasMask, samples: n,
      backgroundMode: ['keep', 'white', 'lines'][prep.mode], output, input,
    };
  }

  /** 画面のキャンバスへ表示する。view = { zoom, cx, cy, original, split }（split は x のデバイスpx、無ければ -1） */
  draw(view) {
    const gl = this.gl;
    const cw = this.canvas.width;
    const ch = this.canvas.height;
    gl.bindFramebuffer(gl.FRAMEBUFFER, null);
    gl.viewport(0, 0, cw, ch);
    if (!this.W) {
      gl.clearColor(0.16, 0.16, 0.18, 1);
      gl.clear(gl.COLOR_BUFFER_BIT);
      return;
    }
    const p = this.program('display', S.FS_DISPLAY);
    gl.useProgram(p.prog);
    this.unit = 0;
    this.sampler(p, 'uOut', this.tex.out);
    this.sampler(p, 'uSrc', this.tex.src);
    gl.uniform2f(p.loc('uCanvas'), cw, ch);
    gl.uniform2f(p.loc('uCenter'), view.cx, view.cy);
    gl.uniform1f(p.loc('uZoom'), view.zoom);
    gl.uniform2f(p.loc('uImg'), this.W, this.H);
    gl.uniform1f(p.loc('uSplit'), view.split ?? -1);
    gl.uniform1i(p.loc('uOriginal'), view.original ? 1 : 0);
    gl.drawArrays(gl.TRIANGLES, 0, 3);
  }

  // ---- 読み出し ----

  /** 出力（8bit グレースケール）の矩形を読む。行は画像の上から順 */
  readPixels(x = 0, y = 0, w = this.W, h = this.H) {
    const gl = this.gl;
    gl.bindFramebuffer(gl.FRAMEBUFFER, this.fbo);
    gl.framebufferTexture2D(gl.FRAMEBUFFER, gl.COLOR_ATTACHMENT0, gl.TEXTURE_2D, this.tex.out, 0);
    const out = new Uint8Array(w * h);
    const STRIP = Math.max(1, Math.floor((1 << 22) / Math.max(1, w)));   // 1回に約4M画素まで
    const buf = new Uint8Array(w * Math.min(STRIP, h) * 4);
    for (let y0 = 0; y0 < h; y0 += STRIP) {
      const n = Math.min(STRIP, h - y0);
      gl.readPixels(x, y + y0, w, n, gl.RGBA, gl.UNSIGNED_BYTE, buf);
      const base = y0 * w;
      for (let i = 0, m = w * n; i < m; i++) out[base + i] = buf[i * 4];
    }
    gl.framebufferTexture2D(gl.FRAMEBUFFER, gl.COLOR_ATTACHMENT0, gl.TEXTURE_2D, null, 0);
    return out;
  }
}
