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

// 5. 合成: 白地 → トーン層 → ベタ → 線画（乗算）→ 背景処理 → 透かし（→ 純2値）
export const FS_COMPOSITE = HEAD + `
uniform sampler2D uLum;      // 平滑化した輝度（トーン分け用）
uniform sampler2D uBlur;     // RG = G(σ), G(1.6σ)
uniform sampler2D uSdf;
uniform sampler2D uWm;       // R = 文字, G = 文字の縁取り
uniform int uCount;
uniform vec4 uA[8];          // しきい値, ぼかし幅, sin(角度), cos(角度)
uniform vec4 uB[8];          // 間隔px, 太さpx, 暗さで太さ, 濃さ
uniform vec4 uC[8];          // 位相, -, -, -
uniform vec4 uSolid;         // 有効, しきい値, ぼかし幅
uniform vec4 uLine;          // 濃さ, τ, ε, φ
uniform int uLineOn;         // 線画の計算が必要か（ぼかしテクスチャが有効か）
uniform vec4 uBg;            // モード(0 そのまま /1 白 /2 線だけ), 広げ量px, シルエット線px, 背景の線の濃さ
uniform vec4 uWmRect;        // 透かしの矩形 x, y, w, h（px）
uniform vec4 uWmP;           // モード(0 なし /1 1か所 /2 タイル), 濃さ, タイル周期 x, y
uniform int uBinary;
out vec4 o;

// 輝度 L がしきい値 th 未満なら 1。soft > 0 なら境界をなだらかにする
float below(float L, float th, float soft) {
  if (soft > 1e-5) return 1.0 - smoothstep(th - 0.5 * soft, th + 0.5 * soft, L);
  return L < th ? 1.0 : 0.0;
}

// 4x4 のベイヤー行列（0..1）。純2値のときに透かしの濃さを点の密度で表す
const int BAYER[16] = int[16](0, 8, 2, 10, 12, 4, 14, 6, 3, 11, 1, 9, 15, 7, 13, 5);
float bayer(vec2 p) {
  ivec2 q = ivec2(p) & 3;
  return (float(BAYER[q.y * 4 + q.x]) + 0.5) / 16.0;
}

void main() {
  ivec2 ip = ivec2(gl_FragCoord.xy);
  vec2 p = gl_FragCoord.xy;
  float L = texelFetch(uLum, ip, 0).r;

  // --- トーン層（重ねがけ）: 残る白 = Π(1 − ink_i) ---
  float white = 1.0;
  for (int i = 0; i < 8; i++) {
    if (i >= uCount) break;
    vec4 A = uA[i];
    vec4 B = uB[i];
    float m = below(L, A.x, A.y);
    if (m <= 0.0) continue;
    // しきい値からどれだけ暗いか（0 = しきい値ちょうど、1 = 真っ黒）
    float t = clamp((A.x - L) / max(A.x, 1e-4), 0.0, 1.0);
    // 境界のぼかしは線を細らせて表す（灰色にはしない）
    float w = min(B.y * (1.0 + B.z * (2.0 * t - 1.0)) * m, B.x);
    float u = dot(p, A.zw) / B.x + uC[i].x;
    u -= floor(u + 0.5);
    float dist = abs(u) * B.x;                 // 最寄りの線中心までの距離
    float h = 0.5 * w;
    // 画素（幅1）と線（幅 w）の重なり長さ。1px 未満の線でも平均インク量 = 太さ/間隔
    float ink = clamp(min(dist + 0.5, h) - max(dist - 0.5, -h), 0.0, 1.0);
    white *= 1.0 - ink * B.w;
  }
  float v = white;

  // --- ベタ ---
  if (uSolid.x > 0.5) v *= 1.0 - below(L, uSolid.y, uSolid.z);

  // --- 線画 (XDoG) ---
  float line = 0.0;
  if (uLineOn == 1) {
    vec2 g = texelFetch(uBlur, ip, 0).rg;
    float D = g.r - uLine.y * g.g;
    line = clamp(-tanh(uLine.w * (D - uLine.z)), 0.0, 1.0);
  }
  v *= 1.0 - uLine.x * line;

  // --- 背景処理 ---
  if (uBg.x > 0.5) {
    float sd = texelFetch(uSdf, ip, 0).r + uBg.y;
    float person = clamp(sd + 0.5, 0.0, 1.0);
    if (uBg.x < 1.5) {
      v = mix(1.0, v, person);
      if (uBg.z > 0.0) v *= 1.0 - clamp(uBg.z - sd + 0.5, 0.0, 1.0) * person;
    } else {
      v = mix(1.0 - uBg.w * line, v, person);
    }
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

  o = vec4(v, 0.0, 0.0, 1.0);
}`;

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
out vec4 o;
void main() {
  vec2 fc = vec2(gl_FragCoord.x, uCanvas.y - gl_FragCoord.y);
  vec2 uv = ((fc - 0.5 * uCanvas) / uZoom + uCenter) / uImg;
  vec3 src = texture(uSrc, uv).rgb;
  float outv = texture(uOut, uv).r;
  vec3 col = (uOriginal == 1 || fc.x < uSplit) ? src : vec3(outv);
  if (uv.x < 0.0 || uv.y < 0.0 || uv.x > 1.0 || uv.y > 1.0) col = vec3(0.16, 0.16, 0.18);
  if (uSplit >= 0.0 && abs(fc.x - uSplit) < 1.0) col = vec3(0.95, 0.45, 0.1);
  o = vec4(col, 1.0);
}`;
