// 自分用スタイル（気に入った層の組み合わせを、名前を付けて保存した物）の一覧・保存・管理。
// 保存先はサーバ側（利用者の設定フォルダと、プロジェクトのフォルダの styles.json）。
// 適用そのもの（設定の書き換え・履歴）は app.js が、組み込みのスタイルと同じ経路で行う。

import { el } from './panel.js';
import { regionLabel, resolveStyle } from './schema.js';

const NAME_RE = /^[\p{L}\p{N}_\-()（）]{1,32}$/u;
const ORIGIN_LABELS = { user: '自分用', project: 'このプロジェクト' };

/** 名前の照合用の形（組み込みのスタイル名と同じ規則: 全角/半角・大文字小文字・空白・_ の違いを無視） */
const keyOf = (s) => String(s).normalize('NFKC').toLowerCase().replace(/[\s_]/g, '');

/** 名前として使えない理由（使えるなら ''）。サーバ側（styles.py）と同じ規則 */
export function nameProblem(name) {
  if (!NAME_RE.test(name)) return '名前に使えるのは英数字・かな漢字・_ - ( ) だけです（32 文字まで）';
  if (resolveStyle(name) || resolveStyle(name, 'person')) return '組み込みのスタイルの名前・別名と重なるので使えません';
  return '';
}

/** 自分用スタイルの 1 件を、スタイルの適用（schema.js の applyStyle）に渡す形にする */
export function customOf(row) {
  const data = row.data || {};
  return {
    solid: data.solid || {}, layers: data.layers || [], backgroundMode: data.mode || 'keep',
    ...(row.includeLevels && data.levels ? { levels: data.levels } : {}),
  };
}

/**
 * @param {object} host  app.js が渡す橋渡し:
 *   enabled()  サーバが自分用スタイルに対応しているか
 *   hasProject()  プロジェクトを開いているか（プロジェクト専用の保存先が使えるか）
 *   clientId  この画面の ID
 *   regionData(region, withLevels)  いまの設定の、その領域の中身 { data, target }
 *   apply(row, region)  スタイルを領域に当てる
 *   changed()  一覧が変わった（パネルを作り直す）
 *   message(text, kind)
 */
export function createStyleUI(host) {
  let rows = [];                      // 自分用の一覧（組み込みは含めない）
  let problems = [];

  async function refresh() {
    if (!host.enabled()) { rows = []; return rows; }
    try {
      const r = await fetch('api/styles', { cache: 'no-store' });
      const j = r.ok ? await r.json() : null;
      rows = j && Array.isArray(j.styles) ? j.styles.filter((s) => s.origin === 'user' || s.origin === 'project') : [];
      problems = j && Array.isArray(j.problems) ? j.problems : [];
    } catch {
      rows = [];
    }
    host.changed();
    return rows;
  }

  const find = (origin, name) => rows.find((r) => r.origin === origin && r.name === name) || null;

  /** その領域のスタイルの選択欄に出す物（背景向けは背景だけ、人物向けは人物・部分だけ。隠れている物は出さない） */
  function listFor(region) {
    const background = region === 'background';
    return rows.filter((r) => !r.shadowed && (r.target === 'any' || (r.target === 'background') === background));
  }

  async function request(method, name, body = null, scope = null) {
    const r = await fetch(`api/styles/${encodeURIComponent(name)}${scope ? `?scope=${encodeURIComponent(scope)}` : ''}`, {
      method, body: body ? JSON.stringify(body) : undefined,
      headers: { 'Content-Type': 'application/json', 'X-Inkdeus-Client': host.clientId },
    });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error((j.problems && j.problems.join(' / ')) || j.error || `HTTP ${r.status}`);
    return j;
  }

  /** いまの設定の、その領域の中身を、名前を付けて保存する（同じ名前があれば上書き） */
  async function save({ name, description = '', scope = 'user', includeLevels = false }, region) {
    const { data, target } = host.regionData(region, includeLevels);
    const saved = await request('PUT', name, { scope, description, target, includeLevels, data });
    await refresh();
    return saved;
  }

  async function remove(origin, name) {
    const r = await request('DELETE', name, null, origin);
    await refresh();
    return r;
  }

  async function rename(origin, name, to) {
    const problem = nameProblem(to);
    if (problem) throw new Error(problem);
    const r = await request('PATCH', name, { to }, origin);
    await refresh();
    return r;
  }

  /** 既にあるスタイルを、いまの設定の、その領域の中身で上書きする（名前・説明・保存先・明るさ補正を含めるかはそのまま） */
  async function overwrite(origin, name, region) {
    const row = find(origin, name);
    if (!row) throw new Error(`スタイルが見つかりません: ${name}`);
    return save({ name, description: row.description, scope: origin, includeLevels: row.includeLevels }, region);
  }

  // ---- ダイアログ ----

  function dialog(id, title, body, buttons) {
    const old = document.getElementById(id);
    if (old) old.remove();
    const d = el('dialog', { id, class: 'style-dialog' },
      el('h3', {}, title), body, el('div', { class: 'buttons style-dialog-buttons' }, buttons));
    // ダイアログの中のキー操作を、画面のショートカット（E / B / スペースなど）に渡さない
    for (const ev of ['keydown', 'keyup']) d.addEventListener(ev, (e) => e.stopPropagation());
    d.addEventListener('close', () => d.remove());
    document.body.append(d);
    d.showModal();
    return d;
  }

  /** 「いまの設定をスタイルとして保存…」 */
  function openSave(region) {
    const name = el('input', { type: 'text', id: 'styleName', maxlength: 32, placeholder: '例: 細かい影' });
    const desc = el('input', { type: 'text', id: 'styleDesc', maxlength: 200, placeholder: '（任意）' });
    const scopeUser = el('input', { type: 'radio', name: 'styleScope', id: 'styleScopeUser', value: 'user' });
    const scopeProject = el('input', { type: 'radio', name: 'styleScope', id: 'styleScopeProject', value: 'project',
      disabled: !host.hasProject() });
    scopeUser.checked = true;
    const levels = el('input', { type: 'checkbox', id: 'styleLevels' });
    const note = el('div', { class: 'note', id: 'styleSaveNote' });
    const ok = el('button', { type: 'button', class: 'primary', id: 'styleSaveOK' }, '保存');
    const cancel = el('button', { type: 'button', id: 'styleSaveCancel' }, 'キャンセル');
    const scope = () => (scopeProject.checked ? 'project' : 'user');
    const update = () => {
      const value = name.value.trim();
      const problem = value ? nameProblem(value) : ' ';
      const exists = rows.some((r) => r.origin === scope() && keyOf(r.name) === keyOf(value));
      note.textContent = problem.trim() || (exists ? '同じ名前のスタイルがあります。保存すると上書きします。' : '');
      note.classList.toggle('warn', !!problem.trim() || exists);
      ok.textContent = exists ? '上書き保存' : '保存';
      ok.disabled = !!problem;
    };
    for (const node of [name, scopeUser, scopeProject]) node.addEventListener('input', update);
    const d = dialog('styleSaveDialog', `${regionLabel(region)}の設定をスタイルとして保存`, el('div', { class: 'style-form' },
      el('label', { for: 'styleName' }, '名前'), name,
      el('label', { for: 'styleDesc' }, '説明'), desc,
      el('span', {}, '保存先'),
      el('span', { class: 'style-scopes' },
        el('label', { class: 'check', title: 'どのプロジェクトでも使える（利用者の設定フォルダに保存）' }, scopeUser, '自分用（全プロジェクト共通）'),
        el('label', { class: 'check', title: host.hasProject() ? 'このプロジェクトのフォルダに保存' : 'プロジェクトを開いているときだけ使えます' },
          scopeProject, 'このプロジェクト')),
      el('span', {}, '中身'),
      el('span', {},
        el('div', { class: 'note' }, `ベタと層${region === 'background' ? '・背景の描き方' : ''}を保存します。`),
        el('label', { class: 'check', title: '当てたとき、明るさ補正（明るさ・コントラスト・ガンマ）もこの値にする' }, levels, '明るさ補正も含める')),
      note), [ok, cancel]);
    cancel.addEventListener('click', () => d.close());
    const submit = async () => {
      if (ok.disabled) return;
      ok.disabled = true;
      try {
        await save({ name: name.value.trim(), description: desc.value.trim(), scope: scope(), includeLevels: levels.checked }, region);
        d.close();
        host.message(`スタイル「${name.value.trim()}」を保存しました（${ORIGIN_LABELS[scope()]}）`);
      } catch (e) {
        note.textContent = e.message;
        note.classList.add('warn');
        ok.disabled = false;
      }
    };
    ok.addEventListener('click', submit);
    for (const node of [name, desc]) node.addEventListener('keydown', (e) => { if (e.key === 'Enter') submit(); });
    update();
    name.focus();
    return d;
  }

  /** 「自分用スタイルを管理…」（名前変更・削除・上書き保存・適用） */
  function openManage(region) {
    const body = el('div', { class: 'style-list', id: 'styleList' });
    const close = el('button', { type: 'button', id: 'styleManageClose' }, '閉じる');
    const d = dialog('styleManageDialog', '自分用スタイルの管理', body, [close]);
    close.addEventListener('click', () => d.close());
    const run = (fn) => async () => {
      try {
        await fn();
      } catch (e) {
        host.message(e.message, 'error');
      }
      render();
    };
    function render() {
      const items = rows.map((row) => el('div', { class: 'style-row', 'data-name': row.name, 'data-origin': row.origin },
        el('div', { class: 'style-row-head' },
          el('span', { class: 'style-row-name' }, row.name),
          el('span', { class: 'style-origin', 'data-origin': row.origin }, ORIGIN_LABELS[row.origin]),
          row.shadowed ? el('span', { class: 'note warn', title: '同じ名前のスタイルが「このプロジェクト」にあり、そちらが使われます' }, '（隠れています）') : null,
          el('span', { class: 'note' }, `層 ${row.layers}・ベタ${row.solid ? 'あり' : 'なし'}`
            + `${row.includeLevels ? '・明るさ補正つき' : ''}${row.target === 'background' ? '・背景向け' : ''}`)),
        row.description ? el('div', { class: 'note' }, row.description) : null,
        el('div', { class: 'buttons' },
          el('button', { type: 'button', class: 'style-apply', title: `${regionLabel(region)}に当てる`,
            disabled: !listFor(region).includes(row),
            onclick: () => { host.apply(row, region); d.close(); } }, `${regionLabel(region)}に当てる`),
          el('button', { type: 'button', class: 'style-rename', onclick: run(async () => {
            const to = window.prompt(`スタイル「${row.name}」の新しい名前`, row.name);
            if (to !== null && to.trim() && to.trim() !== row.name) await rename(row.origin, row.name, to.trim());
          }) }, '名前変更'),
          el('button', { type: 'button', class: 'style-overwrite', title: `いまの${regionLabel(region)}の設定で、このスタイルを上書きする`,
            onclick: run(async () => {
              if (window.confirm(`スタイル「${row.name}」を、いまの${regionLabel(region)}の設定で上書きします。よろしいですか？`)) {
                await overwrite(row.origin, row.name, region);
              }
            }) }, '上書き保存'),
          el('button', { type: 'button', class: 'style-delete', onclick: run(async () => {
            if (window.confirm(`スタイル「${row.name}」（${ORIGIN_LABELS[row.origin]}）を削除します。よろしいですか？`)) {
              await remove(row.origin, row.name);
            }
          }) }, '削除'))));
      body.replaceChildren(
        ...(items.length ? items : [el('div', { class: 'note' },
          '自分用スタイルはまだありません。スタイルの選択欄の「いまの設定をスタイルとして保存…」で作れます。')]),
        ...problems.map((p) => el('div', { class: 'note warn' }, p)));
    }
    render();
    return d;
  }

  return { refresh, list: () => rows.slice(), listFor, find, save, remove, rename, overwrite, openSave, openManage,
    problems: () => problems.slice() };
}
