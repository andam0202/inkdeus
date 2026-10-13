// WebGL2 の描画パイプライン。プレビューと書き出しは同じ出力テクスチャを使う（WYSIWYG）。
// 依存する設定が変わったパスだけを再計算する（各パスが「何から作ったか」のキーを覚えている）。

import * as S from './shaders.js';
import { effectiveUnit, effectiveRegions, effectiveWatermark, MAX_LAYERS, MAX_PARTS } from './schema.js';
import { Paint, layerTarget } from './paint.js';

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
    // 合成パスの制限。テクスチャは最大 14 枚（輝度・ぼかし・距離場・透かし・部分マップ・消去マスク 8・ペン）で、
    // WebGL2 が保証する 16 に収まる。uniform の本数は、使う層の総数に合わせて配列の長さを選ぶ
    this.maxUniformVectors = gl.getParameter(gl.MAX_FRAGMENT_UNIFORM_VECTORS);
    this.maxTextureUnits = gl.getParameter(gl.MAX_TEXTURE_IMAGE_UNITS);
    this.maxLayers = Math.max(S.LAYER_CAPACITIES[0],
      ...S.LAYER_CAPACITIES.filter((c) => S.uniformVectors(c) <= this.maxUniformVectors));
    this.parts = [];          // 部分マップに入っている部分名（番号 1〜 の順）
    this.dropped = [];        // 層の総数が上限を超えたために、人物と同じ設定で描いた部分
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
    this.paint = new Paint(this);     // 消しゴムのマスクとペンの絵（人が画面で描く物）
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
    this.parts = [];
    this.wmRect = null;
    this.tex.src = this.createTexture(gl.RGBA8, width, height, { mips: true });
    gl.texSubImage2D(gl.TEXTURE_2D, 0, 0, 0, gl.RGBA, gl.UNSIGNED_BYTE, source);
    gl.generateMipmap(gl.TEXTURE_2D);
    this.tex.out = this.createTexture(gl.R8, width, height, { mips: true });
    this.level = this.measureLevel();
    this.paint.reset();               // 消しゴム・ペンは画像ごと（引き継がない）
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
    if (source.data instanceof Uint8Array) {
      // 画素の並び（1 バイト/画素。手直しを合成した人物マスク）をそのまま渡す
      gl.pixelStorei(gl.UNPACK_ALIGNMENT, 1);
      gl.texImage2D(gl.TEXTURE_2D, 0, gl.R8, source.width, source.height, 0, gl.RED, gl.UNSIGNED_BYTE, source.data);
      gl.pixelStorei(gl.UNPACK_ALIGNMENT, 4);
    } else {
      gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA8, gl.RGBA, gl.UNSIGNED_BYTE, source);
    }
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

  /**
   * 部分マスク（[{ name, source }]。白 = その部分）から、部分の番号（1〜）の地図を 1 枚の R8 に作る。
   * 部分は互いに重ならない前提（重なったら、並びの後ろの部分が勝つ）。空の並び・null で解除
   */
  setParts(list) {
    this.drop('partMap');
    this.parts = [];
    for (const { name, source } of list || []) this.addPart(name, source);
  }

  /**
   * 部分マップを、画素の並び（1 バイト/画素。0 = どの部分でもない、1〜 = names の番号）からそのまま作る
   * （手直しを合成した結果を渡すための物。画像と同じ大きさであること）
   */
  setPartMap(codes, names) {
    const gl = this.gl;
    this.drop('partMap');
    this.parts = [];
    if (!this.W || !codes || !names || !names.length) return;
    const map = this.work('partMap', gl.R8);
    gl.bindTexture(gl.TEXTURE_2D, map);
    gl.pixelStorei(gl.UNPACK_ALIGNMENT, 1);
    gl.texSubImage2D(gl.TEXTURE_2D, 0, 0, 0, this.W, this.H, gl.RED, gl.UNSIGNED_BYTE, codes);
    gl.pixelStorei(gl.UNPACK_ALIGNMENT, 4);
    this.parts = names.slice(0, MAX_PARTS);
  }

  /** マスク・部分マップを入れ替えたあと、それに依る中間結果を作り直させる */
  invalidate() {
    this.keys = {};
  }

  /** 部分マスクを 1 つ、部分マップへ足す（1 枚ずつ読んで足せるように。足せたら true） */
  addPart(name, source) {
    const gl = this.gl;
    if (!this.W || !source || this.parts.length >= MAX_PARTS || this.parts.includes(name)) return false;
    const { W, H } = this;
    let map = this.tex.partMap;
    if (!map) {
      map = this.work('partMap', gl.R8);
      gl.bindFramebuffer(gl.FRAMEBUFFER, this.fbo);
      gl.framebufferTexture2D(gl.FRAMEBUFFER, gl.COLOR_ATTACHMENT0, gl.TEXTURE_2D, map, 0);
      gl.viewport(0, 0, W, H);
      gl.clearColor(0, 0, 0, 0);
      gl.clear(gl.COLOR_BUFFER_BIT);
    }
    const mask = gl.createTexture();
    gl.bindTexture(gl.TEXTURE_2D, mask);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA8, gl.RGBA, gl.UNSIGNED_BYTE, source);
    for (const [k, v] of [[gl.TEXTURE_MIN_FILTER, gl.LINEAR], [gl.TEXTURE_MAG_FILTER, gl.LINEAR],
      [gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE], [gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE]]) {
      gl.texParameteri(gl.TEXTURE_2D, k, v);
    }
    this.parts.push(name);
    this.pass('partmap', S.FS_PARTMAP, map, W, H, (p) => {
      this.sampler(p, 'uMask', mask);
      gl.uniform2f(p.loc('uSize'), W, H);
      gl.uniform1f(p.loc('uCode'), this.parts.length);
    });
    gl.framebufferTexture2D(gl.FRAMEBUFFER, gl.COLOR_ATTACHMENT0, gl.TEXTURE_2D, null, 0);
    gl.deleteTexture(mask);
    return true;
  }

  /**
   * 画像の座標 (x, y) がどの領域か。{ part: 部分名 | null, inside: 人物マスク（広げ込み）の内側か }。画像の外は null
   */
  regionAt(x, y, growPx = 0) {
    const gl = this.gl;
    const ix = Math.floor(x);
    const iy = Math.floor(y);
    if (!this.W || ix < 0 || iy < 0 || ix >= this.W || iy >= this.H) return null;
    gl.bindFramebuffer(gl.FRAMEBUFFER, this.fbo);
    let part = null;
    if (this.tex.partMap) {
      const buf = new Uint8Array(4);
      gl.framebufferTexture2D(gl.FRAMEBUFFER, gl.COLOR_ATTACHMENT0, gl.TEXTURE_2D, this.tex.partMap, 0);
      gl.readPixels(ix, iy, 1, 1, gl.RGBA, gl.UNSIGNED_BYTE, buf);
      part = buf[0] > 0 ? this.parts[buf[0] - 1] || null : null;
    }
    let inside = true;
    if (this.hasMask && this.tex.sdf) {
      const buf = new Float32Array(4);
      gl.framebufferTexture2D(gl.FRAMEBUFFER, gl.COLOR_ATTACHMENT0, gl.TEXTURE_2D, this.tex.sdf, 0);
      gl.readPixels(ix, iy, 1, 1, gl.RGBA, gl.FLOAT, buf);
      inside = buf[0] + growPx >= 0;
    }
    gl.framebufferTexture2D(gl.FRAMEBUFFER, gl.COLOR_ATTACHMENT0, gl.TEXTURE_2D, null, 0);
    return { part, inside };
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
    const regions = effectiveRegions(params, this.hasMask, this.parts);
    const bg = params.regions.background;
    const soloing = !!solo;
    // solo.lines: 線画だけを薄く表示する（マスクの手直しのとき、領域の色分けを見やすくするため）
    const linesOnly = soloing && solo.lines === true;
    const mode = soloing ? 0 : { keep: 0, white: 1, lines: 2 }[regions.mode] || 0;
    const lineStrength = linesOnly ? 0.45 : (params.lines.enabled && !soloing ? params.lines.strength : 0);

    // 領域の並び: 0 = 人物、1 = 背景、2〜 = 別の設定にした部分（linked の部分は、人物の領域で描く）
    const makeSet = (name, reg, r) => {
      // ソロ表示は、連動中の背景なら人物の層として扱う
      const soloHere = soloing && (solo.region === name || (name === 'background' && reg.linked && solo.region === 'person'));
      const layers = reg.layers
        .map((l, i) => ({ l, i }))
        .filter(({ l, i }) => l.type === 'hatch' && (soloing ? soloHere && i === solo.index : l.enabled))
        .slice(0, MAX_LAYERS);
      const active = r !== 1 || mode === 0;        // 背景の層とベタは「層で描く」ときだけ
      return {
        name,
        // 人物と同じ層の並びを使う（配列には 1 回だけ詰める）
        shared: name === 'background' && reg.linked && !(soloing && solo.region === 'background'),
        layers: active ? layers : [],
        solidOn: active && reg.solid.enabled && !soloing,
        solid: reg.solid,
        levels: reg.levels,
        // 統計用: 設定上の層番号（無効な層は飛ばして詰めてあるため）
        index: (active ? layers : []).map(({ i }) => i),
      };
    };
    const sets = [makeSet('person', regions.person, 0), makeSet('background', regions.background, 1)];
    const partSet = new Int32Array(8);              // 部分の番号（1〜）→ 領域の番号（0 = 人物と同じ設定で描く）
    const own = (set) => (set.shared ? 0 : set.layers.length);
    let total = own(sets[0]) + own(sets[1]);
    this.dropped = [];
    regions.parts.forEach((part, i) => {
      if (part.linked) return;
      const set = makeSet(part.name, part, sets.length);
      // 層の総数が、この環境の uniform の上限から決まる数を超えるなら、その部分は人物と同じ設定で描く
      if (sets.length >= S.MAX_SETS || total + set.layers.length > this.maxLayers) {
        this.dropped.push(part.name);
        return;
      }
      total += set.layers.length;
      partSet[i + 1] = sets.length;
      sets.push(set);
    });
    const capacity = S.LAYER_CAPACITIES.find((c) => c >= total) || this.maxLayers;
    const knockout = !soloing && sets.some((x) => x.solidOn && x.solid.lineKnockout > 0);
    const needLines = lineStrength > 0 || mode === 2 || knockout;

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

    // 層は、使う物だけを 1 本の配列に詰める（領域ごとに「先頭の番号と数」で引く）。
    // 消しゴム: 各層・ベタ・線画・すべて の消去マスクのスロット（描いた物が無ければ -1）も一緒に渡す
    const paint = this.paint;
    const A = new Float32Array(capacity * 4);
    const B = new Float32Array(capacity * 4);
    const C = new Float32Array(capacity * 4);
    const ranges = new Int32Array(S.MAX_SETS * 2);
    const levels = new Float32Array(S.MAX_SETS * 4);
    const solid = new Float32Array(S.MAX_SETS * 4);
    const slots = [];
    let next = 0;
    sets.forEach((set, r) => {
      if (set.shared) {
        ranges.set(set.layers.length ? [ranges[0], ranges[1]] : [0, 0], r * 2);
      } else {
        ranges.set([next, set.layers.length], r * 2);
        set.layers.forEach(({ l }) => {
          const a = (l.angle * Math.PI) / 180;
          const spacing = Math.max(0.25, l.spacing * unit);
          const slot = l.id ? paint.slotOf(layerTarget(l.id)) : -1;
          const at = next * 4;
          A.set([l.threshold, l.soft, Math.sin(a), Math.cos(a)], at);
          B.set([spacing, l.width * unit, l.widthByDarkness, l.opacity], at);
          C.set([l.phase, slot, 0, 0], at);
          slots.push(slot);
          next += 1;
        });
      }
      const solidSlot = paint.slotOf(`solid-${set.name}`);
      slots.push(solidSlot);
      levels.set([set.levels.brightness, set.levels.contrast, 1 / Math.max(0.01, set.levels.gamma), solidSlot], r * 4);
      solid.set([set.solidOn ? 1 : 0, set.solid.threshold, set.solid.soft, set.solid.lineKnockout || 0], r * 4);
    });
    const miscSlots = [-1, -1, paint.slotOf('lines'), paint.slotOf('all')];
    const partMap = this.tex.partMap || null;

    const gl = this.gl;
    const apply = (p, statMode, sw, sh) => {
      this.sampler(p, 'uLum', tone);
      this.sampler(p, 'uBlur', blur || this.tex.lum);
      this.sampler(p, 'uSdf', this.tex.sdf || this.tex.lum);
      this.sampler(p, 'uWm', wmTex || this.tex.lum);
      // 部分マップは、別の設定にした部分があるとき（と、統計で部分ごとに数えるとき）だけ読む
      const partOn = !!partMap && (sets.length > 2 || statMode !== 0);
      if (partOn) this.sampler(p, 'uPartMap', partMap);
      else gl.uniform1i(p.loc('uPartMap'), 0);
      gl.uniform1i(p.loc('uPartOn'), partOn ? 1 : 0);
      gl.uniform4iv(p.loc('uPartSet'), partSet);
      gl.uniform2iv(p.loc('uRange'), ranges);
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
      paint.bind(p, slots, miscSlots, !soloing);          // ソロ表示の間は、ペンの絵は隠す
    };
    return { apply, unit, sets, mode, partSet, capacity, program: `composite${capacity}`, fs: S.composite(capacity) };
  }

  /**
   * 設定どおりに出力テクスチャを作り直す。
   * opts.rect = { x, y, w, h }（画像の px）を渡すと、その範囲だけ合成し直す（筆の最中の更新用）
   */
  render(params, opts = {}) {
    if (!this.W) return { ms: 0, passes: [] };
    const gl = this.gl;
    const t0 = performance.now();
    this.lastPasses = [];
    const { W, H } = this;
    const prep = this.prepare(params, opts);
    if (opts.rect) {
      gl.enable(gl.SCISSOR_TEST);
      gl.scissor(opts.rect.x, opts.rect.y, opts.rect.w, opts.rect.h);
    }
    try {
      this.pass(prep.program, prep.fs, this.tex.out, W, H, (p) => prep.apply(p, 0, W, H));
    } finally {
      gl.disable(gl.SCISSOR_TEST);
    }
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
      this.pass(prep.program, prep.fs, tmp, sw, sh, (p) => prep.apply(p, mode, sw, sh));
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
    // 部分ごとの集計（その部分のマスクの画素。人物と同じ設定で描いている部分は、人物の集計にも入る）
    const partAcc = this.parts.map(() => make());
    const n = sw * sh;
    for (let i = 0; i < n; i++) {
      const v = a[i * 4];
      const code = Math.min(b[i * 4 + 3], partAcc.length);
      const bits = b[i * 4 + 2];
      const targets = [acc.all];
      if (!(code > 0 && prep.partSet[code] >= 2)) targets.push(a[i * 4 + 1] >= 128 ? acc.person : acc.background);
      if (code > 0) targets.push(partAcc[code - 1]);
      for (const t of targets) {
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
    const put = (name, t, set, extra = {}) => {
      const o = {
        areaRatio: round(t.n / n),
        blackRatio: t.n ? round(t.black / t.n) : null,
        meanDensity: t.n ? round(t.density / t.n / 255) : null,
        solidRatio: t.n ? round(t.solid / t.n) : null,
        lineRatio: t.n ? round(t.line / t.n) : null,
        ...extra,
      };
      if (set) {
        // 層ごとの「描かれている画素の割合」（その領域の中で）。layer は設定上の層番号
        o.layers = t.n ? set.index.map((layerIndex, k) => ({ layer: layerIndex, coverage: round(t.layers[k] / t.n) })) : [];
      }
      output[name] = o;
      input[name] = { luminance: summary(t.lum, t.n), adjusted: summary(t.adjusted, t.n) };
    };
    put('all', acc.all, null);
    put('person', acc.person, prep.sets[0]);
    put('background', acc.background, prep.sets[1]);
    // 部分: linked = 人物と同じ設定で描いている（そのときの layers は人物の層）
    this.parts.forEach((name, k) => put(name, partAcc[k], prep.sets[prep.partSet[k + 1]], { linked: prep.partSet[k + 1] < 2 }));
    return {
      width: W, height: H, unit: round(prep.unit), hasMask: this.hasMask, samples: n,
      parts: this.parts.slice(),
      // 層の総数が上限を超えたために、人物と同じ設定で描いた部分（普通は空）
      ...(this.dropped.length ? { droppedParts: this.dropped.slice() } : {}),
      backgroundMode: ['keep', 'white', 'lines'][prep.mode], output, input,
      // 消しゴム・ペン（人が画面で描いた物）。上の数値は、それらを反映した最終出力に対する物
      paint: { targets: this.paint.targets(), penUsed: this.paint.penOn() },
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
    // 領域の強調表示（view.highlight = { kind: 1 人物 / 2 背景 / 3 部分, part: 部分の番号, grow: px, own: ビット集合 }）
    const hi = view.highlight || null;
    const sdf = hi && this.hasMask ? this.tex.sdf : null;
    const map = hi ? this.tex.partMap : null;
    if (sdf) this.sampler(p, 'uSdf', sdf); else gl.uniform1i(p.loc('uSdf'), 0);
    if (map) this.sampler(p, 'uPartMap', map); else gl.uniform1i(p.loc('uPartMap'), 0);
    gl.uniform4i(p.loc('uHi'), hi ? hi.kind : 0, hi ? hi.part || 0 : 0, sdf ? 1 : 0, map ? 1 : 0);
    gl.uniform1f(p.loc('uHiGrow'), hi ? hi.grow || 0 : 0);
    gl.uniform1i(p.loc('uHiOwn'), hi ? hi.own || 0 : 0);
    gl.drawArrays(gl.TRIANGLES, 0, 3);
  }

  /** 画面のキャンバスの 1 画素（デバイスpx。左上が原点）を読む。draw の直後に呼ぶこと（テスト・確認用） */
  readCanvas(px, py) {
    const gl = this.gl;
    const buf = new Uint8Array(4);
    gl.bindFramebuffer(gl.FRAMEBUFFER, null);
    gl.readPixels(Math.floor(px), this.canvas.height - 1 - Math.floor(py), 1, 1, gl.RGBA, gl.UNSIGNED_BYTE, buf);
    return Array.from(buf);
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
