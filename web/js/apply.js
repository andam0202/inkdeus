// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 andam0202
//
// 設定の一括適用のダイアログ: いま開いている画像の設定を、画像一覧で選んだ複数の画像へ写す。
// 書き換えはサーバが行う（POST api/apply-params）。ここは、範囲とバージョンの扱いを選ばせて、結果を一覧で見せるだけ。

import { el } from './panel.js';

const SCOPES = [
  ['input', '入力（自動レベル・平滑化）', true],
  ['lines', '線画', true],
  ['person', '人物のトーン', true],
  ['background', '背景のトーン', true],
  ['watermark', '透かし', true],
  ['output', '出力', true],
  ['unit', '画像倍率（unit。ふつうは画像の大きさで自動なので、写さない）', false],
];

const check = (name, label, checked, attrs = {}) => el('label', { class: 'apply-check' },
  el('input', { type: 'checkbox', name, checked, ...attrs }), el('span', {}, label));
const radio = (value, label, checked, ...more) => el('label', { class: 'apply-check' },
  el('input', { type: 'radio', name: 'apply-version', value, checked }), el('span', {}, label), ...more);
const verText = (v) => (v ? ` @${v}` : '');

/**
 * @param {object} opts
 *   source: { name, version, folder }   適用元（いま開いている画像）
 *   targets: string[]                   適用先の画像名（いまのフォルダの中）
 *   parts: string[]                     適用元の画像にある部分名
 *   canExport: boolean                  「適用後に書き出す」を出すか
 *   apply(body) → 結果                  サーバへ適用を頼む
 *   undo(id) → 結果                     その適用を取り消す
 *   exportApplied(rows)                 適用した画像を書き出す（ダイアログを閉じてから始める）
 * @returns {Promise<object|null>} 最後の適用の結果（やめたら null）
 */
export function openApplyDialog(opts) {
  return new Promise((resolve) => {
    const { source, targets } = opts;
    const dialog = el('dialog', { class: 'chooser apply-dialog', id: 'applyDialog' });
    let last = null;
    const close = () => { dialog.close(); dialog.remove(); resolve(last); };
    dialog.addEventListener('cancel', (e) => { e.preventDefault(); close(); });

    const scopeBoxes = SCOPES.map(([key, label, on]) => check(`scope-${key}`, label, on, { 'data-scope': key }));
    const partBoxes = (opts.parts || []).map((part) => check(`part-${part}`, part, true, { 'data-scope': `parts:${part}` }));
    const newName = el('input', { type: 'text', class: 'apply-new-name', maxlength: '32', placeholder: 'バージョン名',
      'aria-label': '新しいバージョンの名前' });
    newName.addEventListener('input', () => { dialog.querySelector('input[value="new"]').checked = true; });
    const force = check('apply-force', '層の消しゴムを対応づけられない画像にも適用する（その層の消しゴムは捨てる。バックアップには残る）', false);
    const exportAfter = check('apply-export', '適用後に書き出す（適用した画像を、順に書き出す）', false);
    const error = el('p', { class: 'apply-error', role: 'alert', hidden: true });
    const result = el('div', { class: 'apply-result', hidden: true });
    const btnApply = el('button', { type: 'button', class: 'primary apply-run' }, `${targets.length} 枚へ適用`);
    const btnUndo = el('button', { type: 'button', class: 'apply-undo', hidden: true }, 'いまの適用を取り消す');
    const btnClose = el('button', { type: 'button', class: 'chooser-cancel' }, 'やめる');
    const form = el('div', { class: 'apply-form' },
      el('fieldset', {}, el('legend', {}, '適用する範囲'),
        ...scopeBoxes.slice(0, 4),
        partBoxes.length ? el('div', { class: 'apply-parts' },
          el('div', { class: 'apply-note' }, '部分ごとのトーン（適用先の画像に、その部分のマスクが無ければ、その部分は飛ばす）'),
          el('div', { class: 'apply-part-list' }, ...partBoxes)) : null,
        ...scopeBoxes.slice(4)),
      el('fieldset', {}, el('legend', {}, '適用先のバージョン'),
        radio('default', '既定のバージョン', true),
        source.version ? radio('same', `いま開いているのと同じ名前のバージョン「${source.version}」（無ければ作る）`, false) : null,
        radio('new', '新しい名前のバージョンを作ってそこへ（元の設定を残したいとき）', false, newName)),
      el('p', { class: 'apply-note' }, '消しゴム・ペン・部分マスクの手直しは対象外です（画像ごとの物のため、写しません）。'
        + '適用先ごとに、前の設定はバックアップへ退避されます。'),
      force, opts.canExport ? exportAfter : null);

    function scopeNow() {
      return [...form.querySelectorAll('input[data-scope]')].filter((i) => i.checked).map((i) => i.dataset.scope);
    }
    function versionNow() {
      const kind = form.querySelector('input[name="apply-version"]:checked').value;
      return kind === 'new' ? `new:${newName.value.trim()}` : kind;
    }
    const where = (r) => `${r.name}${verText(r.version)}`;
    function listOf(title, rows, note) {
      if (!rows.length) return null;
      return el('div', { class: 'apply-group' }, el('div', { class: 'apply-group-title' }, `${title} ${rows.length} 枚`),
        el('ul', {}, ...rows.map((r) => el('li', { 'data-name': r.name }, where(r), note(r) ? el('span', { class: 'apply-why' }, ` — ${note(r)}`) : null))));
    }
    function showResult(r, undone = null) {
      const notes = (x) => [x.created ? 'バージョンを作成' : '',
        x.skippedParts && x.skippedParts.length ? `マスクの無い部分は飛ばした: ${x.skippedParts.join('・')}` : '',
        x.droppedErasers && x.droppedErasers.length ? `層の消しゴムを捨てた: ${x.droppedErasers.length} 個` : ''].filter(Boolean).join(' / ');
      result.replaceChildren(
        el('div', { class: 'apply-summary' }, undone
          ? `取り消しました: ${undone.undone.length} 枚を、適用の直前の状態へ戻しました`
          : `適用 ${r.applied.length} 枚 / 飛ばし ${r.skipped.length} 枚 / 失敗 ${r.failed.length} 枚`),
        ...(undone ? [] : [
          listOf('適用', r.applied, notes),
          listOf('飛ばし', r.skipped, (x) => x.reason || ''),
          listOf('失敗', r.failed, (x) => x.error || ''),
          !r.undoable && r.applied.length ? el('p', { class: 'apply-note' }, 'バックアップが無効のため、この適用は取り消せません') : null,
        ]));
      result.hidden = false;
      result.dataset.applied = String(undone ? 0 : r.applied.length);
      result.dataset.skipped = String(undone ? 0 : r.skipped.length);
      result.dataset.failed = String(undone ? 0 : r.failed.length);
      result.dataset.undone = undone ? '1' : '0';
      form.hidden = true;
      btnApply.hidden = true;
      btnUndo.hidden = !!undone || !r.undoable || !r.applied.length;
      btnClose.textContent = '閉じる';
    }
    const fail = (e) => { error.textContent = e.message || String(e); error.hidden = false; };
    const busy = (on) => { for (const b of [btnApply, btnUndo, btnClose]) b.disabled = on; dialog.classList.toggle('busy', on); };

    btnApply.addEventListener('click', async () => {
      error.hidden = true;
      const scope = scopeNow();
      if (!scope.length) { fail(new Error('適用する範囲を 1 つ以上選んでください')); return; }
      busy(true);
      try {
        last = await opts.apply({ from: source, to: targets, toFolder: source.folder, scope,
          toVersion: versionNow(), force: force.querySelector('input').checked });
        if (opts.canExport && exportAfter.querySelector('input').checked && last.applied.length) {
          const rows = last.applied;
          close();
          opts.exportApplied(rows);
          return;
        }
        showResult(last);
      } catch (e) {
        fail(e);
      } finally {
        busy(false);
      }
    });
    btnUndo.addEventListener('click', async () => {
      error.hidden = true;
      busy(true);
      try {
        showResult(last, await opts.undo(last.id));
      } catch (e) {
        fail(e);
      } finally {
        busy(false);
      }
    });
    btnClose.addEventListener('click', close);

    dialog.append(
      el('h3', {}, '設定をまとめて適用'),
      el('p', { class: 'chooser-message' }, `「${source.name}${verText(source.version)}」の設定を、選択した ${targets.length} 枚へ適用します。`),
      form, error, result,
      el('div', { class: 'chooser-buttons' }, btnUndo, btnApply, btnClose));
    document.body.append(dialog);
    dialog.showModal();
  });
}
