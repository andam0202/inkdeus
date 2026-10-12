// フラグメントシェーダ群。オフスクリーンのパスはすべて
// 「フレームバッファの (x, y) 画素 = 画像の (x, y) 画素（y は下向き）」として描く。

export const VS = `#version 300 es
void main() {
  vec2 p = vec2(float((gl_VertexID << 1) & 2), float(gl_VertexID & 2));
  gl_Position = vec4(p * 2.0 - 1.0, 0.0, 1.0);
}`;

const HEAD = `#version 300 es
precision highp float;
precision highp int;
precision highp sampler2D;
precision highp usampler2D;
`;

// 1. 輝度: sRGB → Lab の L（0..1）。uPack=1 のときは縮小ヒストグラム用に 16bit を RG に詰める
export const FS_LUM = HEAD + `
uniform sampler2D uSrc;
uniform vec2 uLevel;      // lo, hi（レベル補正なしなら 0, 1）
uniform vec2 uInvSize;    // uPack=1 のときの 1/出力サイズ
uniform int uPack;
out vec4 o;
float toLinear(float c) { return c <= 0.04045 ? c / 12.92 : pow((c + 0.055) / 1.055, 2.4); }
float labL(vec3 c) {
  float y = dot(vec3(toLinear(c.r), toLinear(c.g), toLinear(c.b)), vec3(0.2126729, 0.7151522, 0.0721750));
  float f = y > 0.008856 ? pow(y, 1.0 / 3.0) : 7.787 * y + 16.0 / 116.0;
  return clamp(1.16 * f - 0.16, 0.0, 1.0);
}
void main() {
  vec4 s = uPack == 1 ? textureLod(uSrc, gl_FragCoord.xy * uInvSize, 0.0)
                      : texelFetch(uSrc, ivec2(gl_FragCoord.xy), 0);
  float L = labL(mix(vec3(1.0), s.rgb, s.a));     // 透明部分は白地に合成
  if (uPack == 1) {
    float v = floor(L * 65535.0 + 0.5);
    o = vec4(floor(v / 256.0) / 255.0, mod(v, 256.0) / 255.0, 0.0, 1.0);
    return;
  }
  o = vec4(clamp((L - uLevel.x) / (uLevel.y - uLevel.x), 0.0, 1.0), 0.0, 0.0, 1.0);
}`;

// 2. エッジを保つ平滑化（分離型バイラテラル近似の1方向ぶん）
export const FS_BILATERAL = HEAD + `
uniform sampler2D uTex;
uniform ivec2 uDir;
uniform int uRadius;
uniform float uInvS2;     // 1 / (2 σs²)
uniform float uInvR2;     // 1 / (2 σr²)
out vec4 o;
void main() {
  ivec2 p = ivec2(gl_FragCoord.xy);
  ivec2 mx = textureSize(uTex, 0) - 1;
  float c = texelFetch(uTex, p, 0).r;
  float sum = c, wsum = 1.0;
  for (int i = 1; i <= 64; i++) {
    if (i > uRadius) break;
    float ws = exp(-float(i * i) * uInvS2);
    float a = texelFetch(uTex, clamp(p + uDir * i, ivec2(0), mx), 0).r;
    float b = texelFetch(uTex, clamp(p - uDir * i, ivec2(0), mx), 0).r;
    float wa = ws * exp(-(a - c) * (a - c) * uInvR2);
    float wb = ws * exp(-(b - c) * (b - c) * uInvR2);
    sum += wa * a + wb * b;
    wsum += wa + wb;
  }
  o = vec4(sum / wsum, 0.0, 0.0, 1.0);
}`;

// 3. XDoG 用のガウスぼかし。σ と 1.6σ の2本を RG に同時に持つ（D = R − τ·G は合成時に計算）
export const FS_BLUR = HEAD + `
uniform sampler2D uTex;
uniform ivec2 uDir;
uniform int uRadius;
uniform vec2 uInv;        // 1/(2σ1²), 1/(2σ2²)
uniform int uFirst;       // 1: 入力は輝度（R だけ）
out vec4 o;
vec2 tap(ivec2 q) {
  vec2 v = texelFetch(uTex, q, 0).rg;
  return uFirst == 1 ? v.rr : v;
}
void main() {
  ivec2 p = ivec2(gl_FragCoord.xy);
  ivec2 mx = textureSize(uTex, 0) - 1;
  vec2 sum = tap(p), wsum = vec2(1.0);
  for (int i = 1; i <= 128; i++) {
    if (i > uRadius) break;
    vec2 w = exp(-float(i * i) * uInv);
    sum += w * (tap(clamp(p + uDir * i, ivec2(0), mx)) + tap(clamp(p - uDir * i, ivec2(0), mx)));
    wsum += 2.0 * w;
  }
  o = vec4(sum / wsum, 0.0, 1.0);
}`;

// 4a. Jump Flooding の種: マスク内側で、外側の4近傍を持つ画素（＝輪郭）
export const FS_SEED = HEAD + `
uniform sampler2D uMask;
uniform vec2 uSize;
out uvec4 o;
bool inside(vec2 p) { return texture(uMask, p / uSize).r >= 0.5; }
void main() {
  vec2 p = gl_FragCoord.xy;
  bool edge = false;
  if (inside(p)) {
    vec2 offs[4] = vec2[4](vec2(1.0, 0.0), vec2(-1.0, 0.0), vec2(0.0, 1.0), vec2(0.0, -1.0));
    for (int i = 0; i < 4; i++) {
      vec2 q = p + offs[i];
      // 画像の外は輪郭とみなさない（画像の端に線を引かない）
      if (q.x > 0.0 && q.y > 0.0 && q.x < uSize.x && q.y < uSize.y && !inside(q)) edge = true;
    }
  }
  o = edge ? uvec4(uvec2(p), 0u, 1u) : uvec4(65535u, 65535u, 0u, 1u);
}`;

// 4b. Jump Flooding の1ステップ
export const FS_JFA = HEAD + `
uniform usampler2D uTex;
uniform int uStep;
out uvec4 o;
void main() {
  ivec2 p = ivec2(gl_FragCoord.xy);
  ivec2 sz = textureSize(uTex, 0);
  uvec2 best = uvec2(65535u);
  float bd = 1e20;
  for (int dy = -1; dy <= 1; dy++) {
    for (int dx = -1; dx <= 1; dx++) {
      ivec2 q = p + ivec2(dx, dy) * uStep;
      if (q.x < 0 || q.y < 0 || q.x >= sz.x || q.y >= sz.y) continue;
      uvec2 s = texelFetch(uTex, q, 0).rg;
      if (s.x == 65535u) continue;
      vec2 d = vec2(s) - vec2(p);
      float dd = dot(d, d);
      if (dd < bd) { bd = dd; best = s; }
    }
  }
  o = uvec4(best, 0u, 1u);
}`;

// 4c. 符号付き距離（内側が正）。輪郭は内側画素と外側画素のちょうど間（±0.5）
export const FS_SDF = HEAD + `
uniform usampler2D uSeed;
uniform sampler2D uMask;
uniform vec2 uSize;
out vec4 o;
void main() {
  uvec2 s = texelFetch(uSeed, ivec2(gl_FragCoord.xy), 0).rg;
  float d = s.x == 65535u ? 30000.0 : distance(vec2(s) + 0.5, gl_FragCoord.xy);
  bool inside = texture(uMask, gl_FragCoord.xy / uSize).r >= 0.5;
  o = vec4(inside ? d + 0.5 : 0.5 - d, 0.0, 0.0, 1.0);
}`;

// 4d. 部分マップ: 部分マスク（255 = その部分）を、部分の番号（1〜）として1枚の R8 に重ねて書く。
// マスクの大きさが画像と違うときは、滑らかに拡大してしきい値 0.5 で切る
export const FS_PARTMAP = HEAD + `
uniform sampler2D uMask;
uniform vec2 uSize;
uniform float uCode;
out vec4 o;
void main() {
  if (texture(uMask, gl_FragCoord.xy / uSize).r < 0.5) discard;
  o = vec4(uCode / 255.0, 0.0, 0.0, 1.0);
}`;

export const MAX_SETS = 8;           // 領域の数の上限（人物・背景・部分 6）
export const LAYER_CAPACITIES = [16, 32, 48, 64];
// 合成パスが使う uniform の本数の見積もり（層 1 つにつき vec4 が 3 本、それ以外をまとめて 44 本と見る）
export const uniformVectors = (capacity) => 3 * capacity + 44;

// 5. 合成: 白地 → トーン層 → ベタ → 線画（乗算）→ 背景処理 → 消しゴム「すべて」→ ペン（→ 純2値）→ 透かし
// 消しゴム: 対象（各層 / ベタ / 線画 / すべて）ごとの消去マスク（0 = そのまま、1 = 消す）を、そのインク量に掛ける。
// マスクは RGBA テクスチャの各チャンネルに1対象ずつ入っていて、スロット = テクスチャ番号 × 4 + チャンネル（無ければ -1）。
// トーン（明るさ補正・ベタ・層）は領域ごとに別々の設定を持てる（領域 0 = 人物、1 = 背景、2〜 = 別の設定にした部分）。
// 層は、使う物だけを 1 本の配列に詰めてあり、領域ごとに「先頭の番号と数」で引く（配列の長さ NL は使う層の総数で選ぶ）。
// 1 画素が計算するトーンは、人物側（人物か、その画素の部分）と背景側の高々 2 領域ぶんで、領域の数には比例しない。
// uMode: 0 = 出力、1 / 2 = 統計用（縮小した格子で画素を抜き出し、数値を RGBA に詰める）
export const composite = (capacity) => HEAD + `
#define NL ${capacity}
#define NS ${MAX_SETS}
uniform sampler2D uLum;      // 平滑化した輝度（トーン分け用）
uniform sampler2D uBlur;     // RG = G(σ), G(1.6σ)
uniform sampler2D uSdf;
uniform sampler2D uWm;       // R = 文字, G = 文字の縁取り
uniform sampler2D uPartMap;  // 部分の番号（0 = どの部分でもない）
uniform int uPartOn;
uniform ivec4 uPartSet[2];   // 部分の番号 → 領域の番号（0 = 人物と同じ設定で描く）
uniform ivec2 uRange[NS];    // 領域ごとの層の範囲（先頭の番号, 数）
uniform vec4 uA[NL];         // しきい値, ぼかし幅, sin(角度), cos(角度)
uniform vec4 uB[NL];         // 間隔px, 太さpx, 暗さで太さ, 濃さ
uniform vec4 uC[NL];         // 位相, 消去マスクのスロット, -, -
uniform vec4 uLevels[NS];    // 明るさ, コントラスト, 1/ガンマ, ベタの消去マスクのスロット
uniform vec4 uSolid[NS];     // 有効, しきい値, ぼかし幅, ベタの中の線画の白抜き
uniform vec4 uLine;          // 濃さ, τ, ε, φ
uniform int uLineOn;         // 線画の計算が必要か（ぼかしテクスチャが有効か）
uniform int uHasMask;
uniform vec4 uBg;            // 背景の描き方(0 層で描く /1 白 /2 線だけ), 広げ量px, シルエット線px, 背景の線の濃さ
uniform vec4 uWmRect;        // 透かしの矩形 x, y, w, h（px）
uniform vec4 uWmP;           // モード(0 なし /1 1か所 /2 タイル), 濃さ, タイル周期 x, y
uniform int uBinary;
uniform int uMode;
uniform vec2 uSampleScale;   // 統計用: 格子1マスが何画素か
uniform ivec2 uSize;
uniform sampler2D uEr0;      // 消去マスク（使っている枚数は uErN）
uniform sampler2D uEr1;
uniform sampler2D uEr2;
uniform sampler2D uEr3;
uniform sampler2D uEr4;
uniform sampler2D uEr5;
uniform sampler2D uEr6;
uniform sampler2D uEr7;
uniform int uErN;
uniform ivec4 uErMisc;       // -, -, 線画, すべて のスロット
uniform sampler2D uPen;      // ペン: R = 黒の量, G = 白（修正液）の量
uniform int uPenOn;
out vec4 o;

vec4 gErase[8];
// この画素の消去マスクをまとめて読む（マスクは画像より小さいことがあるので、滑らかに拡大して読む）
void loadErase(vec2 uv) {
  if (uErN > 0) gErase[0] = textureLod(uEr0, uv, 0.0);
  if (uErN > 1) gErase[1] = textureLod(uEr1, uv, 0.0);
  if (uErN > 2) gErase[2] = textureLod(uEr2, uv, 0.0);
  if (uErN > 3) gErase[3] = textureLod(uEr3, uv, 0.0);
  if (uErN > 4) gErase[4] = textureLod(uEr4, uv, 0.0);
  if (uErN > 5) gErase[5] = textureLod(uEr5, uv, 0.0);
  if (uErN > 6) gErase[6] = textureLod(uEr6, uv, 0.0);
  if (uErN > 7) gErase[7] = textureLod(uEr7, uv, 0.0);
}
// そのスロットの「残す量」（1 = そのまま、0 = 全部消す）
float keep(int slot) {
  if (slot < 0) return 1.0;
  return 1.0 - gErase[slot >> 2][slot & 3];
}

// 輝度 L がしきい値 th 未満なら 1。soft > 0 なら境界をなだらかにする
float below(float L, float th, float soft) {
  if (soft > 1e-5) return 1.0 - smoothstep(th - 0.5 * soft, th + 0.5 * soft, L);
  return L < th ? 1.0 : 0.0;
}

const int BAYER[16] = int[16](0, 8, 2, 10, 12, 4, 14, 6, 3, 11, 1, 9, 15, 7, 13, 5);
// 4x4 のベイヤー行列（0..1）。純2値のときに透かしの濃さを点の密度で表す
float bayer(vec2 p) {
  ivec2 q = ivec2(p) & 3;
  return (float(BAYER[q.y * 4 + q.x]) + 0.5) / 16.0;
}

vec2 hash2(vec2 c) {
  vec3 q = fract(vec3(c.xyx) * vec3(0.1031, 0.1030, 0.0973));
  q += dot(q, q.yzx + 33.33);
  return fract((q.xx + q.yz) * q.zy);
}

// 領域ごとの明るさ補正（トーン分けに使う輝度だけに掛かる。線画には影響しない）
float adjust(float L, vec4 lv) {
  return pow(clamp((L - 0.5) * lv.y + 0.5 + lv.x, 0.0, 1.0), lv.z);
}

struct Tone {
  float v;       // 残る白（1 = 白）
  float bits;    // 描かれた層のビット集合（統計用）
  float solid;   // ベタの量
  float L;       // 補正後の輝度
};

// 領域 r（0 人物 / 1 背景 / 2〜 部分）のトーン層とベタ。重ねがけ: 残る白 = Π(1 − ink_i)
Tone tone(float L0, int r, vec2 p) {
  vec4 lv = uLevels[r];
  float L = adjust(L0, lv);
  ivec2 range = uRange[r];
  float white = 1.0;
  float bits = 0.0;
  for (int i = 0; i < 8; i++) {
    if (i >= range.y) break;
    int j = range.x + i;
    vec4 A = uA[j];
    vec4 B = uB[j];
    vec4 C = uC[j];
    float m = below(L, A.x, A.y);
    if (m <= 0.0) continue;
    float k = keep(int(C.y));
    if (m > 0.5 && k > 0.5) bits += exp2(float(i));
    // しきい値からどれだけ暗いか（0 = しきい値ちょうど、1 = 真っ黒）
    float t = clamp((A.x - L) / max(A.x, 1e-4), 0.0, 1.0);
    // 境界のぼかしは線を細らせて表す（灰色にはしない）
    float w = min(B.y * (1.0 + B.z * (2.0 * t - 1.0)) * m, B.x);
    float u = dot(p, A.zw) / B.x + C.x;
    u -= floor(u + 0.5);
    float dist = abs(u) * B.x;                 // 最寄りの線中心までの距離
    float h = 0.5 * w;
    // 画素（幅1）と線（幅 w）の重なり長さ。1px 未満の線でも平均インク量 = 太さ/間隔
    float ink = clamp(min(dist + 0.5, h) - max(dist - 0.5, -h), 0.0, 1.0);
    white *= 1.0 - ink * B.w * k;
  }
  vec4 so = uSolid[r];
  // しきい値 1 は「全面ベタ」（輝度がちょうど 1 の画素も塗る）
  float s = so.x > 0.5 ? (so.y >= 1.0 ? 1.0 : below(L, so.y, so.z)) * keep(int(lv.w)) : 0.0;
  return Tone(white * (1.0 - s), bits, s, L);
}

void main() {
  ivec2 ip = ivec2(gl_FragCoord.xy);
  if (uMode != 0) {
    // 格子の各マスの中から決まった位置の1画素を抜き出す（ハッチの周期と干渉しないよう位置をずらす）
    vec2 cell = floor(gl_FragCoord.xy);
    ip = clamp(ivec2((cell + hash2(cell)) * uSampleScale), ivec2(0), uSize - 1);
  }
  vec2 p = vec2(ip) + 0.5;
  float L0 = texelFetch(uLum, ip, 0).r;
  vec2 uv = p / vec2(uSize);
  loadErase(uv);

  // --- 人物の重み（マスクが無ければ全体が人物） ---
  float sd = 30000.0;
  float pw = 1.0;
  if (uHasMask == 1) {
    sd = texelFetch(uSdf, ip, 0).r + uBg.y;
    pw = clamp(sd + 0.5, 0.0, 1.0);
  }
  int mode = int(uBg.x + 0.5);

  // --- 部分（髪・肌・服など）: その画素の部分の番号から、人物側の領域を決める ---
  // 別の設定にした部分（領域 2〜）の画素は、人物マスクの広げ/縮めに関わらずその部分として描く（部分の境界は生のマスクのまま）
  int part = 0;
  if (uPartOn == 1) part = int(texelFetch(uPartMap, ip, 0).r * 255.0 + 0.5);
  int rp = part > 0 ? uPartSet[part >> 2][part & 3] : 0;
  float pwE = rp >= 2 ? 1.0 : pw;

  // --- 線画 (XDoG) ---
  float line = 0.0;
  if (uLineOn == 1) {
    vec2 g = texelFetch(uBlur, ip, 0).rg;
    float D = g.r - uLine.y * g.g;
    line = clamp(-tanh(uLine.w * (D - uLine.z)), 0.0, 1.0);
  }
  float keepLine = keep(uErMisc.z);
  float lineInk = uLine.x * line * keepLine;

  // --- 人物 / 背景それぞれのトーンを作って、人物の重みで混ぜる ---
  Tone tp = Tone(1.0, 0.0, 0.0, L0);
  Tone tb = Tone(1.0, 0.0, 0.0, adjust(L0, uLevels[1]));
  if (pwE > 0.0) tp = tone(L0, rp, p);
  float bgLine = lineInk;
  if (mode == 0) {
    if (pwE < 1.0) tb = tone(L0, 1, p);
  } else if (mode == 2) {
    bgLine = uBg.w * line * keepLine;
  } else {
    bgLine = 0.0;
  }
  // ベタの中の線画の白抜き: ベタで塗られた所の線画を、白で描く（強さ 0 なら何も足さない）
  float knock = line * keepLine;
  float v = mix(tb.v * (1.0 - bgLine) + tb.solid * knock * uSolid[1].w,
                tp.v * (1.0 - lineInk) + tp.solid * knock * uSolid[rp].w, pwE);
  if (mode == 1 && uBg.z > 0.0) v *= 1.0 - clamp(uBg.z - sd + 0.5, 0.0, 1.0) * pw * keepLine;   // シルエット線

  // --- 消しゴム「すべて」: その場所を白にする（トーン・ベタ・線画をまとめて消す） ---
  if (uErMisc.w >= 0) v = 1.0 - (1.0 - v) * keep(uErMisc.w);

  // --- ペン: 黒インクと白（修正液）。合成の最後（透かしの直前）に重ねる ---
  if (uPenOn == 1) {
    vec2 pen = textureLod(uPen, uv, 0.0).rg;
    float white = min(pen.g, 1.0 - pen.r);
    v = v * (1.0 - pen.r - white) + white;
  }

  if (uBinary == 1) v = v >= 0.5 ? 1.0 : 0.0;

  // --- 透かし ---
  if (uWmP.x > 0.5) {
    vec2 uv;
    if (uWmP.x < 1.5) {
      uv = (p - uWmRect.xy) / uWmRect.zw;
    } else {
      vec2 q = vec2(0.9396926 * p.x - 0.3420201 * p.y, 0.3420201 * p.x + 0.9396926 * p.y);
      float row = floor(q.y / uWmP.w);
      q.x += mod(row, 2.0) * 0.5 * uWmP.z;
      uv = mod(q, uWmP.zw) / uWmRect.zw;
    }
    if (uv.x >= 0.0 && uv.y >= 0.0 && uv.x <= 1.0 && uv.y <= 1.0) {
      vec2 wm = textureLod(uWm, uv, 0.0).rg * uWmP.y;
      if (uBinary == 1) {
        float th = bayer(p);
        wm = vec2(wm.r > th ? 1.0 : 0.0, wm.g > th ? 1.0 : 0.0);
      }
      v = mix(v, 1.0, wm.g);
      v = mix(v, 0.0, wm.r);
    }
  }

  if (uMode == 0) {
    o = vec4(v, 0.0, 0.0, 1.0);
  } else if (uMode == 1) {
    // 出力値, 人物側の重み, 線画の濃さ, ベタの量（画素が属する領域のもの）
    bool person = pwE >= 0.5;
    o = vec4(v, pwE, person ? lineInk : bgLine, person ? tp.solid : tb.solid);
  } else {
    // 元の輝度, 補正後の輝度, 描かれた層のビット集合（画素が属する領域のもの）, 部分の番号
    bool person = pwE >= 0.5;
    o = vec4(L0, person ? tp.L : tb.L, (person ? tp.bits : tb.bits) / 255.0, float(part) / 255.0);
  }
}`;

export const FS_COMPOSITE = composite(LAYER_CAPACITIES[0]);

// 6. 表示: 出力テクスチャ（ミップマップ付き）を拡大縮小・移動して描く
export const FS_DISPLAY = HEAD + `
uniform sampler2D uOut;
uniform sampler2D uSrc;
uniform vec2 uCanvas;        // キャンバスの大きさ（デバイスpx）
uniform vec2 uCenter;        // キャンバス中央に来る画像座標
uniform float uZoom;         // 画像1px が何デバイスpx か
uniform vec2 uImg;
uniform float uSplit;        // 比較の仕切りの x（デバイスpx）。負なら無し
uniform int uOriginal;       // 1: 元画像を表示
uniform sampler2D uSdf;
uniform sampler2D uPartMap;
uniform ivec4 uHi;           // 領域の強調表示: 種類（0 なし /1 人物 /2 背景 /3 部分）, 部分の番号, マスクあり, 部分マップあり
uniform float uHiGrow;       // 人物マスクの広げ量（px）
uniform int uHiOwn;          // 別の設定にした部分のビット集合（その画素は、人物・背景の強調には入れない）
out vec4 o;
void main() {
  vec2 fc = vec2(gl_FragCoord.x, uCanvas.y - gl_FragCoord.y);
  vec2 uv = ((fc - 0.5 * uCanvas) / uZoom + uCenter) / uImg;
  vec3 src = texture(uSrc, uv).rgb;
  float outv = texture(uOut, uv).r;
  vec3 col = (uOriginal == 1 || fc.x < uSplit) ? src : vec3(outv);
  if (uHi.x != 0) {
    ivec2 ip = clamp(ivec2(floor(uv * uImg)), ivec2(0), ivec2(uImg) - 1);
    int part = uHi.w == 1 ? int(texelFetch(uPartMap, ip, 0).r * 255.0 + 0.5) : 0;
    bool own = part > 0 && ((uHiOwn >> part) & 1) == 1;
    bool inside = uHi.z == 1 ? texelFetch(uSdf, ip, 0).r + uHiGrow >= 0.0 : true;
    bool hit = uHi.x == 3 ? part == uHi.y : (!own && (uHi.x == 1) == inside);
    if (hit) col = mix(col, vec3(1.0, 0.25, 0.55), 0.55);
  }
  if (uv.x < 0.0 || uv.y < 0.0 || uv.x > 1.0 || uv.y > 1.0) col = vec3(0.16, 0.16, 0.18);
  if (uSplit >= 0.0 && abs(fc.x - uSplit) < 1.0) col = vec3(0.95, 0.45, 0.1);
  o = vec4(col, 1.0);
}`;
