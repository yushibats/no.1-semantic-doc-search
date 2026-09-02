import { apiCall as authApiCall } from './auth.js';
import {
  formatFileSize,
  showLoading,
  hideLoading,
  showConfirmModal,
  showImageModal,
  showTextPreviewModal,
  showToast
} from './utils.js';
import { trackPipelineJob } from './pipeline.js';

const state = {
  folders: [],
  folderById: new Map(),
  tags: [],
  tagGroups: [],
  documentSets: [],
  selectedFolderId: 'folder_root',
  page: 1,
  sort: 'updated_desc',
  currentBatch: null,
  ingestItems: [],
  activeIngestBatches: [],
  dismissedActiveIngestBatchIds: new Set(),
  selectedLibraryDocumentIds: new Set(),
  classificationInProgress: false,
  classificationProgressMessage: '',
  ingestError: '',
  libraryItems: [],
  processingDocumentId: null,
  processingFilename: '',
  processingJobId: null,
  processingLatestJobId: null,
  processingSelectedJobId: null,
  processingData: null,
  processingAffectedObjectCount: 0,
  processingRetrying: false,
  processingPollTimer: null,
  processingRequestId: 0,
  expandedPageDocuments: new Set(),
  pageImagesByDocument: new Map()
};

const escapeHtml = value => String(value ?? '')
  .replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;')
  .replaceAll('"', '&quot;').replaceAll("'", '&#039;');

function flattenFolders(nodes, depth = 0, result = []) {
  for (const node of nodes || []) {
    result.push({ ...node, uiDepth: depth });
    flattenFolders(node.children, depth + 1, result);
  }
  return result;
}

function folderOptions(selected = '', includeRoot = true) {
  return flattenFolders(state.folders)
    .filter(folder => includeRoot || folder.folder_id !== 'folder_root')
    .map(folder => `<option value="${escapeHtml(folder.folder_id)}" ${folder.folder_id === selected ? 'selected' : ''}>${'　'.repeat(folder.uiDepth)}${escapeHtml(folder.name)}</option>`)
    .join('');
}

function documentSetOptions(selected = '') {
  return '<option value="">未割当</option>' + state.documentSets
    .filter(item => item.status === 'ACTIVE')
    .map(item => `<option value="${escapeHtml(item.document_set_id)}" ${item.document_set_id === selected ? 'selected' : ''}>${escapeHtml(item.label)}</option>`)
    .join('');
}

function customerCandidate(item) {
  return item.candidates?.find(candidate => candidate.field_kind === 'CUSTOMER');
}

function dateCandidate(item) {
  return item.candidates?.find(candidate => candidate.field_kind === 'DATE');
}

function selectedTagIds(item) {
  const reviewed = item.review?.tag_ids;
  if (Array.isArray(reviewed)) return new Set(reviewed);
  return new Set((item.candidates || [])
    .filter(candidate => candidate.field_kind === 'TAG' && candidate.confirmed && !candidate.ambiguous)
    .map(candidate => candidate.target_key));
}

function candidateTagIds(item) {
  return new Set((item.candidates || [])
    .filter(candidate => candidate.field_kind === 'TAG')
    .map(candidate => candidate.target_key));
}

function reviewCustomer(item) {
  if (Object.hasOwn(item.review || {}, 'customer_name_raw')) {
    return item.review.customer_name_raw || '';
  }
  return customerCandidate(item)?.value_raw || '';
}

function reviewDate(item) {
  if (Object.hasOwn(item.review || {}, 'document_year')) {
    return {
      year: item.review.document_year || '',
      month: item.review.document_month || ''
    };
  }
  const value = dateCandidate(item)?.value_raw || '';
  const match = /^(\d{4})(?:-(\d{2}))?/.exec(value);
  return match
    ? { year: Number(match[1]), month: match[2] ? Number(match[2]) : '' }
    : { year: '', month: '' };
}

function captureReviewValues() {
  const values = new Map();
  document.querySelectorAll('[data-ingest-item]').forEach(row => {
    const year = Number(row.querySelector('[data-review-year]')?.value) || null;
    const month = Number(row.querySelector('[data-review-month]')?.value) || null;
    values.set(row.dataset.ingestItem, {
      folder_id: row.querySelector('[data-review-folder]')?.value || null,
      document_set_id: row.querySelector('[data-review-document-set]')?.value || null,
      customer_name_raw: row.querySelector('[data-review-customer]')?.value.trim() || '',
      document_year: year,
      document_month: month,
      date_precision: year ? (month ? 'YEAR_MONTH' : 'YEAR') : 'UNKNOWN',
      tag_ids: [...row.querySelectorAll('.ingest-tag-cell input:checked')].map(input => input.value)
    });
  });
  return values;
}

function applyCapturedReviewValues(values) {
  for (const item of state.ingestItems) {
    const review = values.get(String(item.item_id));
    if (review) item.review = { ...(item.review || {}), ...review };
  }
}

function adjacentReviewControls(field, label, index, total) {
  return `<div class="ingest-adjacent-copy" aria-label="${escapeHtml(label)}の隣接行コピー">
    <button type="button" data-copy-field="${escapeHtml(field)}" data-copy-direction="-1" ${index === 0 ? 'disabled' : ''} onclick="window.documentLibraryModule.copyAdjacentReviewValue(this,'${escapeHtml(field)}',-1)" title="上のファイルの${escapeHtml(label)}をコピー"><i class="fas fa-arrow-up"></i> 上からコピー</button>
    <button type="button" data-copy-field="${escapeHtml(field)}" data-copy-direction="1" ${index === total - 1 ? 'disabled' : ''} onclick="window.documentLibraryModule.copyAdjacentReviewValue(this,'${escapeHtml(field)}',1)" title="下のファイルの${escapeHtml(label)}をコピー"><i class="fas fa-arrow-down"></i> 下からコピー</button>
  </div>`;
}

export function copyAdjacentReviewValue(trigger, field, direction) {
  const targetRow = trigger?.closest?.('[data-ingest-item]');
  if (!targetRow || !['customer', 'date'].includes(field) || ![-1, 1].includes(Number(direction))) return;
  const rows = [...document.querySelectorAll('[data-ingest-item]')];
  const targetIndex = rows.indexOf(targetRow);
  const sourceRow = rows[targetIndex + Number(direction)];
  if (!sourceRow) return;

  const selectors = field === 'customer'
    ? ['[data-review-customer]']
    : ['[data-review-year]', '[data-review-month]'];
  for (const selector of selectors) {
    const source = sourceRow.querySelector(selector);
    const target = targetRow.querySelector(selector);
    if (!source || !target) continue;
    target.value = source.value;
    const InputEvent = target.ownerDocument.defaultView.Event;
    target.dispatchEvent(new InputEvent('input', { bubbles: true }));
    target.dispatchEvent(new InputEvent('change', { bubbles: true }));
  }

  const item = state.ingestItems.find(candidate => String(candidate.item_id) === targetRow.dataset.ingestItem);
  if (item) {
    const year = Number(targetRow.querySelector('[data-review-year]')?.value) || null;
    const month = Number(targetRow.querySelector('[data-review-month]')?.value) || null;
    item.review = {
      ...(item.review || {}),
      customer_name_raw: targetRow.querySelector('[data-review-customer]')?.value.trim() || '',
      document_year: year,
      document_month: month,
      date_precision: year ? (month ? 'YEAR_MONTH' : 'YEAR') : 'UNKNOWN'
    };
  }

  const editor = trigger.closest('.ingest-review-value');
  editor?.classList.add('copied');
  window.setTimeout(() => editor?.classList.remove('copied'), 700);
}

function setReviewProgress(message) {
  const progress = document.getElementById('ingestReviewProgress');
  if (progress) progress.textContent = message;
}

async function confirmCustomerNameVariants(values) {
  const uniqueValues = [...new Set(values.map(value => value.trim()).filter(Boolean))];
  if (!uniqueValues.length) return true;
  const results = await Promise.all(uniqueValues.map(value => {
    const params = new URLSearchParams({ value });
    return authApiCall(`/ai/api/document-library/customer-name-normalize?${params}`);
  }));
  const warnings = results.flatMap((result, index) => {
    if (!result.similarity_warning) return [];
    return [`${uniqueValues[index]} → 既存候補: ${(result.similar_names || []).join('、')}`];
  });
  if (!warnings.length) return true;
  return window.confirm(`顧客名に近い既存表記があります。新しい表記のまま保存しますか？\n\n${warnings.join('\n')}`);
}

function itemNeedsReview(item) {
  const candidates = item.candidates || [];
  const selected = selectedTagIds(item);
  const hasKind = state.tags.some(tag => tag.group_code === 'document_kind' && selected.has(tag.tag_id));
  return !hasKind || candidates.some(candidate => candidate.ambiguous || (!candidate.confirmed && candidate.source === 'LLM'));
}

async function loadMasters() {
  const [folders, tagGroups, tags, documentSets] = await Promise.all([
    authApiCall('/ai/api/document-library/folders'),
    authApiCall('/ai/api/document-library/settings/tag-groups'),
    authApiCall('/ai/api/document-library/settings/tags'),
    authApiCall('/ai/api/document-library/document-sets')
  ]);
  state.folders = folders || [];
  state.folderById = new Map(flattenFolders(state.folders).map(item => [item.folder_id, item]));
  state.tagGroups = tagGroups || [];
  state.tags = tags || [];
  state.documentSets = documentSets || [];
  renderFolderControls();
}

function renderFolderControls() {
  const upload = document.getElementById('uploadTargetFolder');
  if (upload) {
    const current = upload.value || 'folder_unclassified';
    upload.innerHTML = folderOptions(current, false);
    if ([...upload.options].some(option => option.value === current)) upload.value = current;
  }
  const tree = document.getElementById('documentFolderTree');
  if (!tree) return;
  const rows = flattenFolders(state.folders).map(folder => {
    const active = folder.folder_id === state.selectedFolderId;
    const controls = folder.is_system ? '' : `<span class="document-folder-actions">
      <button type="button" title="名前変更" onclick="event.stopPropagation(); window.documentLibraryModule.renameFolder('${escapeHtml(folder.folder_id)}')"><i class="fas fa-pen"></i></button>
      <button type="button" title="削除" onclick="event.stopPropagation(); window.documentLibraryModule.deleteFolder('${escapeHtml(folder.folder_id)}')"><i class="fas fa-trash"></i></button>
    </span>`;
    return `<div class="document-folder-row ${active ? 'active' : ''}" style="padding-left:${folder.uiDepth * 18}px">
      <button type="button" class="document-folder-select" onclick="window.documentLibraryModule.selectFolder('${escapeHtml(folder.folder_id)}')">
        <span><i class="fas ${folder.folder_id === 'folder_root' ? 'fa-folder-tree' : 'fa-folder'}"></i> ${escapeHtml(folder.name)}</span>
        <span class="document-folder-count">${folder.descendant_document_count ?? folder.document_count ?? 0}</span>
      </button>${controls}
    </div>`;
  }).join('');
  tree.innerHTML = rows || '<div class="empty-state">フォルダがありません</div>';
}

export async function loadDocumentLibrary({ notification = false } = {}) {
  try {
    await loadMasters();
    await refresh();
    if (!state.currentBatch) await loadActiveIngestBatches();
    if (notification) showToast('文書一覧を更新しました', 'success');
  } catch (error) {
    const target = document.getElementById('documentsList');
    if (target) target.innerHTML = `<div class="retrieval-message error">${escapeHtml(error.message)}</div>`;
    if (notification) showToast(`文書一覧の更新に失敗しました: ${error.message}`, 'error');
  }
}

function renderActiveIngestBatches() {
  const root = document.getElementById('ingestReviewRoot');
  if (!root || state.currentBatch) return;
  const visibleBatches = state.activeIngestBatches.filter(
    batch => !state.dismissedActiveIngestBatchIds.has(String(batch.batch_id))
  );
  if (!visibleBatches.length) {
    root.style.display = 'none';
    root.innerHTML = '';
    return;
  }
  const cards = visibleBatches.map(batch => {
    const total = Number(batch.total_items || 0);
    const completed = Number(batch.analysis_completed_items || 0);
    const pending = Number(batch.analysis_pending_items || 0);
    const percent = total ? Math.min(100, Math.round((completed / total) * 100)) : 0;
    const discardNote = batch.discardable
      ? '一時ObjectとDRAFTレコードだけを削除できます。'
      : '登録処理が開始されているため破棄できません。確認を再開してください。';
    return `<article class="active-ingest-card" data-active-batch="${escapeHtml(batch.batch_id)}">
      <div class="active-ingest-main">
        <div class="active-ingest-heading"><strong>${escapeHtml(batch.target_folder_name || batch.target_folder_id)}</strong><span>${total}件</span></div>
        <div class="active-ingest-progress" aria-label="解析進捗 ${completed}/${total}"><span style="width:${percent}%"></span></div>
        <p>先行解析 ${completed}/${total}${pending ? `（残り${pending}件）` : '（完了）'}・更新 ${escapeHtml(formatProcessingTime(batch.updated_at))}</p>
        <small>${escapeHtml(discardNote)}</small>
      </div>
      <div class="active-ingest-actions">
        <button type="button" class="apex-button px-3 py-2" onclick="window.documentLibraryModule.resumeActiveIngestBatch('${escapeHtml(batch.batch_id)}')"><i class="fas fa-play"></i> 確認を再開</button>
        <button type="button" class="apex-button-secondary px-3 py-2" ${batch.discardable ? '' : 'disabled'} onclick="window.documentLibraryModule.discardActiveIngestBatch('${escapeHtml(batch.batch_id)}')"><i class="fas fa-trash"></i> 破棄</button>
        <button type="button" class="apex-button-secondary px-3 py-2" onclick="window.documentLibraryModule.dismissActiveIngestBatch('${escapeHtml(batch.batch_id)}')">後で</button>
      </div>
    </article>`;
  }).join('');
  root.style.display = 'block';
  root.innerHTML = `<div class="apex-region-header active-ingest-header"><span><i class="fas fa-clock-rotate-left"></i> 未完了のアップロードがあります</span></div>
    <div class="active-ingest-list">${cards}</div>`;
}

export async function loadActiveIngestBatches() {
  try {
    state.activeIngestBatches = await authApiCall('/ai/api/document-library/ingest/active-batches') || [];
    renderActiveIngestBatches();
    return state.activeIngestBatches;
  } catch (error) {
    state.activeIngestBatches = [];
    renderActiveIngestBatches();
    showToast(`未完了のアップロードを確認できませんでした: ${error.message}`, 'warning');
    return [];
  }
}

export function dismissActiveIngestBatch(batchId) {
  state.dismissedActiveIngestBatchIds.add(String(batchId));
  renderActiveIngestBatches();
}

export async function resumeActiveIngestBatch(batchId) {
  showLoading('未完了のアップロードを読み込んでいます...');
  let loaded = false;
  try {
    const data = await authApiCall(`/ai/api/document-library/ingest/batches/${encodeURIComponent(batchId)}`);
    state.currentBatch = data.batch;
    state.ingestItems = data.items || [];
    state.dismissedActiveIngestBatchIds.delete(String(batchId));
    state.ingestError = '';
    state.classificationProgressMessage = '保存済みの確認内容を復元しました。';
    renderIngestReview();
    loaded = true;
  } catch (error) {
    showToast(`確認を再開できませんでした: ${error.message}`, 'error');
  } finally {
    hideLoading();
  }
  if (loaded) await classifyAllDrafts({ onlyPending: true });
}

export async function discardActiveIngestBatch(batchId) {
  const batch = state.activeIngestBatches.find(item => String(item.batch_id) === String(batchId));
  if (!batch?.discardable) {
    showToast('登録処理が開始されているため破棄できません。確認を再開してください。', 'warning');
    return;
  }
  const description = `${batch.target_folder_name || batch.target_folder_id} の${batch.total_items || 0}件`;
  if (!window.confirm(`${description}の一時ObjectとDRAFTレコードを削除します。復元できません。続行しますか？`)) return;
  showLoading('未完了のアップロードを破棄しています...');
  try {
    const result = await authApiCall(`/ai/api/document-library/ingest/batches/${encodeURIComponent(batchId)}`, {
      method: 'DELETE', timeout: 120000
    });
    showToast(`${result.deleted_objects || 0}件の一時Objectを削除し、ドラフトを破棄しました`, 'success');
    state.dismissedActiveIngestBatchIds.delete(String(batchId));
    await loadMasters();
    await refresh();
    await loadActiveIngestBatches();
  } catch (error) {
    showToast(`ドラフトを破棄できませんでした: ${error.message}`, 'error');
  } finally {
    hideLoading();
  }
}

export async function refresh() {
  const target = document.getElementById('documentsList');
  if (!target) return;
  target.innerHTML = '<div class="retrieval-message">文書を読み込んでいます...</div>';
  const includeDescendants = document.getElementById('libraryIncludeDescendants')?.checked ?? true;
  const query = document.getElementById('libraryQuery')?.value.trim() || '';
  const params = new URLSearchParams({
    page: String(state.page),
    page_size: '20',
    include_descendants: String(includeDescendants),
    sort: state.sort
  });
  if (state.selectedFolderId) params.set('folder_id', state.selectedFolderId);
  if (query) params.set('query', query);
  const data = await authApiCall(`/ai/api/document-library/documents?${params}`);
  state.libraryItems = data.items || [];
  const visibleIds = new Set(
    state.libraryItems.map(item => String(item.document_id))
  );
  state.selectedLibraryDocumentIds = new Set(
    [...state.selectedLibraryDocumentIds].filter(id => visibleIds.has(id))
  );
  renderLibrary(data);
}

function metadataChips(metadata) {
  const values = [];
  if (metadata.document_set_label) values.push(`案件: ${metadata.document_set_label}`);
  if (metadata.customer_name_raw) values.push(`顧客: ${metadata.customer_name_raw}`);
  if (metadata.document_year) {
    values.push(metadata.document_month
      ? `${metadata.document_year}年${String(metadata.document_month).padStart(2, '0')}月`
      : `${metadata.document_year}年`);
  }
  for (const tag of metadata.tags || []) values.push(tag.name);
  return values.map(value => `<span class="metadata-chip">${escapeHtml(value)}</span>`).join('');
}

const DOCUMENT_STATUS_LABELS = {
  INDEXED: '索引済み',
  READY: '索引済み',
  UNPROCESSED: '索引待ち',
  PROCESSING: '処理中',
  UPDATE_AVAILABLE: '更新あり',
  UPDATE_FAILED: '更新失敗',
  FAILED: '失敗',
  CANCELLED: 'キャンセル済み'
};

const JOB_STATUS_LABELS = {
  QUEUED: '待機中',
  RUNNING: '実行中',
  SUCCEEDED: '完了',
  PARTIAL_FAILED: '一部失敗',
  FAILED: '失敗',
  CANCELLED: 'キャンセル済み'
};

const STEP_STATUS_LABELS = {
  QUEUED: '待機中',
  RUNNING: '実行中',
  SUCCEEDED: '完了',
  REUSED: '完了（再利用）',
  FAILED: '失敗',
  BLOCKED: '前工程待ち',
  CANCELLED: 'キャンセル済み',
  SKIPPED: '対象外'
};

export function pipelineStatusValue(item) {
  return item.processing?.document_status || item.status || 'UNPROCESSED';
}

export function pipelineLabel(item) {
  const value = pipelineStatusValue(item);
  return DOCUMENT_STATUS_LABELS[value] || value;
}

function pipelineStatusTone(value) {
  if (['FAILED', 'UPDATE_FAILED'].includes(value)) return 'failed';
  if (value === 'CANCELLED') return 'cancelled';
  if (['INDEXED', 'READY'].includes(value)) return 'succeeded';
  if (value === 'PROCESSING') return 'running';
  return 'waiting';
}

export function processingStepLabel(step) {
  const component = String(step.component_key || '');
  const suffix = component.includes(':') ? component.split(':').slice(1).join(':') : '';
  const labels = {
    RENDER: 'ページ画像生成',
    NATIVE_PARSE: 'ネイティブテキスト抽出',
    MINERU_PARSE: 'MinerU解析',
    OCR: 'OCR',
    NORMALIZE: '正規化・チャンク化',
    VLM: suffix ? `VLM抽出（プロファイル ${suffix}）` : 'VLM抽出',
    EMBED: suffix ? `埋め込み（${suffix}）` : '埋め込み',
    PUBLISH: '索引公開',
    CONCEPT: 'AI検索候補抽出'
  };
  return labels[step.kind] || component || step.kind || '不明な工程';
}

function stepStatusIcon(status) {
  return {
    SUCCEEDED: 'fa-check-circle', REUSED: 'fa-recycle', RUNNING: 'fa-spinner fa-spin',
    FAILED: 'fa-triangle-exclamation', BLOCKED: 'fa-ban', QUEUED: 'fa-clock',
    CANCELLED: 'fa-circle-xmark', SKIPPED: 'fa-forward'
  }[status] || 'fa-circle';
}

function stepStatusTone(status) {
  if (status === 'RUNNING') return 'running';
  if (['SUCCEEDED', 'REUSED'].includes(status)) return 'succeeded';
  if (status === 'FAILED') return 'failed';
  if (status === 'BLOCKED') return 'blocked';
  if (status === 'CANCELLED') return 'cancelled';
  return 'waiting';
}

function stepExplanation(step) {
  if (step.not_planned) {
    return 'このJobの実行対象に含まれていません（Job作成時に無効、設定不足、または対象条件外）';
  }
  if (step.status === 'RUNNING') {
    return step.progress_total > 0
      ? `${step.progress_current}/${step.progress_total} を処理しています`
      : '現在実行しています';
  }
  return {
    SUCCEEDED: '正常に実行できました',
    REUSED: '以前の正常な処理結果を再利用しました',
    FAILED: '実行できませんでした',
    BLOCKED: '前工程が失敗または未完了のため実行できません',
    QUEUED: '前工程の完了を待っています',
    CANCELLED: '実行されませんでした',
    SKIPPED: '設定または条件により実行対象外です'
  }[step.status] || '状態を確認しています';
}

function processingSummary(data, job = data.job) {
  const steps = job?.steps || [];
  const failed = steps.filter(step => step.status === 'FAILED');
  const running = steps.filter(step => step.status === 'RUNNING');
  const blocked = steps.filter(step => step.status === 'BLOCKED');
  const queued = steps.filter(step => step.status === 'QUEUED');
  if (!job) return { tone: 'waiting', title: '索引Jobはまだ作成されていません', detail: '正式登録後に索引処理が開始されます。' };
  if (failed.length) {
    return {
      tone: 'failed',
      title: `${failed.map(processingStepLabel).join('、')}で失敗しました`,
      detail: blocked.length
        ? `失敗した工程に依存する${blocked.length}工程は実行できていません。接続先を復旧した後、Jobの再試行が必要です。`
        : '接続先や設定を確認した後、Jobを再試行してください。'
    };
  }
  if (running.length) return { tone: 'running', title: `${running.map(processingStepLabel).join('、')}を実行中です`, detail: '完了すると次の工程へ自動的に進みます。' };
  if (queued.length) return { tone: 'waiting', title: '索引処理の開始または前工程の完了を待っています', detail: `${queued.length}工程が待機中です。` };
  if (job.status === 'SUCCEEDED') {
    return job.is_additional
      ? { tone: 'succeeded', title: '追加Jobが完了しました', detail: '対象工程の再実行が完了しました。元の一連の処理は「全体工程」タブで確認できます。' }
      : { tone: 'succeeded', title: 'すべての索引処理が完了しました', detail: '検索で利用できる索引が公開されています。' };
  }
  if (job.status === 'CANCELLED') return { tone: 'cancelled', title: '索引処理はキャンセルされました', detail: '未実行の工程は検索へ反映されていません。' };
  return { tone: 'waiting', title: JOB_STATUS_LABELS[job.status] || job.status, detail: '工程別の状態を確認してください。' };
}

function formatProcessingTime(value) {
  if (!value) return '—';
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? String(value) : parsed.toLocaleString('ja-JP');
}

function pageImageContentUrl(documentId, releaseId, artifactId) {
  const token = localStorage.getItem('loginToken');
  const baseUrl = `/ai/api/documents/${encodeURIComponent(documentId)}/releases/${encodeURIComponent(releaseId)}/page-images/${encodeURIComponent(artifactId)}/content`;
  return token ? `${baseUrl}?token=${encodeURIComponent(token)}` : baseUrl;
}

function pageTextLabel(item) {
  const component = String(item.component_key || '');
  const suffix = component.includes(':') ? component.split(':').slice(1).join(':') : '';
  const componentLabels = {
    native: 'ネイティブ抽出',
    mineru: 'MinerU解析',
    ocr: 'OCR',
    normalize: '正規化',
    render: 'ページ画像生成'
  };
  const artifactLabels = {
    PAGE_TEXT: 'ページテキスト',
    NATIVE_TEXT: '抽出テキスト',
    MINERU_TEXT: 'MinerUテキスト',
    OCR_TEXT: 'OCRテキスト',
    VLM_TEXT: 'VLMテキスト'
  };
  const componentLabel = component.startsWith('vlm:')
    ? `VLMプロファイル ${suffix}`
    : (componentLabels[component] || component || '生成結果');
  return `${componentLabel}・${artifactLabels[item.artifact_kind] || item.artifact_kind}`;
}

function renderDocumentPagesPanel(documentId) {
  const panel = document.getElementById(`document-pages-panel-${documentId}`);
  if (!panel) return;
  const pageState = state.pageImagesByDocument.get(documentId);
  if (!pageState || (pageState.loading && !pageState.data)) {
    panel.innerHTML = '<div class="document-pages-state"><i class="fas fa-spinner fa-spin"></i> ページ画像を読み込んでいます...</div>';
    return;
  }
  if (pageState.error && !pageState.data) {
    panel.innerHTML = `<div class="document-pages-state error"><span>ページ画像を取得できませんでした: ${escapeHtml(pageState.error)}</span><button type="button" class="apex-button-secondary apex-button-xs" onclick="window.documentLibraryModule.loadDocumentPages('${escapeHtml(documentId)}')">再試行</button></div>`;
    return;
  }
  const data = pageState.data;
  if (!data?.items?.length) {
    panel.innerHTML = '<div class="document-pages-state">最新の処理結果にはページ画像がありません。</div>';
    return;
  }
  const releaseLabel = data.release_status === 'PUBLISHED' ? '公開済み' : 'Draft（処理途中を含む）';
  const cards = data.items.map(item => {
    const imageUrl = pageImageContentUrl(documentId, data.release_id, item.artifact_id);
    return `<article class="document-page-card">
      <button type="button" class="document-page-thumbnail" onclick="window.documentLibraryModule.previewDocumentPageImage('${escapeHtml(documentId)}','${escapeHtml(item.artifact_id)}')" aria-label="ページ ${Number(item.page_number)}の画像を拡大">
        <img src="${escapeHtml(imageUrl)}" alt="ページ ${Number(item.page_number)}" loading="lazy"><span hidden><i class="fas fa-image"></i></span>
      </button>
      <div class="document-page-card-body"><strong>ページ ${Number(item.page_number)}</strong><small>${item.size == null ? '' : escapeHtml(formatFileSize(item.size))}</small>
        <button type="button" class="apex-button-secondary apex-button-xs" onclick="window.documentLibraryModule.previewDocumentPageTexts('${escapeHtml(documentId)}',${Number(item.page_number)})"><i class="fas fa-file-lines"></i> 生成テキスト</button>
      </div>
    </article>`;
  }).join('');
  const more = data.pagination?.has_next
    ? `<button type="button" class="apex-button-secondary px-3 py-2" ${pageState.loading ? 'disabled' : ''} onclick="window.documentLibraryModule.loadMoreDocumentPages('${escapeHtml(documentId)}')"><i class="fas ${pageState.loading ? 'fa-spinner fa-spin' : 'fa-plus'}"></i> さらに表示（${data.items.length} / ${Number(data.total)}ページ）</button>`
    : '';
  panel.innerHTML = `<div class="document-pages-header"><span><i class="fas fa-images"></i> ${escapeHtml(releaseLabel)}・全${Number(data.total)}ページ</span><small>画像をクリックすると拡大できます</small></div><div class="document-page-grid">${cards}</div>${pageState.error ? `<div class="document-pages-state error">追加取得に失敗しました: ${escapeHtml(pageState.error)}</div>` : ''}<div class="document-pages-more">${more}</div>`;
}

export async function loadDocumentPages(documentId, page = 1, append = false) {
  const previous = state.pageImagesByDocument.get(documentId);
  state.pageImagesByDocument.set(documentId, { ...previous, loading: true, error: null });
  renderDocumentPagesPanel(documentId);
  try {
    const data = await authApiCall(`/ai/api/documents/${encodeURIComponent(documentId)}/page-images?release=latest&page=${Number(page)}&page_size=50`);
    if (append && previous?.data) data.items = [...previous.data.items, ...data.items];
    state.pageImagesByDocument.set(documentId, { loading: false, error: null, data });
    const pageBadge = document.getElementById('documentsPageImageCountBadge');
    if (pageBadge) pageBadge.textContent = `ページ画像: 選択文書 ${Number(data.total)}件`;
  } catch (error) {
    if (error.status === 404 && !append) {
      state.pageImagesByDocument.set(documentId, {
        loading: false,
        error: null,
        data: { items: [], total: 0, pagination: { current_page: 1, total_pages: 1, has_next: false } }
      });
    } else {
      state.pageImagesByDocument.set(documentId, { loading: false, error: error.message, data: previous?.data || null });
    }
  }
  renderDocumentPagesPanel(documentId);
}

export async function toggleDocumentPages(documentId) {
  const row = document.getElementById(`document-pages-row-${documentId}`);
  const button = document.querySelector(`[data-page-toggle-document="${CSS.escape(documentId)}"]`);
  if (!row || !button) return;
  const opening = row.hidden;
  row.hidden = !opening;
  button.setAttribute('aria-expanded', String(opening));
  button.querySelector('i')?.classList.toggle('fa-chevron-right', !opening);
  button.querySelector('i')?.classList.toggle('fa-chevron-down', opening);
  if (opening) {
    state.expandedPageDocuments.add(documentId);
    if (state.pageImagesByDocument.has(documentId)) renderDocumentPagesPanel(documentId);
    else await loadDocumentPages(documentId);
  } else {
    state.expandedPageDocuments.delete(documentId);
  }
}

export async function loadMoreDocumentPages(documentId) {
  const pageState = state.pageImagesByDocument.get(documentId);
  if (!pageState?.data?.pagination?.has_next || pageState.loading) return;
  await loadDocumentPages(documentId, Number(pageState.data.pagination.current_page || 1) + 1, true);
}

export function previewDocumentPageImage(documentId, artifactId) {
  const data = state.pageImagesByDocument.get(documentId)?.data;
  const items = data?.items || [];
  const index = items.findIndex(item => item.artifact_id === artifactId);
  if (index < 0) {
    showToast('ページ画像が更新されています。ページ一覧を再取得してください', 'warning');
    return;
  }
  const urls = items.map(item => pageImageContentUrl(documentId, data.release_id, item.artifact_id));
  const titles = items.map(item => `ページ ${Number(item.page_number)}`);
  showImageModal(urls[index], titles[index], urls, index, titles);
}

export async function previewDocumentPageTexts(documentId, pageNumber) {
  try {
    showLoading('生成テキストを取得しています...');
    const data = await authApiCall(`/ai/api/documents/${encodeURIComponent(documentId)}/page-texts?release=latest&page_number=${Number(pageNumber)}`);
    const order = ['PAGE_TEXT', 'NATIVE_TEXT', 'MINERU_TEXT', 'OCR_TEXT', 'VLM_TEXT'];
    const items = (data.items || []).slice().sort((left, right) => order.indexOf(left.artifact_kind) - order.indexOf(right.artifact_kind));
    if (!items.length) {
      showToast('このページにはまだ生成テキストがありません', 'info');
      return;
    }
    const sections = items.map(item => {
      let text = item.raw_text || '';
      if (item.payload_json != null) text += `${text ? '\n\n' : ''}--- 構造化出力 (JSON) ---\n${JSON.stringify(item.payload_json, null, 2)}`;
      return {
        label: `${pageTextLabel(item)}${item.stage_status === 'STALE' ? '（要更新）' : ''}`,
        text,
        meta: [item.artifact_kind, item.created_at ? `生成日時: ${item.created_at}` : null].filter(Boolean).join('　')
      };
    });
    const totalPages = state.pageImagesByDocument.get(documentId)?.data?.total || null;
    showTextPreviewModal(`ページ ${Number(pageNumber)}${totalPages ? ` / ${Number(totalPages)}` : ''} の生成テキスト`, sections, {
      onPrev: Number(pageNumber) > 1 ? () => previewDocumentPageTexts(documentId, Number(pageNumber) - 1) : null,
      onNext: (!totalPages || Number(pageNumber) < Number(totalPages)) ? () => previewDocumentPageTexts(documentId, Number(pageNumber) + 1) : null
    });
  } catch (error) {
    if (error.status === 404) showToast('このページにはまだ生成テキストがありません', 'info');
    else showToast(`生成テキストを取得できませんでした: ${error.message}`, 'error');
  } finally {
    hideLoading();
  }
}

function ensureProcessingDialog() {
  let dialog = document.getElementById('documentProcessingDialog');
  if (dialog) return dialog;
  dialog = document.createElement('dialog');
  dialog.id = 'documentProcessingDialog';
  dialog.className = 'document-processing-dialog';
  dialog.setAttribute('aria-labelledby', 'documentProcessingTitle');
  dialog.innerHTML = `<div class="document-processing-shell">
    <header><div><h2 id="documentProcessingTitle">索引処理の詳細</h2><p id="documentProcessingFilename"></p></div>
      <button type="button" class="document-processing-close" aria-label="処理詳細を閉じる" onclick="window.documentLibraryModule.closeProcessingDetails()"><i class="fas fa-times"></i></button></header>
    <div id="documentProcessingBody" class="document-processing-body"></div>
    <footer><button type="button" id="documentProcessingRetry" class="apex-button px-4 py-2" onclick="window.documentLibraryModule.retryProcessingJob()" hidden><i class="fas fa-rotate-right"></i> 失敗工程から再試行</button><button type="button" class="apex-button-secondary px-3 py-2" onclick="window.documentLibraryModule.refreshProcessingDetails()"><i class="fas fa-sync-alt"></i> 状態を更新</button><button type="button" class="apex-button px-4 py-2" onclick="window.documentLibraryModule.closeProcessingDetails()">閉じる</button></footer>
  </div>`;
  dialog.addEventListener('close', stopProcessingPoll);
  dialog.addEventListener('cancel', stopProcessingPoll);
  document.body.appendChild(dialog);
  return dialog;
}

function stopProcessingPoll() {
  if (state.processingPollTimer) window.clearTimeout(state.processingPollTimer);
  state.processingPollTimer = null;
}

function renderProcessingDetails(data) {
  const body = document.getElementById('documentProcessingBody');
  if (!body) return;
  state.processingData = data;
  const history = data.job_history?.length ? data.job_history : (data.job ? [data.job] : []);
  const latestJob = history.at(-1) || null;
  if (!state.processingSelectedJobId || !history.some(job => job.job_id === state.processingSelectedJobId)) {
    state.processingSelectedJobId = latestJob?.job_id || null;
  }
  const job = history.find(value => value.job_id === state.processingSelectedJobId) || latestJob;
  const summary = processingSummary(data, job);
  const steps = job?.steps || [];
  const completed = steps.filter(step => ['SUCCEEDED', 'REUSED'].includes(step.status)).length;
  const failed = steps.filter(step => step.status === 'FAILED').length;
  const pending = steps.filter(step => ['QUEUED', 'BLOCKED'].includes(step.status)).length;
  const presentKinds = new Set(steps.map(step => step.kind));
  const notPlannedSteps = job?.is_additional ? [] : [
    { kind: 'MINERU_PARSE', component_key: 'mineru', status: 'SKIPPED', not_planned: true },
    { kind: 'OCR', component_key: 'ocr', status: 'SKIPPED', not_planned: true },
    { kind: 'VLM', component_key: 'vlm', status: 'SKIPPED', not_planned: true },
    { kind: 'EMBED', component_key: 'embedding', status: 'SKIPPED', not_planned: true },
    { kind: 'CONCEPT', component_key: 'concepts', status: 'SKIPPED', not_planned: true }
  ].filter(step => !presentKinds.has(step.kind));
  const displaySteps = [...steps, ...notPlannedSteps];
  const stepRows = displaySteps.map(step => `<article class="document-processing-step ${stepStatusTone(step.status)}">
    <div class="document-processing-step-icon"><i class="fas ${stepStatusIcon(step.status)}"></i></div>
    <div class="document-processing-step-main"><div class="document-processing-step-heading"><strong>${escapeHtml(processingStepLabel(step))}</strong><span>${escapeHtml(STEP_STATUS_LABELS[step.status] || step.status)}</span></div>
      <p>${escapeHtml(stepExplanation(step))}${step.attempt_count ? `・試行 ${Number(step.attempt_count)}回` : ''}</p>
      ${step.error_summary ? `<pre>${escapeHtml(step.error_summary)}</pre>` : ''}
    </div>
  </article>`).join('');
  state.processingJobId = job?.job_id || null;
  state.processingLatestJobId = latestJob?.job_id || null;
  state.processingAffectedObjectCount = Number(job?.affected_object_count || 0);
  const retryButton = document.getElementById('documentProcessingRetry');
  if (retryButton) {
    const failedSteps = steps.filter(step => step.status === 'FAILED');
    const hasGpuFailure = failedSteps.some(step => ['MINERU_PARSE', 'OCR', 'VLM', 'EMBED'].includes(step.kind));
    retryButton.hidden = !job || job.job_id !== latestJob?.job_id || !['FAILED', 'PARTIAL_FAILED'].includes(job.status) || !failedSteps.length;
    retryButton.disabled = state.processingRetrying;
    retryButton.innerHTML = state.processingRetrying
      ? '<i class="fas fa-spinner fa-spin"></i> 再試行を開始しています...'
      : `<i class="fas fa-rotate-right"></i> ${hasGpuFailure ? 'GPU関連工程から再試行' : '失敗工程から再試行'}`;
  }
  const tabs = history.length > 1
    ? `<nav class="document-processing-tabs" role="tablist" aria-label="索引Job履歴">${history.map(value => `<button type="button" role="tab" aria-selected="${value.job_id === job?.job_id}" class="${value.job_id === job?.job_id ? 'active' : ''}" onclick="window.documentLibraryModule.selectProcessingJob('${escapeHtml(value.job_id)}')"><span>${escapeHtml(value.tab_label || (value.is_additional ? '追加Job' : '全体工程'))}</span><small>${escapeHtml(JOB_STATUS_LABELS[value.status] || value.status)}</small></button>`).join('')}</nav>`
    : '';
  const contextNote = job?.is_additional
    ? '<p class="document-processing-job-note"><i class="fas fa-code-branch"></i> このタブは追加Jobで実行した工程だけを表示しています。</p>'
    : (history.length > 1 ? '<p class="document-processing-job-note"><i class="fas fa-list-check"></i> このタブは最初に計画された一連の工程を表示しています。</p>' : '');
  body.innerHTML = `${tabs}${contextNote}<section class="document-processing-summary ${summary.tone}" role="status"><strong>${escapeHtml(summary.title)}</strong><p>${escapeHtml(summary.detail)}</p></section>
    ${job ? `<dl class="document-processing-meta"><div><dt>Job状態</dt><dd>${escapeHtml(JOB_STATUS_LABELS[job.status] || job.status)}</dd></div><div><dt>この文書</dt><dd>完了 ${completed}・失敗 ${failed}・待機 ${pending}</dd></div><div><dt>対象文書数</dt><dd>${Number(job.affected_object_count || 1)}件</dd></div><div><dt>最終更新</dt><dd>${escapeHtml(formatProcessingTime(job.updated_at))}</dd></div><div class="wide"><dt>Job ID</dt><dd><code>${escapeHtml(job.job_id)}</code></dd></div></dl>` : ''}
    <section class="document-processing-steps"><h3>工程別の状態</h3>${stepRows || '<div class="empty-state"><p>工程情報はまだありません</p></div>'}</section>`;
}

async function loadProcessingDetails({ showLoadingState = true } = {}) {
  const documentId = state.processingDocumentId;
  if (!documentId) return;
  const requestId = ++state.processingRequestId;
  const body = document.getElementById('documentProcessingBody');
  if (body && showLoadingState) body.innerHTML = '<div class="document-processing-loading"><i class="fas fa-spinner fa-spin"></i> 処理状態を確認しています...</div>';
  stopProcessingPoll();
  try {
    const data = await authApiCall(`/ai/api/document-library/documents/${encodeURIComponent(documentId)}/processing`);
    if (requestId !== state.processingRequestId || documentId !== state.processingDocumentId) return;
    renderProcessingDetails(data);
    const item = state.libraryItems.find(value => value.document_id === documentId);
    if (item) item.processing = data;
    const statusButton = document.querySelector(`[data-processing-document="${CSS.escape(documentId)}"]`);
    if (statusButton) {
      const value = data.document_status || 'UNPROCESSED';
      statusButton.textContent = DOCUMENT_STATUS_LABELS[value] || value;
      statusButton.className = `pipeline-status-chip pipeline-status-button ${pipelineStatusTone(value)}`;
    }
    const activeJobs = data.job_history?.length ? data.job_history : (data.job ? [data.job] : []);
    if (activeJobs.some(job => ['QUEUED', 'RUNNING'].includes(job.status))) {
      state.processingPollTimer = window.setTimeout(() => loadProcessingDetails({ showLoadingState: false }), 3000);
    }
  } catch (error) {
    if (requestId !== state.processingRequestId || !body) return;
    body.innerHTML = `<div class="retrieval-message error">処理状態を取得できませんでした: ${escapeHtml(error.message)}</div>`;
  }
}

export function selectProcessingJob(jobId) {
  if (!state.processingData) return;
  state.processingSelectedJobId = jobId;
  renderProcessingDetails(state.processingData);
}

export async function showProcessingDetails(documentId, filename = '') {
  if (!filename) {
    filename = state.libraryItems.find(item => item.document_id === documentId)?.file_name || '';
  }
  state.processingDocumentId = documentId;
  state.processingFilename = filename;
  state.processingSelectedJobId = null;
  state.processingData = null;
  const dialog = ensureProcessingDialog();
  document.getElementById('documentProcessingFilename').textContent = filename;
  if (!dialog.open) {
    if (typeof dialog.showModal === 'function') dialog.showModal();
    else dialog.setAttribute('open', '');
  }
  await loadProcessingDetails();
}

export function closeProcessingDetails() {
  stopProcessingPoll();
  state.processingDocumentId = null;
  state.processingJobId = null;
  state.processingLatestJobId = null;
  state.processingSelectedJobId = null;
  state.processingData = null;
  state.processingAffectedObjectCount = 0;
  state.processingRetrying = false;
  state.processingRequestId += 1;
  const dialog = document.getElementById('documentProcessingDialog');
  if (!dialog) return;
  if (typeof dialog.close === 'function' && dialog.open) dialog.close();
  else dialog.removeAttribute('open');
}

export async function refreshProcessingDetails() {
  await loadProcessingDetails();
}

function confirmProcessingRetry(documentCount) {
  const message = `${documentCount}件の文書について、GPU関連の失敗工程と、そのため停止した後続工程を新しいJobで再試行します。完了済み工程は再実行しません。GPUサービスが起動済みであることを確認してください。`;
  const dialog = document.createElement('dialog');
  if (typeof dialog.showModal !== 'function') {
    return showConfirmModal(message, '失敗工程を再試行しますか？', {
      variant: 'warning', confirmText: '再試行する'
    });
  }

  document.getElementById('processingRetryConfirmDialog')?.remove();
  dialog.id = 'processingRetryConfirmDialog';
  dialog.className = 'processing-retry-confirm-dialog';
  dialog.setAttribute('aria-labelledby', 'processingRetryConfirmTitle');
  dialog.innerHTML = `<div class="processing-retry-confirm-shell">
    <div class="processing-retry-confirm-icon"><i class="fas fa-exclamation-triangle"></i></div>
    <h2 id="processingRetryConfirmTitle">失敗工程を再試行しますか？</h2>
    <p>${escapeHtml(message)}</p>
    <footer>
      <button type="button" class="apex-button-secondary px-3 py-2" data-retry-cancel>キャンセル</button>
      <button type="button" class="apex-button px-4 py-2" data-retry-confirm><i class="fas fa-rotate-right"></i> 再試行する</button>
    </footer>
  </div>`;
  document.body.appendChild(dialog);

  return new Promise(resolve => {
    let settled = false;
    const finish = confirmed => {
      if (settled) return;
      settled = true;
      if (dialog.open && typeof dialog.close === 'function') dialog.close();
      dialog.remove();
      document.getElementById('documentProcessingRetry')?.focus();
      resolve(confirmed);
    };
    dialog.querySelector('[data-retry-confirm]').addEventListener('click', () => finish(true));
    dialog.querySelector('[data-retry-cancel]').addEventListener('click', () => finish(false));
    dialog.addEventListener('cancel', event => {
      event.preventDefault();
      finish(false);
    });
    dialog.showModal();
    dialog.querySelector('[data-retry-cancel]').focus();
  });
}

export async function retryProcessingJob() {
  const jobId = state.processingJobId;
  if (!jobId || state.processingRetrying) return;
  const documentCount = Math.max(1, state.processingAffectedObjectCount);
  const confirmed = await confirmProcessingRetry(documentCount);
  if (!confirmed) return;

  state.processingRetrying = true;
  const retryButton = document.getElementById('documentProcessingRetry');
  if (retryButton) {
    retryButton.disabled = true;
    retryButton.innerHTML = '<i class="fas fa-spinner fa-spin"></i> 再試行を開始しています...';
  }
  try {
    const result = await authApiCall(`/ai/api/pipeline/jobs/${encodeURIComponent(jobId)}/retry`, { method: 'POST' });
    state.processingJobId = result.job_id;
    state.processingLatestJobId = result.job_id;
    state.processingSelectedJobId = result.job_id;
    showToast('GPU関連の失敗工程と後続工程の再試行を開始しました', 'success');
    await loadProcessingDetails();
  } catch (error) {
    showToast(`再試行を開始できませんでした: ${error.message}`, 'error');
  } finally {
    state.processingRetrying = false;
    const currentButton = document.getElementById('documentProcessingRetry');
    if (currentButton) currentButton.disabled = false;
  }
}

function selectedLibraryItems() {
  return state.libraryItems.filter(item =>
    state.selectedLibraryDocumentIds.has(String(item.document_id))
  );
}

function updateLibraryBulkActions() {
  const selectedCount = state.selectedLibraryDocumentIds.size;
  const count = document.getElementById('librarySelectedCount');
  if (count) count.textContent = selectedCount + '件選択中';
  document.querySelectorAll('[data-library-bulk-action]').forEach(button => {
    button.disabled = selectedCount === 0;
  });
  const visibleIds = state.libraryItems.map(item => String(item.document_id));
  const selectedVisible = visibleIds.filter(id =>
    state.selectedLibraryDocumentIds.has(id)
  ).length;
  const selectAll = document.getElementById('librarySelectAll');
  if (selectAll) {
    selectAll.checked = visibleIds.length > 0 && selectedVisible === visibleIds.length;
    selectAll.indeterminate = selectedVisible > 0 && selectedVisible < visibleIds.length;
  }
  document.querySelectorAll('[data-library-document-row]').forEach(row => {
    row.classList.toggle(
      'is-selected',
      state.selectedLibraryDocumentIds.has(String(row.dataset.libraryDocumentRow))
    );
  });
}

export function toggleLibraryDocumentSelection(documentId, checked) {
  if (checked) state.selectedLibraryDocumentIds.add(String(documentId));
  else state.selectedLibraryDocumentIds.delete(String(documentId));
  updateLibraryBulkActions();
}

export function toggleAllVisibleDocuments(checked) {
  for (const item of state.libraryItems) {
    const documentId = String(item.document_id);
    if (checked) state.selectedLibraryDocumentIds.add(documentId);
    else state.selectedLibraryDocumentIds.delete(documentId);
    const input = document.querySelector(
      '[data-library-document-checkbox="' + CSS.escape(documentId) + '"]'
    );
    if (input) input.checked = checked;
  }
  updateLibraryBulkActions();
}

export async function bulkMoveSelectedDocuments() {
  const items = selectedLibraryItems();
  const folderId = document.getElementById('libraryBulkFolder')?.value || '';
  if (!items.length) {
    showToast('フォルダを変更する文書を選択してください', 'warning');
    return;
  }
  if (!folderId) {
    showToast('移動先フォルダを選択してください', 'warning');
    return;
  }
  const folder = state.folderById.get(folderId);
  const confirmed = await showConfirmModal(
    items.length + '件の文書を「' + (folder?.name || folderId) + '」へ移動します。索引の再生成は行いません。',
    'フォルダを一括変更',
    { confirmText: '変更', cancelText: 'キャンセル' }
  );
  if (!confirmed) return;
  showLoading('フォルダを変更しています...');
  try {
    await authApiCall('/ai/api/document-library/documents/bulk-metadata', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        document_ids: items.map(item => item.document_id),
        patch: { folder_id: folderId }
      }),
      timeout: 120000
    });
    state.selectedLibraryDocumentIds.clear();
    showToast(items.length + '件のフォルダを変更しました', 'success');
    await Promise.all([loadMasters(), refresh()]);
  } catch (error) {
    showToast('フォルダを変更できませんでした: ' + error.message, 'error');
  } finally {
    hideLoading();
  }
}

export async function bulkDeleteSelectedDocuments() {
  const items = selectedLibraryItems();
  if (!items.length) {
    showToast('削除する文書を選択してください', 'warning');
    return;
  }
  const names = items.slice(0, 8).map(item => '・' + item.file_name).join('\n');
  const remaining = items.length > 8 ? '\nほか' + (items.length - 8) + '件' : '';
  const confirmed = await showConfirmModal(
    items.length + '件の文書を削除します。元ファイル、ページ画像、生成テキスト、埋め込み、索引も削除され、復元できません。\n\n' + names + remaining,
    '選択した文書を削除',
    {
      variant: 'danger',
      confirmText: '完全に削除',
      cancelText: 'キャンセル'
    }
  );
  if (!confirmed) return;
  showLoading('選択した文書と派生成果物を削除しています...');
  try {
    const result = await authApiCall('/ai/api/document-library/documents', {
      method: 'DELETE',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        document_ids: items.map(item => item.document_id)
      }),
      timeout: 300000
    });
    for (const documentId of result.deleted_document_ids || []) {
      state.selectedLibraryDocumentIds.delete(String(documentId));
      state.expandedPageDocuments.delete(String(documentId));
      state.pageImagesByDocument.delete(String(documentId));
    }
    if (result.failed_count) {
      const first = result.failures?.[0];
      showToast(
        result.deleted_count + '件を削除、' + result.failed_count + '件は失敗しました' +
          (first?.error ? ': ' + first.error : ''),
        'warning'
      );
    } else if (result.cleanup_warning_count) {
      const first = result.cleanup_warnings?.[0];
      showToast(
        result.deleted_count + '件を一覧から削除しました。Object Storageの清掃に' +
          result.cleanup_warning_count + '件の警告があります' + (first?.error ? ': ' + first.error : ''),
        'warning'
      );
    } else {
      showToast(result.deleted_count + '件を削除しました', 'success');
    }
    await Promise.all([loadMasters(), refresh()]);
  } catch (error) {
    showToast('文書を削除できませんでした: ' + error.message, 'error');
  } finally {
    hideLoading();
  }
}

export async function bulkReprocessSelectedDocuments() {
  const items = selectedLibraryItems();
  if (!items.length) {
    showToast('再処理する文書を選択してください', 'warning');
    return;
  }
  const request = {
    object_names: items.map(item => item.object_name),
    mode: 'FULL',
    steps: [],
    force: true,
    include_downstream: false,
    publish_mode: 'AUTO'
  };
  try {
    const preview = await authApiCall('/ai/api/pipeline/jobs/preview', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(request),
      timeout: 120000
    });
    const warnings = (preview.warnings || []).length
      ? '\n\n注意:\n' + preview.warnings.map(value => '・' + value).join('\n')
      : '';
    const confirmed = await showConfirmModal(
      items.length + '件を、現在有効な設定で初回登録時と同じ索引処理へ通します。ページ生成、テキスト抽出、MinerU/OCR、VLM、埋め込み、AI検索候補、公開のうち、現在有効な工程を最初から実行します。\n\nユーザーが確定したフォルダ・カテゴリ・顧客・年月は保持されます。' + warnings,
      '前処理・索引をすべて再実行',
      {
        variant: 'warning',
        confirmText: '再処理を開始',
        cancelText: 'キャンセル'
      }
    );
    if (!confirmed) return;
    const idempotencyKey = window.crypto?.randomUUID?.()
      || 'library-full-' + Date.now() + '-' + Math.random().toString(16).slice(2);
    const response = await authApiCall('/ai/api/pipeline/jobs', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Idempotency-Key': idempotencyKey
      },
      body: JSON.stringify(request),
      timeout: 120000
    });
    trackPipelineJob(response);
    state.selectedLibraryDocumentIds.clear();
    showToast(items.length + '件の再処理を開始しました。処理タスクで進捗を確認できます', 'success');
    await refresh();
  } catch (error) {
    showToast('再処理を開始できませんでした: ' + error.message, 'error');
  }
}
function renderLibrary(data) {
  const target = document.getElementById('documentsList');
  const fileBadge = document.getElementById('documentsFileCountBadge');
  const statusBadge = document.getElementById('documentsStatusBadge');
  if (fileBadge) fileBadge.textContent = `ファイル: ${data.total || 0}件`;
  if (statusBadge) statusBadge.textContent = '取得済み';
  const pageImageBadge = document.getElementById('documentsPageImageCountBadge');
  if (pageImageBadge) pageImageBadge.textContent = 'ページ画像: 文書を展開';
  if (!state.libraryItems.length) {
    target.innerHTML = '<div class="empty-state"><i class="fas fa-folder-open"></i><p>この範囲に文書はありません</p></div>';
    return;
  }
  const rows = state.libraryItems.map(item => {
    const objectPath = String(item.object_name).split('/').map(encodeURIComponent).join('/');
    const expanded = state.expandedPageDocuments.has(item.document_id);
    const cachedPages = state.pageImagesByDocument.get(item.document_id)?.data;
    const pageCount = cachedPages ? `${Number(cachedPages.total)}ページ` : 'ページを表示';
    const selected = state.selectedLibraryDocumentIds.has(String(item.document_id));
    return `<tr data-library-document-row="${escapeHtml(item.document_id)}" class="${selected ? 'is-selected' : ''}">
      <td class="document-library-selection-cell"><input type="checkbox" data-library-document-checkbox="${escapeHtml(item.document_id)}" aria-label="${escapeHtml(item.file_name)}を選択" ${selected ? 'checked' : ''} onchange="window.documentLibraryModule.toggleLibraryDocumentSelection('${escapeHtml(item.document_id)}', this.checked)"></td>
      <td class="document-library-document-cell"><div class="document-library-document-line"><strong title="${escapeHtml(item.file_name)}">${escapeHtml(item.file_name)}</strong><div class="document-library-document-meta"><div class="metadata-chip-list">${metadataChips(item.metadata)}</div><button type="button" data-page-toggle-document="${escapeHtml(item.document_id)}" class="document-pages-toggle" aria-expanded="${expanded}" onclick="window.documentLibraryModule.toggleDocumentPages('${escapeHtml(item.document_id)}')"><i class="fas fa-chevron-${expanded ? 'down' : 'right'}"></i> ${pageCount}</button></div></div></td>
      <td>${escapeHtml(item.metadata.folder_name)}</td>
      <td>${escapeHtml(formatFileSize(item.file_size || 0))}</td>
      <td><button type="button" data-processing-document="${escapeHtml(item.document_id)}" class="pipeline-status-chip pipeline-status-button ${pipelineStatusTone(pipelineStatusValue(item))}" onclick="window.documentLibraryModule.showProcessingDetails('${escapeHtml(item.document_id)}')" aria-label="${escapeHtml(item.file_name)}の索引処理詳細を表示">${escapeHtml(pipelineLabel(item))}</button></td>
      <td class="document-actions">
        <button type="button" class="apex-button-secondary apex-button-xs" onclick="window.documentLibraryModule.toggleMetadataEditor('${escapeHtml(item.document_id)}')"><i class="fas fa-tags"></i> 属性</button>
        <a class="apex-button-secondary apex-button-xs" href="/ai/api/object/${encodeURIComponent(item.bucket)}/${objectPath}" target="_blank"><i class="fas fa-download"></i></a>
      </td>
    </tr>
    <tr id="document-pages-row-${escapeHtml(item.document_id)}" class="document-pages-row" ${expanded ? '' : 'hidden'}><td colspan="6"><div id="document-pages-panel-${escapeHtml(item.document_id)}" class="document-pages-panel"></div></td></tr>
    <tr id="metadata-editor-${escapeHtml(item.document_id)}" class="metadata-editor-row" hidden><td colspan="6">${metadataEditor(item)}</td></tr>`;
  }).join('');
  target.innerHTML = `<div class="document-library-bulk-toolbar" aria-label="選択した文書の一括操作">
      <strong id="librarySelectedCount">${state.selectedLibraryDocumentIds.size}件選択中</strong>
      <div class="document-library-bulk-move">
        <select id="libraryBulkFolder" class="form-input" aria-label="移動先フォルダ"><option value="">移動先フォルダ...</option>${folderOptions('', false)}</select>
        <button type="button" data-library-bulk-action class="apex-button-secondary apex-button-xs" onclick="window.documentLibraryModule.bulkMoveSelectedDocuments()"><i class="fas fa-folder-tree"></i> フォルダ変更</button>
      </div>
      <button type="button" data-library-bulk-action class="apex-button-secondary apex-button-xs" onclick="window.documentLibraryModule.bulkReprocessSelectedDocuments()"><i class="fas fa-rotate"></i> 前処理をすべて再実行</button>
      <button type="button" data-library-bulk-action class="apex-button-danger apex-button-xs" onclick="window.documentLibraryModule.bulkDeleteSelectedDocuments()"><i class="fas fa-trash"></i> 削除</button>
      <div class="document-library-bulk-sort">
        <label for="librarySortSelect"><i class="fas fa-sort-amount-down"></i> 並び順</label>
        <select id="librarySortSelect" class="form-input" aria-label="文書の並び順" onchange="window.documentLibraryModule.setLibrarySort(this.value)">
          <option value="updated_desc" ${state.sort === 'updated_desc' ? 'selected' : ''}>更新日時の新しい順</option>
          <option value="created_desc" ${state.sort === 'created_desc' ? 'selected' : ''}>登録日時の新しい順</option>
          <option value="updated_asc" ${state.sort === 'updated_asc' ? 'selected' : ''}>更新日時の古い順</option>
          <option value="filename_asc" ${state.sort === 'filename_asc' ? 'selected' : ''}>ファイル名順</option>
        </select>
      </div>
    </div>
    <div class="table-wrapper-scrollable"><table class="apex-table document-library-table"><thead><tr><th class="document-library-selection-cell"><input id="librarySelectAll" type="checkbox" aria-label="表示中の文書をすべて選択" onchange="window.documentLibraryModule.toggleAllVisibleDocuments(this.checked)"></th><th>文書</th><th>フォルダ</th><th>サイズ</th><th>索引</th><th>操作</th></tr></thead><tbody>${rows}</tbody></table></div>
    <div class="document-library-pagination"><button class="apex-button-secondary apex-button-xs" ${data.page <= 1 ? 'disabled' : ''} onclick="window.documentLibraryModule.changePage(-1)">前へ</button><span>${data.page} / ${data.total_pages}</span><button class="apex-button-secondary apex-button-xs" ${data.page >= data.total_pages ? 'disabled' : ''} onclick="window.documentLibraryModule.changePage(1)">次へ</button></div>`;
  updateLibraryBulkActions();
  for (const documentId of state.expandedPageDocuments) {
    if (document.getElementById(`document-pages-panel-${documentId}`)) {
      if (state.pageImagesByDocument.has(documentId)) renderDocumentPagesPanel(documentId);
      else loadDocumentPages(documentId);
    }
  }
}

function tagPicker(selected, candidateIds = new Set(), prefix = '') {
  const byGroup = new Map();
  for (const tag of state.tags.filter(value => value.active)) {
    if (!byGroup.has(tag.group_id)) byGroup.set(tag.group_id, []);
    byGroup.get(tag.group_id).push(tag);
  }
  return [...byGroup.entries()].map(([groupId, tags]) => {
    const group = state.tagGroups.find(value => value.group_id === groupId);
    const type = group?.selection_mode === 'SINGLE' ? 'radio' : 'checkbox';
    const name = `${prefix}-tag-${groupId}`;
    return `<fieldset class="metadata-tag-group"><legend>${escapeHtml(group?.name || '')}</legend>${tags.map(tag => `<label class="metadata-tag-option ${candidateIds.has(tag.tag_id) ? 'suggested' : ''}"><input type="${type}" name="${escapeHtml(name)}" value="${escapeHtml(tag.tag_id)}" ${selected.has(tag.tag_id) ? 'checked' : ''}> ${escapeHtml(tag.name)}</label>`).join('')}</fieldset>`;
  }).join('');
}

function metadataEditor(item) {
  const selected = new Set((item.metadata.tags || []).map(tag => tag.tag_id));
  return `<div class="metadata-editor" data-document-id="${escapeHtml(item.document_id)}" data-row-version="${item.metadata.row_version}">
    <div><label class="form-label">フォルダ</label><select data-edit-folder class="form-input">${folderOptions(item.metadata.folder_id, false)}</select></div>
    <div><label class="form-label">案件グループ</label><select data-edit-document-set class="form-input">${documentSetOptions(item.metadata.document_set_id || '')}</select><div class="metadata-settings-actions"><button type="button" class="apex-button-secondary apex-button-xs" onclick="window.documentLibraryModule.suggestDocumentSet(this,'${escapeHtml(item.document_id)}')">候補を確認</button><button type="button" class="apex-button-secondary apex-button-xs" onclick="window.documentLibraryModule.createDocumentSet(this)">新規作成</button></div><small class="form-help">顧客名だけでは自動統合しません。候補を確認して選択してください。</small></div>
    <div><label class="form-label">顧客名</label><input data-edit-customer class="form-input" value="${escapeHtml(item.metadata.customer_name_raw || '')}"></div>
    <div class="metadata-year-range"><div><label class="form-label">年</label><input data-edit-year type="number" class="form-input" value="${item.metadata.document_year || ''}"></div><div><label class="form-label">月</label><input data-edit-month type="number" min="1" max="12" class="form-input" value="${item.metadata.document_month || ''}"></div></div>
    <div class="metadata-editor-tags">${tagPicker(selected, new Set(), `edit-${item.document_id}`)}</div>
    <div class="metadata-editor-actions"><button type="button" class="apex-button px-3 py-2" onclick="window.documentLibraryModule.saveMetadata('${escapeHtml(item.document_id)}')"><i class="fas fa-save"></i> 保存</button></div>
  </div>`;
}

export function toggleMetadataEditor(documentId) {
  const row = document.getElementById(`metadata-editor-${documentId}`);
  if (row) row.hidden = !row.hidden;
}

export async function saveMetadata(documentId) {
  const editor = document.querySelector(`.metadata-editor[data-document-id="${CSS.escape(documentId)}"]`);
  if (!editor) return;
  const year = Number(editor.querySelector('[data-edit-year]').value) || null;
  const month = Number(editor.querySelector('[data-edit-month]').value) || null;
  const tagIds = [...editor.querySelectorAll('.metadata-editor-tags input:checked')].map(input => input.value);
  const customer = editor.querySelector('[data-edit-customer]').value.trim();
  if (!await confirmCustomerNameVariants([customer])) return;
  await authApiCall(`/ai/api/document-library/documents/${encodeURIComponent(documentId)}/metadata`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      folder_id: editor.querySelector('[data-edit-folder]').value,
      document_set_id: editor.querySelector('[data-edit-document-set]').value || null,
      customer_name_raw: customer,
      customer_source: 'USER',
      customer_confirmed: Boolean(customer),
      document_year: year,
      document_month: month,
      date_precision: year ? (month ? 'YEAR_MONTH' : 'YEAR') : 'UNKNOWN',
      date_source: year ? 'USER' : null,
      date_confirmed: Boolean(year),
      tag_ids: tagIds,
      expected_row_version: Number(editor.dataset.rowVersion)
    })
  });
  showToast('文書属性を保存しました。再埋込みは発生しません', 'success');
  await Promise.all([loadMasters(), refresh()]);
}

export function selectFolder(folderId) {
  state.selectedFolderId = folderId;
  state.page = 1;
  renderFolderControls();
  refresh();
}

export function changePage(delta) {
  state.page = Math.max(1, state.page + delta);
  refresh();
}

const LIBRARY_SORT_VALUES = new Set([
  'updated_desc',
  'created_desc',
  'updated_asc',
  'filename_asc'
]);

function updateLibrarySortControl() {
  const select = document.getElementById('librarySortSelect');
  if (select) select.value = state.sort;
}

export async function setLibrarySort(sort) {
  if (!LIBRARY_SORT_VALUES.has(sort) || state.sort === sort) return;
  state.sort = sort;
  state.page = 1;
  state.selectedLibraryDocumentIds.clear();
  updateLibrarySortControl();
  await refresh();
}

export async function createFolderPrompt() {
  if (!state.folders.length) await loadMasters();
  const name = window.prompt('新しいフォルダ名を入力してください');
  if (!name?.trim()) return;
  const parent = state.selectedFolderId || document.getElementById('uploadTargetFolder')?.value || 'folder_root';
  await authApiCall('/ai/api/document-library/folders', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name: name.trim(), parent_folder_id: parent })
  });
  showToast('フォルダを作成しました', 'success');
  await loadDocumentLibrary();
}

export async function renameFolder(folderId) {
  const current = state.folderById.get(folderId);
  const name = window.prompt('新しいフォルダ名', current?.name || '');
  if (!name?.trim() || name.trim() === current?.name) return;
  await authApiCall(`/ai/api/document-library/folders/${encodeURIComponent(folderId)}`, {
    method: 'PATCH', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name: name.trim() })
  });
  await loadDocumentLibrary();
}

export async function deleteFolder(folderId) {
  if (!window.confirm('空のフォルダを削除しますか？')) return;
  await authApiCall(`/ai/api/document-library/folders/${encodeURIComponent(folderId)}`, { method: 'DELETE' });
  if (state.selectedFolderId === folderId) state.selectedFolderId = 'folder_root';
  await loadDocumentLibrary();
}

export async function startDraftIngest(files) {
  if (!files?.length) throw new Error('ファイルを選択してください');
  if (!state.folders.length) await loadMasters();
  const folderId = document.getElementById('uploadTargetFolder')?.value || 'folder_unclassified';
  showLoading('ドラフトをObject Storageへ保存しています...');
  try {
    const batch = await authApiCall('/ai/api/document-library/ingest/batches', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ target_folder_id: folderId })
    });
    const form = new FormData();
    for (const file of files) form.append('files', file);
    const uploaded = await authApiCall(`/ai/api/document-library/ingest/batches/${encodeURIComponent(batch.batch_id)}/items`, {
      method: 'POST', body: form, timeout: 300000
    });
    state.currentBatch = uploaded.batch;
    state.ingestItems = uploaded.items || [];
    state.ingestError = '';
    state.classificationProgressMessage = 'アップロードが完了しました。先行解析を開始します。';
    renderIngestReview();
    hideLoading();
    if (uploaded.failed?.length) showToast(`${uploaded.failed.length}件の保存に失敗しました`, 'warning');
    await classifyAllDrafts({ onlyPending: false });
  } finally {
    hideLoading();
  }
}

export function ingestItemAnalysisComplete(item) {
  return item?.llm_result != null || [
    'LLM_SKIPPED', 'REVIEW_REQUIRED', 'CONFIRMED', 'COMMITTING',
    'REGISTERED', 'INDEX_QUEUED', 'INDEXED'
  ].includes(String(item?.state || ''));
}

export async function classifyAllDrafts({ onlyPending = false } = {}) {
  if (state.classificationInProgress || !state.ingestItems.length) return;
  const preservedReviews = captureReviewValues();
  applyCapturedReviewValues(preservedReviews);
  const pending = state.ingestItems
    .map((item, index) => ({ item, index }))
    .filter(({ item }) => !onlyPending || !ingestItemAnalysisComplete(item));
  if (!pending.length) {
    state.classificationProgressMessage = '解析済みの候補と手入力内容を復元しました。内容を確認して登録できます。';
    state.ingestError = '';
    renderIngestReview();
    return;
  }
  const completedAtStart = onlyPending ? state.ingestItems.length - pending.length : 0;
  let failed = 0;
  state.classificationInProgress = true;
  state.ingestError = '';
  renderIngestReview();
  try {
    for (let pendingIndex = 0; pendingIndex < pending.length; pendingIndex += 1) {
      const { item, index } = pending[pendingIndex];
      const current = completedAtStart + pendingIndex + 1;
      state.classificationProgressMessage = `内容を先行解析しています ${current}/${state.ingestItems.length}: ${item.original_filename}`;
      setReviewProgress(state.classificationProgressMessage);
      let classified = null;
      try {
        classified = await authApiCall(`/ai/api/document-library/ingest/items/${encodeURIComponent(item.item_id)}/classify`, { method: 'POST', timeout: 300000 });
      } catch (error) {
        failed += 1;
        state.ingestItems[index].error_summary = error.message;
      }
      const latestReviews = captureReviewValues();
      applyCapturedReviewValues(latestReviews);
      if (classified) {
        classified.review = {
          ...(classified.review || {}),
          ...(preservedReviews.get(String(item.item_id)) || {}),
          ...(latestReviews.get(String(item.item_id)) || {})
        };
        state.ingestItems[index] = classified;
      }
      state.classificationProgressMessage = `内容を先行解析しています ${current}/${state.ingestItems.length}`;
      renderIngestReview();
    }
  } finally {
    state.classificationInProgress = false;
    state.classificationProgressMessage = failed
      ? `先行解析が終了しました（失敗 ${failed}件）。失敗理由を確認し、必要なら再解析してください。`
      : '解析が完了しました。候補を確認してから登録してください。';
    state.ingestError = failed ? `${failed}件の先行解析に失敗しました。各行の理由を確認してください。` : '';
    renderIngestReview();
  }
}

function renderIngestReview() {
  const root = document.getElementById('ingestReviewRoot');
  if (!root) return;
  root.style.display = 'block';
  const rows = state.ingestItems.map((item, index) => {
    const selected = selectedTagIds(item);
    const candidateIds = candidateTagIds(item);
    const customer = reviewCustomer(item);
    const date = reviewDate(item);
    const evidence = (item.candidates || []).map(candidate => {
      const source = candidate.source === 'LLM' ? '内容' : 'ファイル名';
      const confidence = candidate.confidence == null ? '' : ` ${(candidate.confidence * 100).toFixed(0)}%`;
      return `${source}: ${candidate.value_raw}${confidence}${candidate.ambiguous ? '（競合）' : ''}`;
    }).join(' / ');
    return `<tr data-ingest-item="${escapeHtml(item.item_id)}" data-row-version="${item.row_version}" data-needs-review="${itemNeedsReview(item)}">
      <td><strong>${escapeHtml(item.original_filename)}</strong><div class="ingest-evidence">${escapeHtml(evidence || item.error_summary || '候補なし')}</div></td>
      <td><select data-review-folder class="form-input">${folderOptions(item.review?.folder_id || item.folder_id, false)}</select></td>
      <td><select data-review-document-set class="form-input">${documentSetOptions(item.review?.document_set_id || '')}</select><div class="metadata-settings-actions"><button type="button" class="apex-button-secondary apex-button-xs" onclick="window.documentLibraryModule.suggestDocumentSet(this,'${escapeHtml(item.document_id)}')">候補</button><button type="button" class="apex-button-secondary apex-button-xs" onclick="window.documentLibraryModule.createDocumentSet(this)">新規</button></div></td>
      <td><div class="ingest-tag-cell">${tagPicker(selected, candidateIds, `review-${item.item_id}`)}</div></td>
      <td><div class="ingest-review-value"><input data-review-customer class="form-input" value="${escapeHtml(customer)}" placeholder="要確認">${adjacentReviewControls('customer', '顧客名', index, state.ingestItems.length)}</div></td>
      <td><div class="ingest-review-value"><div class="metadata-year-range"><input data-review-year type="number" class="form-input" value="${date.year}" aria-label="年"><input data-review-month type="number" min="1" max="12" class="form-input" value="${date.month}" aria-label="月"></div>${adjacentReviewControls('date', '年月', index, state.ingestItems.length)}</div></td>
      <td><span class="review-status ${itemNeedsReview(item) ? 'pending' : 'ready'}">${itemNeedsReview(item) ? '要確認' : 'ルール確定'}</span></td>
    </tr>`;
  }).join('');
  const actionDisabled = state.classificationInProgress ? 'disabled' : '';
  const progressMessage = state.classificationProgressMessage || '候補を確認してください';
  const errorMessage = state.ingestError
    ? `<div id="ingestReviewError" class="ingest-review-error" role="alert"><i class="fas fa-circle-exclamation"></i><span>${escapeHtml(state.ingestError)}</span></div>`
    : '';
  root.innerHTML = `<div class="apex-region-header"><span><i class="fas fa-clipboard-check"></i> 登録内容の確認</span><span id="ingestReviewProgress" class="text-xs">${escapeHtml(progressMessage)}</span></div>
    ${errorMessage}
    <div class="ingest-review-toolbar">
      <select id="bulkReviewFolder" class="form-input"><option value="">登録先を一括変更...</option>${folderOptions('', false)}</select>
      <button class="apex-button-secondary px-3 py-2" onclick="window.documentLibraryModule.applyBulkFolder()">適用</button>
      <select id="bulkReviewDocumentSet" class="form-input"><option value="__none__">案件を一括変更...</option>${documentSetOptions('')}</select>
      <button class="apex-button-secondary px-3 py-2" onclick="window.documentLibraryModule.applyBulkDocumentSet()">適用</button>
      <label class="metadata-inline-check"><input id="onlyUnclassifiedReview" type="checkbox" onchange="window.documentLibraryModule.filterReviewRows()"> 要確認のみ</label>
      <button id="reclassifyIngestButton" class="apex-button-secondary px-3 py-2" ${actionDisabled} onclick="window.documentLibraryModule.classifyAllDrafts({ onlyPending: false })"><i class="fas fa-brain"></i> 内容を再解析</button>
      <button id="discardIngestButton" class="apex-button-secondary px-3 py-2" ${actionDisabled} onclick="window.documentLibraryModule.cancelCurrentBatch()"><i class="fas fa-times"></i> ドラフト破棄</button>
      <button id="confirmIngestButton" class="apex-button px-4 py-2" ${actionDisabled} onclick="window.documentLibraryModule.confirmCurrentBatch()"><i class="fas fa-check"></i> ${state.classificationInProgress ? '解析完了後に登録できます' : '表示内容を確認して登録'}</button>
    </div>
    <div class="table-wrapper-scrollable"><table class="apex-table ingest-review-table"><thead><tr><th>ファイル・根拠</th><th>フォルダ</th><th>案件グループ</th><th>タグ</th><th>顧客</th><th>年月</th><th>状態</th></tr></thead><tbody>${rows}</tbody></table></div>`;
}

export function applyBulkFolder() {
  const value = document.getElementById('bulkReviewFolder')?.value;
  if (!value) return;
  document.querySelectorAll('[data-ingest-item] [data-review-folder]').forEach(select => { select.value = value; });
}

export function applyBulkDocumentSet() {
  const value = document.getElementById('bulkReviewDocumentSet')?.value;
  if (value == null || value === '__none__') return;
  document.querySelectorAll('[data-ingest-item] [data-review-document-set]').forEach(select => { select.value = value; });
}

export function filterReviewRows() {
  const only = document.getElementById('onlyUnclassifiedReview')?.checked;
  document.querySelectorAll('[data-ingest-item]').forEach(row => {
    row.hidden = Boolean(only && row.dataset.needsReview !== 'true');
  });
}

export async function confirmCurrentBatch() {
  if (!state.currentBatch) return;
  if (state.classificationInProgress) {
    state.ingestError = '先行解析中です。すべての解析が終わってから登録してください。';
    renderIngestReview();
    return;
  }
  state.ingestError = '';
  const rows = [...document.querySelectorAll('[data-ingest-item]')];
  const customerValues = rows.map(row => row.querySelector('[data-review-customer]').value.trim());
  if (!await confirmCustomerNameVariants(customerValues)) return;
  showLoading('確認内容を保存しています...');
  try {
    for (const row of rows) {
      const existingItem = state.ingestItems.find(item => String(item.item_id) === String(row.dataset.ingestItem));
      if (['REGISTERED', 'INDEX_QUEUED', 'INDEXED'].includes(String(existingItem?.state || ''))) continue;
      const year = Number(row.querySelector('[data-review-year]').value) || null;
      const month = Number(row.querySelector('[data-review-month]').value) || null;
      const customer = row.querySelector('[data-review-customer]').value.trim();
      const payload = {
        folder_id: row.querySelector('[data-review-folder]').value,
        document_set_id: row.querySelector('[data-review-document-set]').value || null,
        customer_name_raw: customer,
        document_year: year,
        document_month: month,
        date_precision: year ? (month ? 'YEAR_MONTH' : 'YEAR') : 'UNKNOWN',
        tag_ids: [...row.querySelectorAll('.ingest-tag-cell input:checked')].map(input => input.value),
        expected_row_version: Number(row.dataset.rowVersion)
      };
      const updated = await authApiCall(`/ai/api/document-library/ingest/items/${encodeURIComponent(row.dataset.ingestItem)}`, {
        method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload)
      });
      row.dataset.rowVersion = updated.row_version;
      const itemIndex = state.ingestItems.findIndex(item => String(item.item_id) === String(row.dataset.ingestItem));
      if (itemIndex >= 0) state.ingestItems[itemIndex] = { ...updated, review: { ...(updated.review || {}), ...payload } };
    }
    const result = await authApiCall(`/ai/api/document-library/ingest/batches/${encodeURIComponent(state.currentBatch.batch_id)}/confirm`, { method: 'POST' });
    showToast(`登録を確定し、索引Job ${result.job_id} を開始しました`, 'success');
    document.getElementById('ingestReviewRoot').style.display = 'none';
    state.currentBatch = null;
    state.ingestItems = [];
    state.classificationProgressMessage = '';
    state.ingestError = '';
    await loadDocumentLibrary();
  } catch (error) {
    applyCapturedReviewValues(captureReviewValues());
    state.ingestError = `登録できませんでした: ${error.message}`;
    renderIngestReview();
    showToast(state.ingestError, 'error');
  } finally {
    hideLoading();
  }
}

export async function createDocumentSet(trigger) {
  const label = window.prompt('案件グループ名（顧客名だけでなく、場所・工事名・時期など識別できる名前を推奨）');
  if (!label?.trim()) return;
  const created = await authApiCall('/ai/api/document-library/document-sets', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ label: label.trim(), description: null })
  });
  state.documentSets.push(created);
  document.querySelectorAll('[data-edit-document-set], [data-review-document-set], #bulkReviewDocumentSet').forEach(select => {
    if (![...select.options].some(option => option.value === created.document_set_id)) {
      select.add(new Option(created.label, created.document_set_id));
    }
  });
  const target = trigger?.closest('.metadata-editor, [data-ingest-item]')?.querySelector('[data-edit-document-set], [data-review-document-set]');
  if (target) target.value = created.document_set_id;
  showToast('案件グループを作成しました。保存または登録すると文書へ反映されます', 'success');
}

export async function suggestDocumentSet(trigger, documentId) {
  if (!documentId) {
    showToast('文書登録後に候補を確認できます。現在は一覧から選択するか新規作成してください', 'info');
    return;
  }
  const suggestions = await authApiCall(`/ai/api/document-library/documents/${encodeURIComponent(documentId)}/document-set-suggestions?limit=8`);
  if (!suggestions.length) {
    showToast('安全に提示できる案件候補はありません', 'info');
    return;
  }
  const lines = suggestions.map((item, index) => `${index + 1}: ${item.label}（${item.reasons.join('・')}）`).join('\n');
  const selectedNumber = Number(window.prompt(`候補は顧客名だけでは確定しません。内容を確認して番号を入力してください。\n\n${lines}`));
  const selected = suggestions[selectedNumber - 1];
  if (!selected) return;
  const target = trigger?.closest('.metadata-editor, [data-ingest-item]')?.querySelector('[data-edit-document-set], [data-review-document-set]');
  if (target) target.value = selected.document_set_id;
}

export async function cancelCurrentBatch() {
  if (!state.currentBatch) return;
  if (state.classificationInProgress) {
    state.ingestError = '先行解析中はドラフトを破棄できません。解析完了後に実行してください。';
    renderIngestReview();
    return;
  }
  if (!window.confirm('一時保存したObjectを削除してドラフトを破棄します。復元できません。続行しますか？')) return;
  const batchId = state.currentBatch.batch_id;
  showLoading('ドラフトを破棄しています...');
  try {
    const result = await authApiCall(`/ai/api/document-library/ingest/batches/${encodeURIComponent(batchId)}`, {
      method: 'DELETE', timeout: 120000
    });
    showToast(`${result.deleted_objects || 0}件の一時Objectを削除し、ドラフトを破棄しました`, 'success');
    document.getElementById('ingestReviewRoot').style.display = 'none';
    state.currentBatch = null;
    state.ingestItems = [];
    state.classificationProgressMessage = '';
    state.ingestError = '';
    state.dismissedActiveIngestBatchIds.delete(String(batchId));
    await loadMasters();
    await refresh();
    await loadActiveIngestBatches();
  } catch (error) {
    state.ingestError = `ドラフトを破棄できませんでした: ${error.message}`;
    renderIngestReview();
    showToast(state.ingestError, 'error');
  } finally {
    hideLoading();
  }
}

window.documentLibraryModule = {
  load: loadDocumentLibrary,
  refresh,
  toggleLibraryDocumentSelection,
  toggleAllVisibleDocuments,
  bulkMoveSelectedDocuments,
  bulkDeleteSelectedDocuments,
  bulkReprocessSelectedDocuments,
  selectFolder,
  changePage,
  setLibrarySort,
  createFolderPrompt,
  renameFolder,
  deleteFolder,
  toggleMetadataEditor,
  saveMetadata,
  showProcessingDetails,
  closeProcessingDetails,
  refreshProcessingDetails,
  retryProcessingJob,
  toggleDocumentPages,
  loadDocumentPages,
  loadMoreDocumentPages,
  previewDocumentPageImage,
  previewDocumentPageTexts,
  startDraftIngest,
  loadActiveIngestBatches,
  resumeActiveIngestBatch,
  discardActiveIngestBatch,
  dismissActiveIngestBatch,
  classifyAllDrafts,
  ingestItemAnalysisComplete,
  applyBulkFolder,
  applyBulkDocumentSet,
  createDocumentSet,
  suggestDocumentSet,
  copyAdjacentReviewValue,
  filterReviewRows,
  confirmCurrentBatch,
  cancelCurrentBatch
};
