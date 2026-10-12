// 設定JSONの真実の源: キー・既定値・値域・UI ラベルはすべてここで定義する。
// px: true の値は「基準px」（実際の描画では unit 倍される）。

export const VERSION = 1;
export const MAX_LAYERS = 8;
export const UNIT_BASE_WIDTH = 1280;      // unit の自動値 = 画像の幅 / これ
export const UNIT_RANGE = { min: 0.1, max: 16 };
export const WATERMARK_FALLBACK_TEXT = 'inkdeus';

const range = (def, min, max, step, label, extra = {}) =>
  ({ type: 'range', def, min, max, step, label, ...extra });
const bool = (def, label, extra = {}) => ({ type: 'bool', def, label, ...extra });
const select = (def, options, label) => ({ type: 'select', def, options, label });
const text = (def, label, maxLength = 64) => ({ type: 'text', def, label, maxLength });

export const FIELDS = {
  input: {
    autoLevel: bool(true, '自動レベル補正（1〜99%）'),
    smoothRadius: range(2.0, 0, 12, 0.1, '平滑化の半径', { px: true }),
    smoothStrength: range(0.10, 0, 0.5, 0.005, '平滑化の強さ'),
  },
  lines: {
    enabled: bool(true, '線画を描く'),
    sigma: range(0.8, 0.2, 4, 0.05, '線の太さ σ', { px: true }),
    tau: range(0.98, 0.8, 1.0, 0.001, 'τ（大きいほど線が増える）'),
    epsilon: range(-0.01, -0.1, 0.05, 0.001, 'しきい値 ε'),
    phi: range(100, 1, 400, 1, '鋭さ φ'),
    strength: range(1.0, 0, 1, 0.01, '濃さ'),
  },
  solid: {
    enabled: bool(true, 'ベタを塗る'),
    threshold: range(0.15, 0, 1, 0.005, 'しきい値'),
    soft: range(0.0, 0, 0.3, 0.005, '境界のぼかし幅'),
  },
  background: {
    mode: select('keep', [['keep', 'そのまま'], ['white', '白'], ['lines', '線だけ']], '背景'),
    grow: range(3.0, -20, 40, 0.5, 'マスクの広げ/縮め', { px: true }),
    silhouetteWidth: range(1.5, 0, 10, 0.1, 'シルエット線の太さ', { px: true }),
    lineStrength: range(0.6, 0, 1, 0.01, '背景の線の濃さ'),
  },
  watermark: {
    // enabled は所有者モードのときだけ意味を持つ（それ以外では常に透かしを入れる）。
    // 大きさと濃さの下限は「見えなくできない」ための下限。
    enabled: bool(true, '透かしを入れる', { ownerDef: false }),
    text: text(WATERMARK_FALLBACK_TEXT, '文字列'),
    position: select('br', [['br', '右下'], ['bl', '左下'], ['tr', '右上'], ['tl', '左上'],
      ['center', '中央'], ['tile', 'タイル状の繰り返し']], '位置'),
    size: range(14, 8, 120, 1, '大きさ', { px: true }),
    opacity: range(0.35, 0.15, 1, 0.01, '濃さ'),
  },
  output: {
    binary: bool(false, '純2値（白と黒だけ）'),
  },
};

export const LAYER_FIELDS = {
  type: select('hatch', [['hatch', 'ハッチ']], '種類'),
  enabled: bool(true, '表示'),
  threshold: range(0.5, 0, 1, 0.005, 'しきい値'),
  soft: range(0.03, 0, 0.3, 0.005, '境界のぼかし幅'),
  angle: range(45, 0, 180, 1, '角度 (°)'),
  spacing: range(2.0, 0.5, 20, 0.1, '線の間隔', { px: true }),
  width: range(0.5, 0.05, 10, 0.05, '線の太さ', { px: true }),
  widthByDarkness: range(0.0, 0, 1, 0.01, '暗さで太さを変える'),
  opacity: range(1.0, 0, 1, 0.01, '濃さ'),
  phase: range(0.0, 0, 1, 0.01, '位相ずらし'),
};

export const SECTION_LABELS = {
  input: '入力', lines: '線画', solid: 'ベタ', layers: 'トーン層',
  background: '背景', watermark: '透かし', output: '出力',
};

const layer = (o) => ({ type: 'hatch', ...o });

// プリセットは「トーン層とベタ」だけを差し替える（線画・背景・透かしはそのまま）
export const PRESETS = {
  kasane4: {
    label: '重ねがけ4段',
    solid: { enabled: true, threshold: 0.15, soft: 0 },
    layers: [
      layer({ threshold: 0.72, angle: 45, spacing: 2.0, width: 0.5, widthByDarkness: 0.6 }),
      layer({ threshold: 0.52, angle: 135, spacing: 2.0, width: 0.5 }),
      layer({ threshold: 0.38, angle: 0, spacing: 2.0, width: 0.5 }),
      layer({ threshold: 0.27, angle: 90, spacing: 2.0, width: 0.5 }),
    ],
  },
  single: {
    label: '単層の細ハッチ',
    solid: { enabled: true, threshold: 0.22, soft: 0 },
    layers: [
      layer({ threshold: 0.72, angle: 45, spacing: 2.0, width: 0.5, widthByDarkness: 0.8 }),
    ],
  },
  lineonly: {
    label: '網点なし・線画のみ',
    solid: { enabled: false, threshold: 0.15, soft: 0 },
    layers: [],
  },
};
export const DEFAULT_PRESET = 'kasane4';

const isObject = (v) => typeof v === 'object' && v !== null && !Array.isArray(v);

function fieldDefault(f, owner) {
  return owner && 'ownerDef' in f ? f.ownerDef : f.def;
}

function normValue(v, f, path, warnings, owner) {
  const def = fieldDefault(f, owner);
  if (v === undefined) return def;
  switch (f.type) {
    case 'bool':
      if (typeof v === 'boolean') return v;
      break;
    case 'range':
      if (typeof v === 'number' && Number.isFinite(v)) {
        const c = Math.min(f.max, Math.max(f.min, v));
        if (c !== v) warnings.push(`${path}: ${v} は範囲外のため ${c} に丸めました`);
        return c;
      }
      break;
    case 'select':
      if (f.options.some(([key]) => key === v)) return v;
      break;
    case 'text':
      if (typeof v === 'string') return v.slice(0, f.maxLength);
      break;
  }
  warnings.push(`${path}: 値 ${JSON.stringify(v)} は使えないため既定値にしました`);
  return def;
}

function normSection(raw, fields, path, warnings, owner) {
  if (raw !== undefined && !isObject(raw)) {
    warnings.push(`${path}: オブジェクトではないため既定値にしました`);
    raw = undefined;
  }
  const src = raw || {};
  for (const key of Object.keys(src)) {
    if (!(key in fields)) warnings.push(`未知のキー: ${path}.${key}`);
  }
  const out = {};
  for (const [key, f] of Object.entries(fields)) {
    out[key] = normValue(src[key], f, `${path}.${key}`, warnings, owner);
  }
  return out;
}

/**
 * 任意の JSON を設定オブジェクトに整える。欠けたキーは既定値で補い、
 * 未知のキー・使えない値は warnings に積む（黙って無視しない）。
 */
export function normalize(raw, { owner = false } = {}) {
  const warnings = [];
  if (!isObject(raw)) {
    if (raw !== undefined && raw !== null) warnings.push('設定がオブジェクトではないため既定値にしました');
    raw = {};
  }
  const known = new Set(['version', 'unit', 'layers', ...Object.keys(FIELDS)]);
  for (const key of Object.keys(raw)) {
    if (!known.has(key)) warnings.push(`未知のキー: ${key}`);
  }
  if (raw.version !== undefined && raw.version !== VERSION) {
    warnings.push(`version ${JSON.stringify(raw.version)} は未対応です（${VERSION} として読みました）`);
  }
  const params = { version: VERSION, unit: null };
  if (raw.unit !== undefined && raw.unit !== null) {
    if (typeof raw.unit === 'number' && Number.isFinite(raw.unit) && raw.unit > 0) {
      params.unit = Math.min(UNIT_RANGE.max, Math.max(UNIT_RANGE.min, raw.unit));
    } else {
      warnings.push(`unit: 値 ${JSON.stringify(raw.unit)} は使えないため自動にしました`);
    }
  }
  const preset = PRESETS[DEFAULT_PRESET];
  for (const [section, fields] of Object.entries(FIELDS)) {
    const src = raw[section] === undefined && section === 'solid' ? preset.solid : raw[section];
    params[section] = normSection(src, fields, section, warnings, owner);
    if (section === 'solid') {
      let layers = raw.layers === undefined ? preset.layers : raw.layers;
      if (!Array.isArray(layers)) {
        warnings.push('layers: 配列ではないため既定値にしました');
        layers = preset.layers;
      }
      if (layers.length > MAX_LAYERS) {
        warnings.push(`layers: ${layers.length} 層あります。先頭の ${MAX_LAYERS} 層だけ使います`);
        layers = layers.slice(0, MAX_LAYERS);
      }
      params.layers = layers.map((l, i) => normSection(l, LAYER_FIELDS, `layers[${i}]`, warnings, owner));
    }
  }
  return { params, warnings };
}

export function defaultParams(opts) {
  return normalize({}, opts).params;
}

export function newLayer(overrides = {}) {
  return normSection(overrides, LAYER_FIELDS, 'layer', [], false);
}

/** プリセットを適用した新しい設定を返す（トーン層とベタだけ差し替え） */
export function applyPreset(params, key, opts) {
  const p = PRESETS[key];
  if (!p) return params;
  return normalize({ ...params, solid: p.solid, layers: p.layers }, opts).params;
}

/** 実際に使う unit（null なら画像の幅から自動） */
export function effectiveUnit(params, imageWidth) {
  if (typeof params.unit === 'number') return params.unit;
  return Math.max(UNIT_RANGE.min, (imageWidth || UNIT_BASE_WIDTH) / UNIT_BASE_WIDTH);
}

/**
 * 実際に描く透かしの設定。所有者モードでなければ enabled の値に関係なく必ず入れる。
 * 文字列は空にできず、大きさ・濃さは下限未満にならない。
 */
export function effectiveWatermark(params, owner) {
  const f = FIELDS.watermark;
  const w = params.watermark;
  const txt = (w.text || '').trim();
  return {
    enabled: owner ? w.enabled : true,
    text: txt || WATERMARK_FALLBACK_TEXT,
    position: w.position,
    size: Math.max(f.size.min, w.size),
    opacity: Math.max(f.opacity.min, w.opacity),
  };
}

/** README 用: キー一覧を「セクション.キー: 既定値」の配列で返す */
export function describeKeys() {
  const lines = [];
  for (const [section, fields] of Object.entries(FIELDS)) {
    for (const [key, f] of Object.entries(fields)) lines.push(`${section}.${key}: ${JSON.stringify(f.def)}`);
  }
  for (const [key, f] of Object.entries(LAYER_FIELDS)) lines.push(`layers[].${key}: ${JSON.stringify(f.def)}`);
  return lines;
}
