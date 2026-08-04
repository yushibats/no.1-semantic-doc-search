import { apiCall as authApiCall } from './auth.js';
import { showToast } from './utils.js';

const state = {
  folders: [],
  groups: [],
  tags: [],
  rulesets: [],
  profiles: [],
  documentSets: [],
  conceptSettings: null,
  pendingConcepts: [],
  pendingConceptsOpen: false,
  selectedPendingConceptIds: new Set(),
  pendingConceptBusy: false
};
const EMPTY_FOLDER_DEFAULTS = {
  tag_ids: [],
  customer_name_raw: null,
  document_year: null,
  document_month: null,
  date_precision: 'UNKNOWN'
};
const escapeHtml = value => String(value ?? '')
  .replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;')
  .replaceAll('"', '&quot;').replaceAll("'", '&#039;');

function flattenFolders(nodes, depth = 0, result = []) {
  for (const node of nodes || []) {
    result.push({ ...node, depth });
    flattenFolders(node.children, depth + 1, result);
  }
  return result;
}

export async function loadMetadataSettings() {
  const root = document.getElementById('metadataSettingsRoot');
  if (!root) return;
  root.innerHTML = '<div class="apex-region"><div style="padding:24px">文書分類設定を読み込んでいます...</div></div>';
  try {
    [state.folders, state.groups, state.tags, state.rulesets, state.profiles, state.documentSets, state.conceptSettings, state.pendingConcepts] = await Promise.all([
      authApiCall('/ai/api/document-library/folders'),
      authApiCall('/ai/api/document-library/settings/tag-groups'),
      authApiCall('/ai/api/document-library/settings/tags'),
      authApiCall('/ai/api/document-library/settings/rulesets'),
      authApiCall('/ai/api/document-library/settings/folder-profiles'),
      authApiCall('/ai/api/document-library/document-sets?include_archived=true&limit=500'),
      authApiCall('/ai/api/document-library/settings/search-concepts'),
      authApiCall('/ai/api/document-library/search-concepts?status=PENDING&limit=200')
    ]);
    render();
  } catch (error) {
    root.innerHTML = `<div class="apex-region"><div class="retrieval-message error">${escapeHtml(error.message)}。DB管理で文書ライブラリの追加スキーマを初期化してください。</div></div>`;
  }
}

function render() {
  const root = document.getElementById('metadataSettingsRoot');
  const ruleset = state.rulesets.find(item => item.code === 'default') || state.rulesets[0];
  const groupRows = state.groups.map(group => `<tr><td>${escapeHtml(group.name)}</td><td><code>${escapeHtml(group.code)}</code></td><td>${group.selection_mode === 'SINGLE' ? '排他（1件）' : '複数可'}</td><td>${group.active ? '有効' : '無効'}</td><td><button class="apex-button-secondary apex-button-xs" onclick="window.metadataSettingsModule.editGroup('${escapeHtml(group.group_id)}')">編集</button> <button class="apex-button-secondary apex-button-xs" onclick="window.metadataSettingsModule.toggleGroup('${escapeHtml(group.group_id)}')">${group.active ? '無効化' : '有効化'}</button></td></tr>`).join('');
  const groupById = new Map(state.groups.map(group => [group.group_id, group]));
  const tagRows = state.tags.map(tag => `<tr><td>${escapeHtml(groupById.get(tag.group_id)?.name || '')}</td><td>${escapeHtml(tag.name)}</td><td><code>${escapeHtml(tag.code)}</code></td><td>${tag.active ? '有効' : '無効'}</td><td><button class="apex-button-secondary apex-button-xs" onclick="window.metadataSettingsModule.editTag('${escapeHtml(tag.tag_id)}')">編集</button> <button class="apex-button-secondary apex-button-xs" onclick="window.metadataSettingsModule.toggleTag('${escapeHtml(tag.tag_id)}')">${tag.active ? '無効化' : '有効化'}</button></td></tr>`).join('');
  const folderOptions = flattenFolders(state.folders).map(folder => `<option value="${escapeHtml(folder.folder_id)}">${'　'.repeat(folder.depth)}${escapeHtml(folder.name)}</option>`).join('');
  const rulesetOptions = state.rulesets.map(item => `<option value="${escapeHtml(item.ruleset_id)}">${escapeHtml(item.name)}</option>`).join('');
  const conceptSettings = state.conceptSettings || {};
  const documentSetRows = state.documentSets.map(item => `<tr>
    <td><strong>${escapeHtml(item.label)}</strong>${item.description ? `<br><small>${escapeHtml(item.description)}</small>` : ''}</td>
    <td>${Number(item.document_count || 0)}文書</td>
    <td>${item.status === 'ACTIVE' ? '有効' : 'アーカイブ済み'}</td>
    <td><button class="apex-button-secondary apex-button-xs" onclick="window.metadataSettingsModule.editDocumentSet('${escapeHtml(item.document_set_id)}')">編集</button> <button class="apex-button-secondary apex-button-xs" onclick="window.metadataSettingsModule.toggleDocumentSet('${escapeHtml(item.document_set_id)}')">${item.status === 'ACTIVE' ? 'アーカイブ' : '再有効化'}</button></td>
  </tr>`).join('');
  const pendingConceptRows = state.pendingConcepts.map(concept => `<tr data-pending-concept="${escapeHtml(concept.concept_id)}">
    <td class="metadata-concept-select"><input type="checkbox" data-pending-concept-select value="${escapeHtml(concept.concept_id)}" aria-label="${escapeHtml(concept.display_label)}を選択" onchange="window.metadataSettingsModule.togglePendingConceptSelection('${escapeHtml(concept.concept_id)}',this.checked)"></td>
    <td>${concept.facet === 'BEFORE' ? '現況の課題' : concept.facet === 'AFTER' ? '実現したいこと' : 'その他'}</td>
    <td>${escapeHtml(concept.category_name)}</td>
    <td><strong>${escapeHtml(concept.display_label)}</strong></td>
    <td>${concept.support_set_count}案件 / ${concept.support_document_count}文書</td>
    <td><button type="button" data-pending-concept-action class="apex-button apex-button-xs" onclick="window.metadataSettingsModule.setConceptStatus('${escapeHtml(concept.concept_id)}','ACTIVE')">承認</button> <button type="button" data-pending-concept-action class="apex-button-secondary apex-button-xs" onclick="window.metadataSettingsModule.setConceptStatus('${escapeHtml(concept.concept_id)}','HIDDEN')">非表示</button></td>
  </tr>`).join('');
  root.innerHTML = `<div class="apex-region metadata-settings-region">
    <div class="apex-region-header"><span><i class="fas fa-tags"></i> 文書階層・タグ・自動分類設定</span><span class="metadata-chip">顧客名は型付き自由文字</span></div>
    <div class="metadata-settings-content">
      <details open><summary>タグマスターと排他グループ</summary>
        <div class="metadata-settings-actions"><button class="apex-button-secondary px-3 py-2" onclick="window.metadataSettingsModule.createGroup()"><i class="fas fa-plus"></i> グループ追加</button><button class="apex-button-secondary px-3 py-2" onclick="window.metadataSettingsModule.createTag()"><i class="fas fa-plus"></i> タグ追加</button></div>
        <div class="metadata-settings-tables"><table class="apex-table"><thead><tr><th>グループ</th><th>コード</th><th>選択</th><th>状態</th><th>操作</th></tr></thead><tbody>${groupRows}</tbody></table><table class="apex-table"><thead><tr><th>グループ</th><th>タグ</th><th>コード</th><th>状態</th><th>操作</th></tr></thead><tbody>${tagRows}</tbody></table></div>
      </details>
      <details open><summary>案件グループ</summary>
        <p class="form-help">同じ案件の現況図・提案図・写真をまとめる論理グループです。顧客名だけでは自動統合せず、文書管理画面で利用者が確認して割り当てます。</p>
        <div class="metadata-settings-actions"><button class="apex-button-secondary px-3 py-2" onclick="window.metadataSettingsModule.createDocumentSet()"><i class="fas fa-plus"></i> 案件グループを作成</button></div>
        <div class="metadata-settings-tables metadata-settings-single-table"><table class="apex-table"><thead><tr><th>名称・説明</th><th>文書数</th><th>状態</th><th>操作</th></tr></thead><tbody>${documentSetRows || '<tr><td colspan="4">案件グループはありません</td></tr>'}</tbody></table></div>
      </details>
      <details open><summary>ファイル名ルール・年月・顧客正規化・LLM分類</summary>
        <p class="form-help">完全一致、語句組合せ、除外語、拡張子、優先順位、顧客抽出、年月正規表現、先行解析ページ数、閉じたタグ候補用LLMプロンプトをJSONで管理します。保存すると改訂履歴が作成されます。</p>
        <textarea id="classificationRulesetJson" class="form-input metadata-json-editor" spellcheck="false">${escapeHtml(JSON.stringify(ruleset?.config || {}, null, 2))}</textarea>
        <div class="metadata-settings-actions"><button class="apex-button px-4 py-2" onclick="window.metadataSettingsModule.saveRuleset()"><i class="fas fa-save"></i> ルール改訂を保存</button></div>
        <div class="rule-test-panel"><label class="form-label" for="ruleTestFilenames">ルールテスト（1行1ファイル）</label><textarea id="ruleTestFilenames" class="form-input" rows="5" placeholder="20240203_森様邸_計画図.pdf\n01.現況-1F 平面図.pdf"></textarea><button class="apex-button-secondary px-3 py-2" onclick="window.metadataSettingsModule.testRules()"><i class="fas fa-vial"></i> テスト</button><pre id="ruleTestResult" class="metadata-test-result"></pre></div>
      </details>
      <details open><summary>検索候補（AIコンセプト）の抽出・公開設定</summary>
        <p class="form-help">正規化済み本文（ネイティブ抽出・MinerU・OCR）と保存済みVLM文章を入力に使います。抽出失敗は索引公開を止めません。自動公開には信頼度と、確認済み案件グループでの利用数の両方が必要です。</p>
        <div class="metadata-filter-grid">
          <label class="metadata-inline-check"><input id="conceptSettingsEnabled" type="checkbox" ${conceptSettings.enabled ? 'checked' : ''}> AIコンセプト抽出を使用する</label>
          <label class="metadata-inline-check"><input id="conceptSettingsAutoPublish" type="checkbox" ${conceptSettings.auto_publish ? 'checked' : ''}> 強い候補を自動公開する</label>
          <div><label class="form-label" for="conceptConfidence">自動公開の最低confidence</label><input id="conceptConfidence" class="form-input" type="number" min="0" max="1" step="0.01" value="${conceptSettings.auto_publish_confidence ?? 0.85}"></div>
          <div><label class="form-label" for="conceptMinSupport">最低案件数</label><input id="conceptMinSupport" class="form-input" type="number" min="1" max="1000" value="${conceptSettings.min_support_sets ?? 2}"><small class="form-help">未グループ文書は案件数に含めません。</small></div>
          <div><label class="form-label" for="conceptMaxPerDocument">1文書の最大候補数</label><input id="conceptMaxPerDocument" class="form-input" type="number" min="1" max="100" value="${conceptSettings.max_concepts_per_document ?? 16}"></div>
          <div><label class="form-label" for="conceptInitialDisplay">検索画面のカテゴリ別初期表示数</label><input id="conceptInitialDisplay" class="form-input" type="number" min="1" max="100" value="${conceptSettings.initial_display_limit ?? 8}"></div>
          <div><label class="form-label" for="conceptInputLimit">AI入力テキスト上限（文字）</label><input id="conceptInputLimit" class="form-input" type="number" min="1000" max="100000" step="1000" value="${conceptSettings.input_text_limit ?? 24000}"></div>
        </div>
        <label class="form-label" for="conceptPromptText">抽出プロンプト</label>
        <textarea id="conceptPromptText" class="form-input metadata-json-editor" rows="10">${escapeHtml(conceptSettings.prompt_text || '')}</textarea>
        <div class="metadata-settings-actions"><button class="apex-button px-4 py-2" onclick="window.metadataSettingsModule.saveConceptSettings()"><i class="fas fa-save"></i> コンセプト設定を保存</button></div>
        <details id="pendingConceptReview" class="pending-concept-review" ${state.pendingConceptsOpen ? 'open' : ''}>
          <summary><span id="pendingConceptSummaryText"><i class="fas fa-triangle-exclamation"></i> ${state.pendingConcepts.length ? '要確認候補があります' : '要確認候補はありません'}</span><span id="pendingConceptCount" class="metadata-chip pending-concept-count">${state.pendingConcepts.length}件</span></summary>
          <p class="form-help">自動公開条件を満たさない新語です。承認した候補だけが検索画面に表示されます。</p>
          <div class="pending-concept-toolbar">
            <label class="metadata-inline-check"><input id="pendingConceptSelectAll" type="checkbox" onchange="window.metadataSettingsModule.toggleAllPendingConcepts(this.checked)"> すべて選択</label>
            <span id="pendingConceptSelectedCount">0件選択中</span>
            <button id="approveSelectedConcepts" type="button" class="apex-button px-3 py-2" disabled onclick="window.metadataSettingsModule.setSelectedConceptStatus('ACTIVE')"><i class="fas fa-check"></i> 選択を一括承認</button>
            <button id="hideSelectedConcepts" type="button" class="apex-button-secondary px-3 py-2" disabled onclick="window.metadataSettingsModule.setSelectedConceptStatus('HIDDEN')"><i class="fas fa-eye-slash"></i> 選択を一括非表示</button>
          </div>
          <div class="metadata-settings-tables metadata-settings-single-table pending-concept-table"><table class="apex-table"><thead><tr><th class="metadata-concept-select"></th><th>区分</th><th>カテゴリ</th><th>候補</th><th>利用</th><th>操作</th></tr></thead><tbody id="pendingConceptRows">${pendingConceptRows || '<tr data-pending-concept-empty><td colspan="6">要確認候補はありません</td></tr>'}</tbody></table></div>
        </details>
      </details>
      <details><summary>フォルダ別ルールプロファイル・既定値</summary>
        <div class="metadata-filter-grid"><div><label class="form-label">フォルダ</label><select id="folderProfileFolder" class="form-input">${folderOptions}</select></div><div><label class="form-label">ルールセット</label><select id="folderProfileRuleset" class="form-input">${rulesetOptions}</select></div><div><label class="metadata-inline-check"><input id="folderProfileInherit" type="checkbox" checked> 子孫へ継承</label></div></div>
        <label class="form-label" for="folderProfileDefaults">既定属性JSON</label><textarea id="folderProfileDefaults" class="form-input" rows="6">{\n  "tag_ids": [],\n  "customer_name_raw": null,\n  "document_year": null,\n  "document_month": null,\n  "date_precision": "UNKNOWN"\n}</textarea>
        <div class="metadata-settings-actions"><button class="apex-button px-4 py-2" onclick="window.metadataSettingsModule.saveFolderProfile()"><i class="fas fa-save"></i> フォルダ設定を保存</button></div>
      </details>
    </div>
  </div>`;
  const pendingDetails = document.getElementById('pendingConceptReview');
  pendingDetails?.addEventListener('toggle', () => {
    state.pendingConceptsOpen = pendingDetails.open;
  });
  updatePendingConceptReviewUi();
  document.getElementById('folderProfileFolder')?.addEventListener('change', loadSelectedFolderProfile);
  loadSelectedFolderProfile();
}

function loadSelectedFolderProfile() {
  const folderId = document.getElementById('folderProfileFolder')?.value;
  const profile = state.profiles.find(item => item.folder_id === folderId);
  const fallbackRuleset = state.rulesets.find(item => item.code === 'default') || state.rulesets[0];
  document.getElementById('folderProfileRuleset').value = profile?.ruleset_id || fallbackRuleset?.ruleset_id || '';
  document.getElementById('folderProfileInherit').checked = profile?.inherit_to_descendants ?? true;
  document.getElementById('folderProfileDefaults').value = JSON.stringify(profile?.defaults || EMPTY_FOLDER_DEFAULTS, null, 2);
}

export async function createGroup() {
  const name = window.prompt('タググループ名');
  if (!name?.trim()) return;
  const code = window.prompt('英数字コード（例: document_kind）');
  if (!code?.trim()) return;
  const single = window.confirm('このグループを排他（1文書につき1タグ）にしますか？');
  await authApiCall('/ai/api/document-library/settings/tag-groups', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ code: code.trim(), name: name.trim(), selection_mode: single ? 'SINGLE' : 'MULTI', active: true, sort_order: state.groups.length * 10 })
  });
  showToast('タググループを追加しました', 'success');
  await loadMetadataSettings();
}

export async function createTag() {
  if (!state.groups.length) return;
  const groupList = state.groups.map((group, index) => `${index + 1}: ${group.name}`).join('\n');
  const groupNumber = Number(window.prompt(`所属グループ番号\n${groupList}`));
  const group = state.groups[groupNumber - 1];
  if (!group) return;
  const name = window.prompt('タグ名');
  if (!name?.trim()) return;
  const code = window.prompt('英数字コード');
  if (!code?.trim()) return;
  await authApiCall('/ai/api/document-library/settings/tags', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ group_id: group.group_id, code: code.trim(), name: name.trim(), active: true, sort_order: state.tags.length * 10 })
  });
  showToast('タグを追加しました', 'success');
  await loadMetadataSettings();
}

async function saveGroup(group, updates) {
  await authApiCall(`/ai/api/document-library/settings/tag-groups/${encodeURIComponent(group.group_id)}`, {
    method: 'PUT', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      code: group.code,
      name: group.name,
      selection_mode: group.selection_mode,
      active: group.active,
      sort_order: group.sort_order,
      ...updates
    })
  });
  await loadMetadataSettings();
}

export async function editGroup(groupId) {
  const group = state.groups.find(item => item.group_id === groupId);
  if (!group) return;
  const name = window.prompt('タググループ名', group.name);
  if (!name?.trim()) return;
  const selectionMode = window.prompt('選択方式（SINGLE または MULTI）', group.selection_mode)?.trim().toUpperCase();
  if (!['SINGLE', 'MULTI'].includes(selectionMode)) throw new Error('選択方式はSINGLEまたはMULTIです');
  await saveGroup(group, { name: name.trim(), selection_mode: selectionMode });
  showToast('タググループを更新しました', 'success');
}

export async function toggleGroup(groupId) {
  const group = state.groups.find(item => item.group_id === groupId);
  if (!group) return;
  await saveGroup(group, { active: !group.active });
  showToast(`タググループを${group.active ? '無効化' : '有効化'}しました`, 'success');
}

async function saveTag(tag, updates) {
  await authApiCall(`/ai/api/document-library/settings/tags/${encodeURIComponent(tag.tag_id)}`, {
    method: 'PUT', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      group_id: tag.group_id,
      code: tag.code,
      name: tag.name,
      active: tag.active,
      sort_order: tag.sort_order,
      ...updates
    })
  });
  await loadMetadataSettings();
}

export async function editTag(tagId) {
  const tag = state.tags.find(item => item.tag_id === tagId);
  if (!tag) return;
  const name = window.prompt('タグ名', tag.name);
  if (!name?.trim()) return;
  await saveTag(tag, { name: name.trim() });
  showToast('タグを更新しました', 'success');
}

export async function toggleTag(tagId) {
  const tag = state.tags.find(item => item.tag_id === tagId);
  if (!tag) return;
  await saveTag(tag, { active: !tag.active });
  showToast(`タグを${tag.active ? '無効化' : '有効化'}しました`, 'success');
}

export async function saveRuleset() {
  const ruleset = state.rulesets.find(item => item.code === 'default') || state.rulesets[0];
  if (!ruleset) throw new Error('ルールセットがありません');
  const config = JSON.parse(document.getElementById('classificationRulesetJson').value);
  await authApiCall(`/ai/api/document-library/settings/rulesets/${encodeURIComponent(ruleset.ruleset_id)}`, {
    method: 'PUT', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ code: ruleset.code, name: ruleset.name, enabled: ruleset.enabled, config })
  });
  showToast('分類ルールの新しい改訂を保存しました', 'success');
  await loadMetadataSettings();
}

export async function testRules() {
  const filenames = document.getElementById('ruleTestFilenames').value.split('\n').map(value => value.trim()).filter(Boolean);
  const config = JSON.parse(document.getElementById('classificationRulesetJson').value);
  const result = await authApiCall('/ai/api/document-library/settings/rules/test', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ filenames, config })
  });
  document.getElementById('ruleTestResult').textContent = JSON.stringify(result, null, 2);
}

export async function saveFolderProfile() {
  const folderId = document.getElementById('folderProfileFolder').value;
  const payload = {
    ruleset_id: document.getElementById('folderProfileRuleset').value,
    inherit_to_descendants: document.getElementById('folderProfileInherit').checked,
    defaults: JSON.parse(document.getElementById('folderProfileDefaults').value)
  };
  await authApiCall(`/ai/api/document-library/settings/folder-profiles/${encodeURIComponent(folderId)}`, {
    method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload)
  });
  showToast('フォルダ別設定を保存しました', 'success');
  await loadMetadataSettings();
}

export async function createDocumentSet() {
  const label = window.prompt('案件グループ名（顧客名だけでなく、場所・年月・工事名など識別できる名称を推奨）');
  if (!label?.trim()) return;
  const description = window.prompt('説明（任意）', '') || null;
  await authApiCall('/ai/api/document-library/document-sets', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ label: label.trim(), description })
  });
  showToast('案件グループを作成しました', 'success');
  await loadMetadataSettings();
}

export async function editDocumentSet(documentSetId) {
  const item = state.documentSets.find(value => value.document_set_id === documentSetId);
  if (!item) return;
  const label = window.prompt('案件グループ名', item.label);
  if (!label?.trim()) return;
  const description = window.prompt('説明（任意）', item.description || '');
  await authApiCall(`/ai/api/document-library/document-sets/${encodeURIComponent(documentSetId)}`, {
    method: 'PATCH', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ label: label.trim(), description: description || null })
  });
  showToast('案件グループを更新しました', 'success');
  await loadMetadataSettings();
}

export async function toggleDocumentSet(documentSetId) {
  const item = state.documentSets.find(value => value.document_set_id === documentSetId);
  if (!item) return;
  const nextStatus = item.status === 'ACTIVE' ? 'ARCHIVED' : 'ACTIVE';
  if (nextStatus === 'ARCHIVED' && !window.confirm(`「${item.label}」をアーカイブしますか？\n文書との関連は保持され、検索結果の既存グループ表示にも残ります。`)) return;
  await authApiCall(`/ai/api/document-library/document-sets/${encodeURIComponent(documentSetId)}`, {
    method: 'PATCH', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ status: nextStatus })
  });
  showToast(nextStatus === 'ACTIVE' ? '案件グループを再有効化しました' : '案件グループをアーカイブしました', 'success');
  await loadMetadataSettings();
}

function pendingConceptIds() {
  return state.pendingConcepts.map(concept => String(concept.concept_id));
}

function updatePendingConceptReviewUi() {
  const validIds = new Set(pendingConceptIds());
  for (const conceptId of [...state.selectedPendingConceptIds]) {
    if (!validIds.has(conceptId)) state.selectedPendingConceptIds.delete(conceptId);
  }
  document.querySelectorAll('[data-pending-concept-select]').forEach(input => {
    input.checked = state.selectedPendingConceptIds.has(String(input.value));
    input.disabled = state.pendingConceptBusy;
  });
  const selectedCount = state.selectedPendingConceptIds.size;
  const selectAll = document.getElementById('pendingConceptSelectAll');
  if (selectAll) {
    selectAll.checked = validIds.size > 0 && selectedCount === validIds.size;
    selectAll.indeterminate = selectedCount > 0 && selectedCount < validIds.size;
    selectAll.disabled = state.pendingConceptBusy || !validIds.size;
  }
  const count = document.getElementById('pendingConceptCount');
  if (count) count.textContent = `${validIds.size}件`;
  const summary = document.getElementById('pendingConceptSummaryText');
  if (summary) summary.innerHTML = `<i class="fas fa-triangle-exclamation"></i> ${validIds.size ? '要確認候補があります' : '要確認候補はありません'}`;
  const selected = document.getElementById('pendingConceptSelectedCount');
  if (selected) selected.textContent = `${selectedCount}件選択中`;
  for (const id of ['approveSelectedConcepts', 'hideSelectedConcepts']) {
    const button = document.getElementById(id);
    if (button) button.disabled = state.pendingConceptBusy || selectedCount === 0;
  }
}

function removePendingConceptRows(conceptIds) {
  const removed = new Set(conceptIds.map(String));
  state.pendingConcepts = state.pendingConcepts.filter(
    concept => !removed.has(String(concept.concept_id))
  );
  for (const conceptId of removed) {
    state.selectedPendingConceptIds.delete(conceptId);
    document.querySelector(`[data-pending-concept="${CSS.escape(conceptId)}"]`)?.remove();
  }
  const rows = document.getElementById('pendingConceptRows');
  if (rows && !rows.querySelector('[data-pending-concept]')) {
    rows.innerHTML = '<tr data-pending-concept-empty><td colspan="6">要確認候補はありません</td></tr>';
  }
  updatePendingConceptReviewUi();
}

function setPendingConceptBusy(busy) {
  state.pendingConceptBusy = busy;
  document.querySelectorAll('[data-pending-concept-action]').forEach(button => {
    button.disabled = busy;
  });
  updatePendingConceptReviewUi();
}

export function togglePendingConceptSelection(conceptId, checked) {
  if (checked) state.selectedPendingConceptIds.add(String(conceptId));
  else state.selectedPendingConceptIds.delete(String(conceptId));
  updatePendingConceptReviewUi();
}

export function toggleAllPendingConcepts(checked) {
  state.selectedPendingConceptIds.clear();
  if (checked) {
    for (const conceptId of pendingConceptIds()) {
      state.selectedPendingConceptIds.add(conceptId);
    }
  }
  updatePendingConceptReviewUi();
}

export async function setSelectedConceptStatus(status) {
  const conceptIds = [...state.selectedPendingConceptIds];
  if (!conceptIds.length || state.pendingConceptBusy) return;
  setPendingConceptBusy(true);
  const succeeded = [];
  const failed = [];
  try {
    for (let offset = 0; offset < conceptIds.length; offset += 10) {
      const chunk = conceptIds.slice(offset, offset + 10);
      const results = await Promise.allSettled(chunk.map(conceptId => authApiCall(
        '/ai/api/document-library/settings/search-concepts/' + encodeURIComponent(conceptId),
        {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ status })
        }
      )));
      results.forEach((result, index) => {
        if (result.status === 'fulfilled') succeeded.push(chunk[index]);
        else failed.push({ conceptId: chunk[index], reason: result.reason });
      });
    }
    if (succeeded.length) removePendingConceptRows(succeeded);
    if (failed.length) {
      showToast(
        succeeded.length + '件を更新し、' + failed.length
          + '件は更新できませんでした。未更新の候補を残しています。',
        'error'
      );
    } else {
      showToast(
        status === 'ACTIVE'
          ? succeeded.length + '件の検索候補を承認しました'
          : succeeded.length + '件の検索候補を非表示にしました',
        'success'
      );
    }
  } finally {
    setPendingConceptBusy(false);
  }
}

export async function saveConceptSettings() {
  const confidence = Number(document.getElementById('conceptConfidence').value);
  const payload = {
    enabled: document.getElementById('conceptSettingsEnabled').checked,
    auto_publish: document.getElementById('conceptSettingsAutoPublish').checked,
    auto_publish_confidence: confidence,
    min_support_sets: Number(document.getElementById('conceptMinSupport').value),
    max_concepts_per_document: Number(document.getElementById('conceptMaxPerDocument').value),
    initial_display_limit: Number(document.getElementById('conceptInitialDisplay').value),
    input_text_limit: Number(document.getElementById('conceptInputLimit').value),
    prompt_text: document.getElementById('conceptPromptText').value,
    taxonomy_revision: state.conceptSettings?.taxonomy_revision || 1
  };
  await authApiCall('/ai/api/document-library/settings/search-concepts', {
    method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload)
  });
  showToast('検索候補の抽出・公開設定を保存しました', 'success');
  await loadMetadataSettings();
}

export async function setConceptStatus(conceptId, status) {
  if (state.pendingConceptBusy) return;
  setPendingConceptBusy(true);
  try {
    await authApiCall(`/ai/api/document-library/settings/search-concepts/${encodeURIComponent(conceptId)}`, {
      method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ status })
    });
    removePendingConceptRows([conceptId]);
    showToast(status === 'ACTIVE' ? '検索候補を承認しました' : '検索候補を非表示にしました', 'success');
  } catch (error) {
    showToast(`検索候補を更新できませんでした: ${error.message}`, 'error');
  } finally {
    setPendingConceptBusy(false);
  }
}

window.metadataSettingsModule = {
  load: loadMetadataSettings,
  createGroup,
  createTag,
  editGroup,
  toggleGroup,
  editTag,
  toggleTag,
  saveRuleset,
  testRules,
  saveFolderProfile,
  createDocumentSet,
  editDocumentSet,
  toggleDocumentSet,
  saveConceptSettings,
  togglePendingConceptSelection,
  toggleAllPendingConcepts,
  setSelectedConceptStatus,
  setConceptStatus
};
