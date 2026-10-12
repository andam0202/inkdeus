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

// 層の id（消しゴムのマスクがどの層の物かを決める名前）。短い乱数文字列で、並べ替えても変わらない
const LAYER_ID_RE = /^[a-z0-9]{4,16}$/;
const ID_CHARS = 'abcdefghijklmnopqrstuvwxyz0123456789';

export function newId(taken = null) {
  for (;;) {
    let id = '';
    for (let i = 0; i < 8; i++) id += ID_CHARS[Math.floor(Math.random() * ID_CHARS.length)];
    if (!taken || !taken.has(id)) return id;
  }
}

/** 設定に含まれる層の id（人物・背景の両方） */
export function layerIds(params) {
  const out = new Set();
  for (const r of REGIONS) {
    for (const l of (params && params.regions && params.regions[r] && params.regions[r].layers) || []) {
      if (l && l.id) out.add(l.id);
    }
  }
  return out;
}

/** 層の id を除いた設定（id だけが違う設定を「同じ」と見なすための比較用、と、id を知らない古いサーバへの保存用） */
export function withoutIds(params) {
  return JSON.parse(JSON.stringify(params, (key, value) => (key === 'id' ? undefined : value)));
}

function normValue(v, f, path, problems) {
  const def = f.def;
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
function normFields(raw, fields, path, problems, extra = []) {
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
    out[key] = normValue(src[key], f, `${path}.${key}`, problems);
  }
  return out;
}

function normLayers(raw, path, problems, fallback, ids) {
  let layers = raw === undefined ? fallback : raw;
  if (!Array.isArray(layers)) {
    problems.push(`${path}: 配列ではないため既定値にしました`);
    layers = fallback;
  }
  if (layers.length > MAX_LAYERS) {
    problems.push(`${path}: ${layers.length} 層あります。先頭の ${MAX_LAYERS} 層だけ使います`);
    layers = layers.slice(0, MAX_LAYERS);
  }
  return layers.map((l, i) => {
    const fields = normFields(l, LAYER_FIELDS, `${path}[${i}]`, problems, ['id']);
    let id = isObject(l) ? l.id : undefined;
    if (id !== undefined && id !== null && id !== '' && !(typeof id === 'string' && LAYER_ID_RE.test(id))) {
      problems.push(`${path}[${i}].id: 値 ${JSON.stringify(id)} は使えないため振り直しました`);
      id = undefined;
    }
    if (!id || ids.taken.has(id)) {                 // 無い・重複している（複製された層）ときは振り直す
      id = newId(ids.taken);
      ids.filled = true;
    }
    ids.taken.add(id);
    return { id, ...fields };
  });
}

/** 領域（人物 / 背景）に共通の levels・solid・layers を out に足す */
function normRegionTone(src, out, path, problems, ids) {
  const style = STYLES[SCHEMA.defaultStyle];
  out.levels = normFields(src.levels, LEVEL_FIELDS, `${path}.levels`, problems);
  out.solid = normFields(src.solid === undefined ? style.solid : src.solid, SOLID_FIELDS, `${path}.solid`, problems);
  out.layers = normLayers(src.layers, `${path}.layers`, problems, style.layers, ids);
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
export function normalize(input) {
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
  params.input = normFields(raw.input, SECTIONS.input, 'input', warnings);
  params.lines = normFields(raw.lines, SECTIONS.lines, 'lines', warnings);

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
  const ids = { taken: new Set(), filled: false };   // 層の id は、人物・背景を通して重複させない
  const personSrc = normFields(regions.person, {}, 'regions.person', warnings, tone);
  normRegionTone(isObject(regions.person) ? regions.person : {}, personSrc, 'regions.person', warnings, ids);
  const bg = normFields(regions.background, BACKGROUND_FIELDS, 'regions.background', warnings, tone);
  normRegionTone(isObject(regions.background) ? regions.background : {}, bg, 'regions.background', warnings, ids);
  params.regions = { person: personSrc, background: bg };

  params.watermark = normFields(raw.watermark, SECTIONS.watermark, 'watermark', warnings);
  params.output = normFields(raw.output, SECTIONS.output, 'output', warnings);
  // filledIds: id の無い層に新しい id を振った（層の id が入る前に保存された設定を読んだ）
  return { params, warnings, filledIds: ids.filled };
}

export function defaultParams() {
  return normalize({}).params;
}

/** 新しい層（新しい id つき）。taken は、その設定で既に使われている id */
export function newLayer(overrides = {}, taken = null) {
  return { id: newId(taken), ...normFields(overrides, LAYER_FIELDS, 'layer', [], ['id']) };
}

/**
 * id の書かれていない層に、同じ位置にあった層の id を引き継がせる
 * （層を丸ごと書き直した設定を読み込んでも、その層の消しゴムのマスクが外れないように）
 */
export function inheritIds(raw, base) {
  if (!isObject(raw) || !isObject(raw.regions) || !base) return raw;
  const out = clone(raw);
  const used = layerIds(out);
  for (const r of REGIONS) {
    const layers = isObject(out.regions[r]) ? out.regions[r].layers : null;
    const before = (base.regions && base.regions[r] && base.regions[r].layers) || [];
    if (!Array.isArray(layers)) continue;
    layers.forEach((l, i) => {
      const id = before[i] && before[i].id;
      if (isObject(l) && !l.id && id && !used.has(id)) { l.id = id; used.add(id); }
    });
  }
  return out;
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
  const taken = layerIds(next);
  const fill = (target) => {
    // 層は入れ替わるので、新しい id を振る（前の層の消しゴムのマスクは引き継がない）
    target.solid = { ...target.solid, ...clone(style.solid) };
    target.layers = style.layers.map((l) => { const made = newLayer(l, taken); taken.add(made.id); return made; });
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
 * 実際に描く透かしの設定（入れるかどうかは enabled のとおり）。
 * 描画が壊れないよう、文字列が空なら既定の文字列を使い、大きさ・濃さは最小値未満にしない。
 */
export function effectiveWatermark(params) {
  const f = SECTIONS.watermark;
  const w = params.watermark;
  const txt = (w.text || '').trim();
  return {
    enabled: w.enabled === true,
    text: txt || WATERMARK_FALLBACK_TEXT,
    position: w.position,
    size: Math.max(f.size.min, w.size),
    opacity: Math.max(f.opacity.min, w.opacity),
  };
}
