// 設定JSONの扱い（既定値で補う・検証・旧形式の移行・スタイルの適用）。
// キー・既定値・値域・ラベル・名前付きスタイルの定義は web/schema.json が真実の源で、
// サーバ側（src/inkdeus/schema.py）も同じファイルを読む。

export const SCHEMA = await (await fetch(new URL('../schema.json', import.meta.url), { cache: 'no-store' })).json();

export const VERSION = SCHEMA.version;
export const MAX_LAYERS = SCHEMA.maxLayers;
export const UNIT_RANGE = SCHEMA.unitRange;
export const WATERMARK_FALLBACK_TEXT = SCHEMA.watermarkFallbackText;
export const SECTIONS = SCHEMA.sections;          // input / lines / watermark / output
export const LEVEL_FIELDS = SCHEMA.levels;
export const SOLID_FIELDS = SCHEMA.solid;
export const BACKGROUND_FIELDS = SCHEMA.background;
export const LAYER_FIELDS = SCHEMA.layer;
export const STYLES = SCHEMA.styles;
export const REGIONS = ['person', 'background'];

const isObject = (v) => typeof v === 'object' && v !== null && !Array.isArray(v);
const clone = (v) => JSON.parse(JSON.stringify(v));

export function fieldDefault(f, owner) {
  return owner && 'ownerDef' in f ? f.ownerDef : f.def;
}

function normValue(v, f, path, problems, owner) {
  const def = fieldDefault(f, owner);
  if (v === undefined) return def;
  switch (f.type) {
    case 'bool':
      if (typeof v === 'boolean') return v;
      break;
    case 'range':
      if (typeof v === 'number' && Number.isFinite(v)) {
        const c = Math.min(f.max, Math.max(f.min, v));
        if (c !== v) problems.push(`${path}: ${v} は範囲外（${f.min}〜${f.max}）のため ${c} に丸めました`);
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
  problems.push(`${path}: 値 ${JSON.stringify(v)} は使えないため既定値にしました`);
  return def;
}

/** raw（オブジェクト）を fields に従って整える。extra は fields 以外に許すキー */
function normFields(raw, fields, path, problems, owner, extra = []) {
  if (raw !== undefined && !isObject(raw)) {
    problems.push(`${path}: オブジェクトではないため既定値にしました`);
    raw = undefined;
  }
  const src = raw || {};
  for (const key of Object.keys(src)) {
    if (!(key in fields) && !extra.includes(key)) problems.push(`未知のキー: ${path}.${key}`);
  }
  const out = {};
  for (const [key, f] of Object.entries(fields)) {
    out[key] = normValue(src[key], f, `${path}.${key}`, problems, owner);
  }
  return out;
}

function normLayers(raw, path, problems, fallback) {
  let layers = raw === undefined ? fallback : raw;
  if (!Array.isArray(layers)) {
    problems.push(`${path}: 配列ではないため既定値にしました`);
    layers = fallback;
  }
  if (layers.length > MAX_LAYERS) {
    problems.push(`${path}: ${layers.length} 層あります。先頭の ${MAX_LAYERS} 層だけ使います`);
    layers = layers.slice(0, MAX_LAYERS);
  }
  return layers.map((l, i) => normFields(l, LAYER_FIELDS, `${path}[${i}]`, problems, false));
}

/** 領域（人物 / 背景）に共通の levels・solid・layers を out に足す */
function normRegionTone(src, out, path, problems) {
  const style = STYLES[SCHEMA.defaultStyle];
  out.levels = normFields(src.levels, LEVEL_FIELDS, `${path}.levels`, problems, false);
  out.solid = normFields(src.solid === undefined ? style.solid : src.solid, SOLID_FIELDS, `${path}.solid`, problems, false);
  out.layers = normLayers(src.layers, `${path}.layers`, problems, style.layers);
}

/** 旧形式（version 1: トップレベルに solid / layers / background）を領域別の形へ移す */
export function migrate(raw) {
  if (!isObject(raw)) return raw;
  const old = 'solid' in raw || 'layers' in raw || 'background' in raw || raw.version === 1;
  if (!old || 'regions' in raw) return raw;
  const { solid, layers, background, ...rest } = raw;
  const person = {};
  if (solid !== undefined) person.solid = solid;
  if (layers !== undefined) person.layers = layers;
  return { ...rest, version: VERSION, regions: { person, background: background === undefined ? {} : background } };
}

/**
 * 任意の JSON を設定オブジェクトに整える。欠けたキーは既定値で補い、
 * 未知のキー・使えない値は warnings に積む（黙って無視しない）。
 */
export function normalize(input, { owner = false } = {}) {
  const warnings = [];
  let raw = input;
  if (!isObject(raw)) {
    if (raw !== undefined && raw !== null) warnings.push('設定がオブジェクトではないため既定値にしました');
    raw = {};
  }
  raw = migrate(raw);
  const known = new Set(['version', 'unit', 'regions', ...Object.keys(SECTIONS)]);
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
      if (params.unit !== raw.unit) warnings.push(`unit: ${raw.unit} は範囲外のため ${params.unit} に丸めました`);
    } else {
      warnings.push(`unit: 値 ${JSON.stringify(raw.unit)} は使えないため自動にしました`);
    }
  }
  params.input = normFields(raw.input, SECTIONS.input, 'input', warnings, owner);
  params.lines = normFields(raw.lines, SECTIONS.lines, 'lines', warnings, owner);

  let regions = raw.regions;
  if (regions !== undefined && !isObject(regions)) {
    warnings.push('regions: オブジェクトではないため既定値にしました');
    regions = undefined;
  }
  regions = regions || {};
  for (const key of Object.keys(regions)) {
    if (!REGIONS.includes(key)) warnings.push(`未知のキー: regions.${key}`);
  }
  const tone = ['levels', 'solid', 'layers'];
  const personSrc = normFields(regions.person, {}, 'regions.person', warnings, owner, tone);
  normRegionTone(isObject(regions.person) ? regions.person : {}, personSrc, 'regions.person', warnings);
  const bg = normFields(regions.background, BACKGROUND_FIELDS, 'regions.background', warnings, owner, tone);
  normRegionTone(isObject(regions.background) ? regions.background : {}, bg, 'regions.background', warnings);
  params.regions = { person: personSrc, background: bg };

  params.watermark = normFields(raw.watermark, SECTIONS.watermark, 'watermark', warnings, owner);
  params.output = normFields(raw.output, SECTIONS.output, 'output', warnings, owner);
  return { params, warnings };
}

export function defaultParams(opts) {
  return normalize({}, opts).params;
}

export function newLayer(overrides = {}) {
  return normFields(overrides, LAYER_FIELDS, 'layer', [], false);
}

const styleKeyOf = (s) => String(s).normalize('NFKC').toLowerCase().replace(/[\s_]/g, '');

/** スタイル名（キーまたは別名）からキーを引く。無ければ null */
export function resolveStyle(name) {
  const want = styleKeyOf(name);
  for (const [key, style] of Object.entries(STYLES)) {
    if (styleKeyOf(key) === want || styleKeyOf(style.label) === want
      || (style.aliases || []).some((a) => styleKeyOf(a) === want)) return key;
  }
  return null;
}

/**
 * 名前付きスタイルを領域に適用した新しい設定を返す。
 * 人物: ベタと層を差し替える。背景: 人物との連動を切り、描き方・ベタ・層を差し替える。
 */
export function applyStyle(params, name, region = 'person') {
  const key = resolveStyle(name);
  if (!key) throw new Error(`スタイルが見つかりません: ${name}`);
  const style = STYLES[key];
  const next = clone(params);
  const fill = (target) => {
    target.solid = { ...target.solid, ...clone(style.solid) };
    target.layers = style.layers.map((l) => newLayer(l));
  };
  if (region === 'person' || region === 'both') fill(next.regions.person);
  if (region === 'background' || region === 'both') {
    const bg = next.regions.background;
    if (bg.linked) bg.levels = clone(next.regions.person.levels);
    bg.linked = false;
    bg.mode = style.backgroundMode;
    fill(bg);
  }
  return next;
}

/** 実際に使う unit（null なら画像の幅から自動） */
export function effectiveUnit(params, imageWidth) {
  if (typeof params.unit === 'number') return params.unit;
  return Math.max(UNIT_RANGE.min, (imageWidth || SCHEMA.unitBaseWidth) / SCHEMA.unitBaseWidth);
}

/**
 * 実際に描く領域別の設定。マスクが無ければ全体が人物扱い。
 * 背景が人物と連動（linked）している間は、背景も人物の levels / solid / layers を使う。
 */
export function effectiveRegions(params, hasMask) {
  const { person, background } = params.regions;
  const mode = hasMask ? background.mode : 'keep';
  const tone = background.linked || !hasMask ? person : background;
  return {
    mode,
    person: { levels: person.levels, solid: person.solid, layers: person.layers },
    background: { levels: tone.levels, solid: tone.solid, layers: tone.layers, linked: tone === person },
  };
}

/**
 * 実際に描く透かしの設定。所有者モードでなければ enabled の値に関係なく必ず入れる。
 * 文字列は空にできず、大きさ・濃さは下限未満にならない。
 */
export function effectiveWatermark(params, owner) {
  const f = SECTIONS.watermark;
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
