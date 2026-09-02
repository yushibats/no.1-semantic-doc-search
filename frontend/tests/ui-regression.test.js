import assert from 'node:assert/strict';
import { after, before, test } from 'node:test';
import { readFile } from 'node:fs/promises';
import { JSDOM } from 'jsdom';
import { createServer } from 'vite';

let authModule;
let dbModule;
let documentLibraryModule;
let metadataSettingsModule;
let pipelineModule;
let retrievalSettingsModule;
let searchModule;
let stateModule;
let utilsModule;
let vite;

before(async () => {
  const dom = new JSDOM('<!doctype html><html><body></body></html>', {
    url: 'http://localhost/'
  });
  Object.assign(globalThis, {
    window: dom.window,
    document: dom.window.document,
    localStorage: dom.window.localStorage,
    FileReader: dom.window.FileReader,
    FormData: dom.window.FormData,
    CSS: dom.window.CSS || { escape: value => String(value).replace(/[^a-zA-Z0-9_-]/g, '\\$&') }
  });
  vite = await createServer({
    root: process.cwd(),
    appType: 'custom',
    logLevel: 'silent',
    server: { middlewareMode: true }
  });
  authModule = await vite.ssrLoadModule('/src/modules/auth.js');
  dbModule = await vite.ssrLoadModule('/src/modules/db.js');
  documentLibraryModule = await vite.ssrLoadModule('/src/modules/document-library.js');
  metadataSettingsModule = await vite.ssrLoadModule('/src/modules/metadata-settings.js');
  pipelineModule = await vite.ssrLoadModule('/src/modules/pipeline.js');
  retrievalSettingsModule = await vite.ssrLoadModule('/src/modules/retrieval-settings.js');
  searchModule = await vite.ssrLoadModule('/src/modules/search.js');
  stateModule = await vite.ssrLoadModule('/src/state.js');
  utilsModule = await vite.ssrLoadModule('/src/modules/utils.js');
});

test('文書一覧の索引状態はJob失敗を処理中ではなく失敗として表示する', () => {
  assert.equal(documentLibraryModule.pipelineLabel({
    status: 'PROCESSING',
    processing: { document_status: 'FAILED' }
  }), '失敗');
  assert.equal(documentLibraryModule.pipelineLabel({
    status: 'INDEXED',
    processing: { document_status: 'UPDATE_FAILED' }
  }), '更新失敗');
});

test('処理詳細はOCR・MinerU・VLM・埋め込みを利用者向け工程名にする', () => {
  assert.equal(documentLibraryModule.processingStepLabel({ kind: 'MINERU_PARSE' }), 'MinerU解析');
  assert.equal(documentLibraryModule.processingStepLabel({ kind: 'OCR' }), 'OCR');
  assert.equal(documentLibraryModule.processingStepLabel({ kind: 'VLM', component_key: 'vlm:2' }), 'VLM抽出（プロファイル 2）');
  assert.equal(documentLibraryModule.processingStepLabel({ kind: 'EMBED', component_key: 'embedding:image' }), '埋め込み（image）');
  assert.equal(documentLibraryModule.processingStepLabel({ kind: 'CONCEPT', component_key: 'concepts' }), 'AI検索候補抽出');
});

test('検索画面はタグ・自然言語を主操作にして詳細設定を折りたたむ', async () => {
  const html = await readFile(new URL('../index.html', import.meta.url), 'utf8');
  const styles = await readFile(new URL('../src/style.css', import.meta.url), 'utf8');
  const page = new JSDOM(html);
  const document = page.window.document;
  const settings = document.getElementById('searchAdvancedSettings');
  const metadata = document.getElementById('metadataSearchFilters');
  const concepts = document.getElementById('searchConceptPanel');

  assert.equal(settings.tagName, 'DETAILS');
  assert.equal(settings.open, false);
  assert.match(settings.querySelector('summary').textContent, /検索設定/);
  for (const id of ['topK', 'minScore', 'filenameFilter', 'searchRetrievalModes', 'searchVlmVerify']) {
    assert.ok(settings.querySelector('#' + id), id + ' must be inside search settings');
  }

  assert.equal(concepts.tagName, 'DETAILS');
  assert.equal(concepts.open, false);
  assert.match(concepts.querySelector('summary').textContent, /タグから検索/);
  assert.ok(document.getElementById('searchConceptRequireAll'));
  assert.match(concepts.textContent, /文章を入力しなくても、タグだけで検索できます/);
  assert.match(styles, /\.selected-search-concepts\[hidden\][\s\S]*\.search-concept-query-status\[hidden\]\s*\{\s*display:\s*none;/);

  assert.match(document.getElementById('searchTypeTextTab').textContent, /自然言語で検索/);
  assert.match(document.querySelector('label[for="searchQuery"]').textContent, /文章で入力/);
  assert.match(metadata.querySelector('summary').textContent, /フォルダ・カテゴリ・顧客・年月/);
  assert.match(metadata.textContent, /カテゴリ（すべて満たす）/);
  assert.doesNotMatch(metadata.querySelector('summary').textContent, /タグ/);

  const auxiliary = document.querySelector('.search-auxiliary-options');
  const textPanel = document.getElementById('textSearchPanel');
  assert.ok(auxiliary);
  assert.match(auxiliary.querySelector('.search-auxiliary-label').textContent, /絞り込み・詳細設定/);
  assert.equal(document.getElementById('searchQuery').placeholder, '例: 開放的なLDK、アイランドキッチン');
  assert.equal(metadata.closest('.search-auxiliary-options'), auxiliary);
  assert.equal(settings.closest('.search-auxiliary-options'), auxiliary);
  assert.ok(concepts.classList.contains('search-primary-concept-panel'));
  assert.ok(textPanel.classList.contains('search-primary-query-panel'));
  assert.ok(concepts.compareDocumentPosition(auxiliary) & page.window.Node.DOCUMENT_POSITION_FOLLOWING);
  assert.ok(textPanel.compareDocumentPosition(auxiliary) & page.window.Node.DOCUMENT_POSITION_FOLLOWING);
});

test('入力文からAIが返した既存検索条件だけに候補を絞り込む', async () => {
  document.body.innerHTML = `
    <details id="searchConceptPanel">
      <small id="searchConceptSummaryCount"></small>
      <small id="searchConceptQueryStatus" hidden></small>
      <div id="selectedSearchConcepts"></div>
      <div id="searchConceptFacets"></div>
    </details>
  `;
  const requests = [];
  globalThis.fetch = async (url, options = {}) => {
    requests.push({ url: String(url), body: options.body ? JSON.parse(options.body) : null });
    const body = String(url).endsWith('/search/v2/filters')
      ? {
          v2_retrieval_active: true,
          document_library_ready: true,
          fields: [],
          folders: [],
          tag_groups: [],
          tags: [],
          customer_suggestions: [],
          date_bounds: {},
          search_concept_settings: { enabled: true, initial_display_limit: 8 },
          search_concepts: [
            {
              concept_id: 'isolated-kitchen',
              display_label: '独立した厨房',
              facet: 'BEFORE',
              category_code: 'kitchen',
              category_name: 'キッチン・LDK',
              status: 'ACTIVE',
              support_set_count: 1
            },
            {
              concept_id: 'south-european',
              display_label: '南欧風',
              facet: 'OTHER',
              category_code: 'design',
              category_name: 'デザイン',
              status: 'ACTIVE',
              support_set_count: 1
            },
            {
              concept_id: 'family-time',
              display_label: '家族の団らん',
              facet: 'AFTER',
              category_code: 'lifestyle',
              category_name: '暮らし',
              status: 'ACTIVE',
              support_set_count: 2
            },
            {
              concept_id: 'storage',
              display_label: '玄関収納増設',
              facet: 'AFTER',
              category_code: 'storage',
              category_name: '収納',
              status: 'ACTIVE',
              support_set_count: 1
            }
          ],
          retrieval_modes: []
        }
      : {
          concept_ids: ['family-time'],
          source: 'AI',
          message: '1件の関連候補を抽出しました'
        };
    return { ok: true, status: 200, json: async () => body };
  };

  await searchModule.loadDynamicSearchFilters();
  const facetRoot = document.getElementById('searchConceptFacets');
  assert.ok(facetRoot.querySelector('[data-facet="BEFORE"]'));
  assert.ok(facetRoot.querySelector('[data-facet="AFTER"]'));
  assert.ok(facetRoot.querySelector('[data-facet="OTHER"]'));
  searchModule.filterSearchConcepts('家族が一緒にくつろげるリビング');
  await new Promise(resolve => setTimeout(resolve, 650));

  const suggestionRequest = requests.find(item => item.url.endsWith('/search/v2/concepts/suggest'));
  assert.deepEqual(suggestionRequest.body, {
    query: '家族が一緒にくつろげるリビング',
    limit: 24
  });
  assert.match(document.getElementById('searchConceptFacets').textContent, /家族の団らん/);
  assert.doesNotMatch(document.getElementById('searchConceptFacets').textContent, /玄関収納増設/);
  assert.match(document.getElementById('searchConceptQueryStatus').textContent, /1件/);
});


test("要確認候補は折りたたみ・全幅表示され、一括承認でも画面を再描画しない", async () => {
  document.body.innerHTML = "<div id=\"metadataSettingsRoot\"></div>";
  const pendingConcepts = ["concept-1", "concept-2", "concept-3"].map((conceptId, index) => ({
    concept_id: conceptId,
    facet: index === 0 ? "BEFORE" : "AFTER",
    category_code: "category",
    category_name: "カテゴリ",
    display_label: `候補${index + 1}`,
    normalized_label: `候補${index + 1}`,
    status: "PENDING",
    support_document_count: index + 1,
    support_set_count: 1
  }));
  const patchRequests = [];
  window.UIComponents = { showToast() {} };
  globalThis.fetch = async (url, options = {}) => {
    const value = String(url);
    let body = [];
    if (value.includes("/settings/search-concepts") && (options.method || "GET") === "GET") {
      body = { enabled: true, auto_publish: false, prompt_text: "test" };
    } else if (value.includes("/search-concepts?status=PENDING")) {
      body = pendingConcepts;
    }
    if (options.method === "PATCH") {
      patchRequests.push({ url: value, body: JSON.parse(options.body) });
    }
    return { ok: true, status: 200, json: async () => body };
  };

  await metadataSettingsModule.loadMetadataSettings();
  const root = document.getElementById("metadataSettingsRoot");
  const review = document.getElementById("pendingConceptReview");
  assert.equal(review.open, false);
  assert.ok(review.querySelector(".metadata-settings-single-table"));

  review.open = true;
  metadataSettingsModule.togglePendingConceptSelection("concept-1", true);
  metadataSettingsModule.togglePendingConceptSelection("concept-2", true);
  await metadataSettingsModule.setSelectedConceptStatus("ACTIVE");

  assert.equal(document.getElementById("metadataSettingsRoot"), root);
  assert.equal(review.open, true);
  assert.equal(document.querySelectorAll("[data-pending-concept]").length, 1);
  assert.deepEqual(patchRequests.slice(0, 2), [
    {
      url: "/ai/api/document-library/settings/search-concepts/concept-1",
      body: { status: "ACTIVE" }
    },
    {
      url: "/ai/api/document-library/settings/search-concepts/concept-2",
      body: { status: "ACTIVE" }
    }
  ]);

  await metadataSettingsModule.setConceptStatus("concept-3", "HIDDEN");
  assert.equal(document.getElementById("metadataSettingsRoot"), root);
  assert.equal(document.querySelectorAll("[data-pending-concept]").length, 0);
  assert.match(document.getElementById("pendingConceptSummaryText").textContent, /要確認候補はありません/);
});

test('処理詳細ダイアログは失敗工程・原因・対象外工程を区別して表示する', async () => {
  document.body.innerHTML = '';
  const dialogPrototype = Object.getPrototypeOf(document.createElement('dialog'));
  const originalShowModal = dialogPrototype.showModal;
  const originalClose = dialogPrototype.close;
  const openedDialogs = [];
  dialogPrototype.showModal = function showModalForTest() {
    this.setAttribute('open', '');
    openedDialogs.push(this.id);
  };
  dialogPrototype.close = function closeForTest() {
    this.removeAttribute('open');
    this.dispatchEvent(new window.Event('close'));
  };
  globalThis.fetch = async () => ({
    ok: true,
    status: 200,
    json: async () => ({
      document_status: 'FAILED',
      job: {
        job_id: 'job-1',
        status: 'PARTIAL_FAILED',
        affected_object_count: 1,
        updated_at: '2026-08-03T00:00:00Z',
        steps: [
          { kind: 'RENDER', component_key: 'render', status: 'SUCCEEDED', attempt_count: 1 },
          { kind: 'MINERU_PARSE', component_key: 'mineru', status: 'FAILED', attempt_count: 1, error_summary: 'GPU APIへ接続できません' },
          { kind: 'NORMALIZE', component_key: 'normalize', status: 'BLOCKED', attempt_count: 0 }
        ]
      }
    })
  });

  await documentLibraryModule.showProcessingDetails('doc-1', 'sample.pdf');

  const dialogText = document.getElementById('documentProcessingDialog').textContent;
  assert.match(dialogText, /MinerU解析で失敗しました/);
  assert.match(dialogText, /GPU APIへ接続できません/);
  assert.match(dialogText, /OCR/);
  assert.match(dialogText, /VLM抽出/);
  assert.match(dialogText, /対象外/);
  const retryButton = document.getElementById('documentProcessingRetry');
  assert.equal(retryButton.hidden, false);
  assert.match(retryButton.textContent, /GPU関連工程から再試行/);

  let retryRequest;
  window.UIComponents = {
    showToast() {}
  };
  globalThis.fetch = async (url, options = {}) => {
    if (options.method === 'POST') {
      retryRequest = { url, method: options.method };
      return { ok: true, status: 202, json: async () => ({ success: true, job_id: 'job-2' }) };
    }
    return {
      ok: true,
      status: 200,
      json: async () => ({
        document_status: 'PROCESSING',
        job: {
          job_id: 'job-2',
          status: 'QUEUED',
          affected_object_count: 1,
          steps: [
            { kind: 'MINERU_PARSE', component_key: 'mineru', status: 'QUEUED', attempt_count: 0 },
            { kind: 'NORMALIZE', component_key: 'normalize', status: 'QUEUED', attempt_count: 0 }
          ]
        }
      })
    };
  };

  const retryPromise = documentLibraryModule.retryProcessingJob();
  await Promise.resolve();
  const retryConfirmDialog = document.getElementById('processingRetryConfirmDialog');
  assert.equal(retryConfirmDialog.tagName, 'DIALOG');
  assert.equal(retryConfirmDialog.open, true);
  assert.equal(document.getElementById('documentProcessingDialog').open, true);
  assert.deepEqual(openedDialogs, ['documentProcessingDialog', 'processingRetryConfirmDialog']);
  retryConfirmDialog.querySelector('[data-retry-confirm]').click();
  await retryPromise;

  assert.equal(retryRequest.method, 'POST');
  assert.match(String(retryRequest.url), /\/ai\/api\/pipeline\/jobs\/job-1\/retry$/);
  assert.match(document.getElementById('documentProcessingDialog').textContent, /待っています/);
  documentLibraryModule.closeProcessingDetails();
  if (originalShowModal) dialogPrototype.showModal = originalShowModal;
  else delete dialogPrototype.showModal;
  if (originalClose) dialogPrototype.close = originalClose;
  else delete dialogPrototype.close;
  window.UIComponents = undefined;
});

test('処理詳細は全体工程と追加Jobをタブで切り替える', async () => {
  document.body.innerHTML = '';
  const originalSteps = [
    { kind: 'RENDER', component_key: 'render', status: 'SUCCEEDED', attempt_count: 1 },
    { kind: 'VLM', component_key: 'vlm:1', status: 'SUCCEEDED', attempt_count: 1 },
    { kind: 'EMBED', component_key: 'embedding:image', status: 'SUCCEEDED', attempt_count: 1 },
    { kind: 'PUBLISH', component_key: 'publish', status: 'SUCCEEDED', attempt_count: 1 },
    { kind: 'CONCEPT', component_key: 'concepts', status: 'FAILED', attempt_count: 1, error_summary: '一時エラー' }
  ];
  const additionalSteps = [
    { kind: 'CONCEPT', component_key: 'concepts', status: 'SUCCEEDED', attempt_count: 1 }
  ];
  globalThis.fetch = async () => ({
    ok: true,
    status: 200,
    json: async () => ({
      document_status: 'INDEXED',
      overall_job_id: 'job-original',
      job: {
        job_id: 'job-retry',
        status: 'SUCCEEDED',
        is_additional: true,
        tab_label: '追加Job 1',
        affected_object_count: 1,
        steps: additionalSteps
      },
      job_history: [
        {
          job_id: 'job-original',
          status: 'PARTIAL_FAILED',
          is_additional: false,
          tab_label: '全体工程',
          affected_object_count: 1,
          steps: originalSteps
        },
        {
          job_id: 'job-retry',
          status: 'SUCCEEDED',
          is_additional: true,
          tab_label: '追加Job 1',
          affected_object_count: 1,
          steps: additionalSteps
        }
      ]
    })
  });

  await documentLibraryModule.showProcessingDetails('doc-history', 'plan.pdf');

  const dialog = document.getElementById('documentProcessingDialog');
  assert.equal(dialog.querySelectorAll('.document-processing-tabs button').length, 2);
  assert.match(dialog.querySelector('.document-processing-tabs button.active').textContent, /追加Job 1/);
  assert.match(dialog.textContent, /追加Jobが完了しました/);
  assert.match(dialog.textContent, /追加Jobで実行した工程だけ/);
  assert.doesNotMatch(dialog.querySelector('#documentProcessingBody').textContent, /VLM抽出/);

  documentLibraryModule.selectProcessingJob('job-original');

  assert.match(dialog.querySelector('.document-processing-tabs button.active').textContent, /全体工程/);
  assert.match(dialog.textContent, /最初に計画された一連の工程/);
  assert.match(dialog.textContent, /VLM抽出（プロファイル 1）/);
  assert.match(dialog.textContent, /索引公開/);
  assert.equal(document.getElementById('documentProcessingRetry').hidden, true);
  documentLibraryModule.closeProcessingDetails();
});


test('現在の文書一覧でDraftのページ画像とページ別生成テキストを表示する', async () => {
  document.body.innerHTML = `
    <div id="documentFolderTree"></div>
    <span id="documentsStatusBadge"></span>
    <span id="documentsFileCountBadge"></span>
    <span id="documentsPageImageCountBadge"></span>
    <div id="documentsList"></div>
    <input id="libraryIncludeDescendants" type="checkbox" checked>
    <input id="libraryQuery" value="">
  `;
  window.UIComponents = { showToast() {} };
  localStorage.setItem('loginToken', 'token');

  globalThis.fetch = async url => {
    const value = String(url);
    if (value.includes('/document-library/folders')) {
      return {
        ok: true,
        status: 200,
        json: async () => ([{
          folder_id: 'folder_root',
          name: 'ルート',
          is_system: true,
          document_count: 1,
          descendant_document_count: 1,
          children: [{
            folder_id: 'folder-remodel',
            name: 'リフォーム',
            is_system: false,
            document_count: 1,
            descendant_document_count: 1,
            children: []
          }]
        }])
      };
    }
    if (value.includes('/settings/tag-groups') || value.includes('/settings/tags')) {
      return { ok: true, status: 200, json: async () => [] };
    }
    if (value.includes('/document-library/document-sets')) {
      return { ok: true, status: 200, json: async () => [] };
    }
    if (value.includes('/document-library/documents?')) {
      return {
        ok: true,
        status: 200,
        json: async () => ({
          items: [{
            document_id: 'doc-pages',
            object_name: 'documents/doc-pages/source.pdf',
            file_name: 'source.pdf',
            bucket: 'bucket',
            file_size: 1024,
            status: 'FAILED',
            metadata: {
              folder_id: 'folder-remodel',
              folder_name: 'リフォーム',
              customer_name_raw: '',
              document_year: null,
              document_month: null,
              tags: [],
              row_version: 1
            },
            processing: { document_status: 'FAILED' }
          }],
          total: 1,
          page: 1,
          total_pages: 1
        })
      };
    }
    if (value.includes('/document-library/ingest/active-batches')) {
      return { ok: true, status: 200, json: async () => [] };
    }
    if (value.includes('/documents/doc-pages/page-images')) {
      return {
        ok: true,
        status: 200,
        json: async () => ({
          document_id: 'doc-pages',
          object_name: 'documents/doc-pages/source.pdf',
          revision_id: 'revision-draft',
          release_id: 'release-draft',
          release_status: 'DRAFT',
          stage_status: 'SUCCEEDED',
          total: 1,
          items: [{
            artifact_id: 'artifact-1',
            page_number: 1,
            media_type: 'image/png',
            size: 2048,
            content_sha256: 'hash',
            created_at: '2026-08-03T00:00:00Z',
            stage_status: 'SUCCEEDED'
          }],
          pagination: {
            current_page: 1,
            page_size: 50,
            total: 1,
            total_pages: 1,
            has_next: false,
            has_prev: false
          }
        })
      };
    }
    if (value.includes('/documents/doc-pages/page-texts')) {
      return {
        ok: true,
        status: 200,
        json: async () => ({
          document_id: 'doc-pages',
          selector: 'latest',
          release_id: 'release-draft',
          release_status: 'DRAFT',
          page_number: 1,
          items: [
            {
              component_key: 'native',
              artifact_kind: 'NATIVE_TEXT',
              page_number: 1,
              raw_text: 'ネイティブ抽出結果',
              payload_json: null,
              created_at: '2026-08-03T00:00:00Z',
              stage_status: 'SUCCEEDED'
            },
            {
              component_key: 'vlm:1',
              artifact_kind: 'VLM_TEXT',
              page_number: 1,
              raw_text: 'VLM生成結果',
              payload_json: { kind: 'plan' },
              created_at: '2026-08-03T00:00:00Z',
              stage_status: 'SUCCEEDED'
            }
          ]
        })
      };
    }
    throw new Error(`unexpected request: ${value}`);
  };

  await documentLibraryModule.loadDocumentLibrary();
  assert.match(document.getElementById('documentsList').textContent, /ページを表示/);
  const documentLine = document.querySelector('.document-library-document-line');
  assert.ok(documentLine);
  const documentMeta = documentLine.querySelector('.document-library-document-meta');
  assert.ok(documentMeta);
  assert.equal(documentLine.querySelector('strong')?.nextElementSibling, documentMeta);
  assert.equal(documentMeta.querySelector('.metadata-chip-list')?.parentElement, documentMeta);
  assert.equal(documentMeta.querySelector('.document-pages-toggle')?.parentElement, documentMeta);

  await documentLibraryModule.toggleDocumentPages('doc-pages');
  const panel = document.getElementById('document-pages-panel-doc-pages');
  assert.match(panel.textContent, /Draft/);
  assert.match(panel.textContent, /ページ 1/);
  assert.match(panel.textContent, /生成テキスト/);
  assert.equal(panel.querySelectorAll('.document-page-card').length, 1);
  assert.match(panel.querySelector('img').src, /release-draft/);

  await documentLibraryModule.previewDocumentPageTexts('doc-pages', 1);
  const modal = document.getElementById('textPreviewModalOverlay');
  assert.match(modal.textContent, /ネイティブ抽出/);
  assert.match(modal.textContent, /ネイティブ抽出結果/);
  assert.match(modal.textContent, /VLMプロファイル 1/);
  modal.remove();
  window.UIComponents = undefined;
});

test('未完了アップロードを通知し解析済み項目を再利用して残りだけ再開する', async () => {
  document.body.innerHTML = `
    <div id="documentFolderTree"></div>
    <div id="ingestReviewRoot" style="display:none"></div>
    <span id="documentsStatusBadge"></span>
    <span id="documentsFileCountBadge"></span>
    <span id="documentsPageImageCountBadge"></span>
    <div id="documentsList"></div>
    <input id="libraryIncludeDescendants" type="checkbox" checked>
    <input id="libraryQuery" value="">
  `;
  window.UIComponents = { showToast() {} };
  localStorage.setItem('loginToken', 'token');
  let classifyRequests = 0;
  let resolveClassification;
  const item = (id, complete) => ({
    item_id: id,
    batch_id: 'batch-active',
    document_id: `document-${id}`,
    original_filename: `${id}.jpg`,
    object_name: `documents/${id}/source.jpg`,
    media_type: 'image/jpeg',
    file_size: 100,
    state: complete ? 'REVIEW_REQUIRED' : 'RULE_CLASSIFIED',
    folder_id: 'folder-new',
    rule_result: null,
    llm_result: complete ? { preview: {} } : null,
    review: complete ? { customer_name_raw: '保持する顧客名' } : {},
    candidates: [],
    error_summary: null,
    row_version: 1
  });
  const items = [item('done-1', true), item('done-2', true), item('pending-1', false)];

  globalThis.fetch = async (url, options = {}) => {
    const value = String(url);
    if (value.includes('/document-library/folders')) {
      return { ok: true, status: 200, json: async () => ([{
        folder_id: 'folder_root', name: 'ルート', is_system: true,
        document_count: 0, descendant_document_count: 0,
        children: [{ folder_id: 'folder-new', name: '新築', is_system: false, document_count: 0, descendant_document_count: 0, children: [] }]
      }]) };
    }
    if (value.includes('/settings/tag-groups') || value.includes('/settings/tags')) {
      return { ok: true, status: 200, json: async () => [] };
    }
    if (value.includes('/document-library/document-sets')) {
      return { ok: true, status: 200, json: async () => [] };
    }
    if (value.includes('/document-library/documents?')) {
      return { ok: true, status: 200, json: async () => ({ items: [], total: 0, page: 1, total_pages: 1 }) };
    }
    if (value.includes('/ingest/active-batches')) {
      return { ok: true, status: 200, json: async () => ([{
        batch_id: 'batch-active', status: 'REVIEW_REQUIRED',
        target_folder_id: 'folder-new', target_folder_name: '新築',
        total_items: 3, analysis_completed_items: 2, analysis_pending_items: 1,
        registered_items: 0, discardable: true, updated_at: '2026-08-03T00:07:00Z'
      }]) };
    }
    if (value.endsWith('/ingest/batches/batch-active')) {
      return { ok: true, status: 200, json: async () => ({
        batch: { batch_id: 'batch-active', status: 'REVIEW_REQUIRED', target_folder_id: 'folder-new', total_items: 3 },
        items
      }) };
    }
    if (value.includes('/ingest/items/pending-1/classify') && options.method === 'POST') {
      classifyRequests += 1;
      return new Promise(resolve => {
        resolveClassification = () => resolve({
          ok: true,
          status: 200,
          json: async () => ({ ...items[2], state: 'REVIEW_REQUIRED', llm_result: { preview: {} } })
        });
      });
    }
    if (value.includes('/customer-name-normalize?')) {
      return { ok: true, status: 200, json: async () => ({ similarity_warning: false, similar_names: [] }) };
    }
    if (value.includes('/ingest/items/') && options.method === 'PATCH') {
      return { ok: false, status: 409, json: async () => ({ detail: '取込項目が他の操作で更新されました' }) };
    }
    throw new Error(`unexpected request: ${value}`);
  };

  await documentLibraryModule.loadDocumentLibrary();
  assert.match(document.getElementById('ingestReviewRoot').textContent, /未完了のアップロードがあります/);
  assert.match(document.getElementById('ingestReviewRoot').textContent, /先行解析 2\/3/);

  const resumePromise = documentLibraryModule.resumeActiveIngestBatch('batch-active');
  while (!resolveClassification) await new Promise(resolve => setTimeout(resolve, 0));
  assert.equal(classifyRequests, 1);
  assert.equal(document.getElementById('confirmIngestButton').disabled, true);
  assert.match(document.getElementById('confirmIngestButton').textContent, /解析完了後/);
  resolveClassification();
  await resumePromise;

  assert.equal(classifyRequests, 1);
  assert.equal(document.getElementById('confirmIngestButton').disabled, false);
  assert.equal(document.querySelector('[data-ingest-item="done-1"] [data-review-customer]').value, '保持する顧客名');

  await documentLibraryModule.confirmCurrentBatch();
  assert.match(document.getElementById('ingestReviewError').textContent, /取込項目が他の操作で更新されました/);
  window.UIComponents = undefined;
});

test('取込項目の解析完了判定はLLM結果または完了後状態を使用する', () => {
  assert.equal(documentLibraryModule.ingestItemAnalysisComplete({ state: 'RULE_CLASSIFIED', llm_result: null }), false);
  assert.equal(documentLibraryModule.ingestItemAnalysisComplete({ state: 'REVIEW_REQUIRED', llm_result: null }), true);
  assert.equal(documentLibraryModule.ingestItemAnalysisComplete({ state: 'RULE_CLASSIFIED', llm_result: {} }), true);
});

test('アップロード確認表で顧客名と年月を上下の行から個別にコピーする', () => {
  const row = (id, customer, year, month) => `<tr data-ingest-item="${id}">
    <td><div class="ingest-review-value"><input data-review-customer value="${customer}"><button type="button">顧客コピー</button></div></td>
    <td><div class="ingest-review-value"><input data-review-year value="${year}"><input data-review-month value="${month}"><button type="button">年月コピー</button></div></td>
  </tr>`;
  document.body.innerHTML = `<table><tbody>
    ${row('first', '顧客A', '2024', '1')}
    ${row('middle', '', '2023', '6')}
    ${row('last', '顧客C', '2025', '12')}
  </tbody></table>`;

  const middle = document.querySelector('[data-ingest-item="middle"]');
  const customerButton = middle.querySelectorAll('button')[0];
  const dateButton = middle.querySelectorAll('button')[1];

  documentLibraryModule.copyAdjacentReviewValue(customerButton, 'customer', -1);
  assert.equal(middle.querySelector('[data-review-customer]').value, '顧客A');
  assert.equal(middle.querySelector('[data-review-year]').value, '2023');
  assert.equal(middle.querySelector('[data-review-month]').value, '6');

  documentLibraryModule.copyAdjacentReviewValue(dateButton, 'date', 1);
  assert.equal(middle.querySelector('[data-review-customer]').value, '顧客A');
  assert.equal(middle.querySelector('[data-review-year]').value, '2025');
  assert.equal(middle.querySelector('[data-review-month]').value, '12');

  const firstCustomer = document.querySelector('[data-ingest-item="first"] [data-review-customer]');
  documentLibraryModule.copyAdjacentReviewValue(document.querySelector('[data-ingest-item="first"] button'), 'customer', -1);
  assert.equal(firstCustomer.value, '顧客A');
});

after(async () => {
  await vite?.close();
});

test('ログイン成功後に検索フィルターを読み込む', async () => {
  document.body.innerHTML = `
    <form id="loginForm"><input id="loginUsername" value="tester"><input id="loginPassword" value="secret"></form>
    <div id="loginOverlay"></div><div id="loginError"></div><div id="loginErrorMessage"></div>
    <button id="loginSubmitBtn">ログイン</button><div id="userInfo"></div><span id="userName"></span>
  `;
  let filterLoads = 0;
  window.searchModule = { loadDynamicSearchFilters: async () => { filterLoads += 1; } };
  window.UIComponents = { showToast() {}, setSessionTimeoutToastMode() {} };
  globalThis.fetch = async () => ({
    ok: true,
    json: async () => ({ status: 'success', token: 'token', username: 'tester' })
  });

  await authModule.handleLogin({ preventDefault() {} });

  assert.equal(filterLoads, 1);
  assert.equal(localStorage.getItem('loginToken'), 'token');
});

test('新しい共有検索の有効状態でも最小ベクトル類似度を有効のままにする', async () => {
  document.body.innerHTML = `
    <fieldset id="dynamicSearchFilters" hidden><div id="dynamicSearchFilterFields"></div></fieldset>
    <label id="minScoreLabel" for="minScore">最小ベクトル類似度</label><input id="minScore" value="0.35">
    <input id="imageSearchQuery">
  `;
  const responses = [
    { v2_retrieval_active: true, fields: [] },
    { v2_retrieval_active: false, fields: [] }
  ];
  globalThis.fetch = async () => ({ ok: true, status: 200, json: async () => responses.shift() });

  await searchModule.loadDynamicSearchFilters();
  assert.equal(document.getElementById('minScore').disabled, false);
  assert.equal(document.getElementById('dynamicSearchFilters').hidden, true);
  assert.equal(document.getElementById('minScoreLabel').textContent, '最小ベクトル類似度');

  await searchModule.loadDynamicSearchFilters();
  assert.equal(document.getElementById('minScore').disabled, false);
  assert.equal(document.getElementById('dynamicSearchFilters').hidden, true);
});

test('検索画面は共通の5カテゴリを初期選択で表示する', async () => {
  const html = await readFile(new URL('../index.html', import.meta.url), 'utf8');
  const page = new JSDOM(html);
  const fieldset = page.window.document.getElementById('searchRetrievalModes');
  const inputs = [...fieldset.querySelectorAll('input[name="retrievalMode"]')];
  const minScore = page.window.document.getElementById('minScore');

  assert.equal(fieldset.tagName, 'FIELDSET');
  assert.equal(fieldset.querySelector('legend').textContent.trim(), '検索方式');
  assert.deepEqual(inputs.map(input => input.value), [
    'visual_vector', 'oracle_text', 'text_vector', 'vlm_text', 'vlm_vector'
  ]);
  assert.equal(inputs.every(input => input.checked), true);
  assert.equal(page.window.document.getElementById('minScoreLabel').textContent, '最小ベクトル類似度');
  assert.equal(minScore.value, '0.35');
  assert.equal(minScore.getAttribute('aria-describedby'), 'minScoreHelp');
  assert.match(page.window.document.getElementById('minScoreHelp').textContent, /1 − COSINE距離/);
});

test('検索方式の利用可否と無効理由をフィルターAPIから反映する', async () => {
  document.body.innerHTML = `
    <fieldset id="dynamicSearchFilters" hidden><div id="dynamicSearchFilterFields"></div></fieldset>
    <fieldset id="searchRetrievalModes">
      ${['oracle_text', 'text_vector', 'vlm_text', 'vlm_vector', 'visual_vector'].map(value => `
        <label class="search-retrieval-mode-option"><input type="checkbox" name="retrievalMode" value="${value}" checked><span><strong></strong><small data-mode-description></small><small data-mode-status hidden></small></span></label>
      `).join('')}
    </fieldset>
    <p id="searchRetrievalModesError" hidden></p>
    <input id="imageSearchQuery">
  `;
  const options = ['oracle_text', 'text_vector', 'vlm_text', 'vlm_vector', 'visual_vector'].map(value => ({
    value,
    label: value,
    description: `${value} description`,
    available: value !== 'visual_vector',
    unavailable_reason: value === 'visual_vector' ? '管理者設定で重みが0になっています。' : null
  }));
  globalThis.fetch = async () => ({
    ok: true,
    status: 200,
    json: async () => ({ v2_retrieval_active: true, fields: [], retrieval_modes: options })
  });

  await searchModule.loadDynamicSearchFilters();

  const visual = document.querySelector('input[value="visual_vector"]');
  assert.equal(document.querySelector('input[value="oracle_text"]').checked, true);
  assert.equal(visual.disabled, true);
  assert.equal(visual.checked, false);
  assert.match(visual.closest('label').textContent, /重みが0/);
});

test('検索方式が未選択なら送信せずインラインエラーを表示する', async () => {
  document.body.innerHTML = `
    <details id="searchAdvancedSettings">
      <fieldset id="searchRetrievalModes">
        <input type="checkbox" name="retrievalMode" value="oracle_text">
        <input type="checkbox" name="retrievalMode" value="text_vector">
        <input type="checkbox" name="retrievalMode" value="vlm_text">
        <input type="checkbox" name="retrievalMode" value="vlm_vector">
        <input type="checkbox" name="retrievalMode" value="visual_vector">
      </fieldset>
      <p id="searchRetrievalModesError" role="alert" hidden></p>
    </details>
    <textarea id="searchQuery">照明</textarea>
    <input id="filenameFilter" value=""><input id="topK" value="10"><input id="minScore" value="0.35">
    <button id="textSearchSubmitBtn"><span>検索実行</span></button>
  `;
  let fetchCalls = 0;
  window.UIComponents = { showToast() {} };
  globalThis.fetch = async () => { fetchCalls += 1; throw new Error('must not fetch'); };

  await searchModule.performSearch();

  assert.equal(fetchCalls, 0);
  assert.equal(document.getElementById('searchRetrievalModes').getAttribute('aria-invalid'), 'true');
  assert.equal(document.getElementById('searchRetrievalModesError').hidden, false);
  assert.match(document.getElementById('searchRetrievalModesError').textContent, /1つ以上/);
  assert.equal(document.getElementById('searchAdvancedSettings').open, true);
});

test('検索方式の選択はタブ切替とクリア後も維持する', () => {
  document.body.innerHTML = `
    <button id="searchTypeTextTab"></button><button id="searchTypeImageTab"></button>
    <div id="textSearchPanel"></div><div id="imageSearchPanel"></div>
    <fieldset id="searchRetrievalModes"><input id="keptMode" type="checkbox" name="retrievalMode" value="oracle_text"><input id="selectedMode" type="checkbox" name="retrievalMode" value="visual_vector" checked></fieldset>
    <p id="searchRetrievalModesError" hidden></p>
    <textarea id="searchQuery">照明</textarea><input id="imageSearchQuery" value="条件"><input id="searchImageInput">
    <div id="imageSearchPreview"></div><div id="imageSearchPlaceholder"></div>
    <div id="searchResults"></div><details id="searchAgentProgress"></details>
  `;

  searchModule.switchSearchType('image');
  searchModule.clearSearchResults();

  assert.equal(document.getElementById('keptMode').checked, false);
  assert.equal(document.getElementById('selectedMode').checked, true);
});

test('共有認証ヘルパーはVITE_API_BASE向けにプロキシ接頭辞を除く', async () => {
  let requestedUrl;
  stateModule.appState.set('apiBase', 'https://api.example.test/');
  globalThis.fetch = async (url, options = {}) => {
    requestedUrl = url;
    return { ok: true, status: 200, json: async () => ({}) };
  };

  await authModule.apiCall('/ai/api/settings/retrieval');

  assert.equal(requestedUrl, 'https://api.example.test/settings/retrieval');
  stateModule.appState.set('apiBase', '');
});

test('検索タイムアウト後も入力を保持して操作状態を復元する', async () => {
  document.body.innerHTML = `
    <textarea id="searchQuery">保持する検索条件</textarea>
    <input id="filenameFilter" value="report"><input id="topK" value="10"><input id="minScore" value="0.7">
    <button id="textSearchSubmitBtn"><span>検索実行</span></button>
  `;
  let toast;
  window.UIComponents = { showToast(message, type) { toast = { message, type }; } };
  globalThis.fetch = async () => { throw new Error('リクエストがタイムアウトしました'); };

  await searchModule.performSearch();

  const button = document.getElementById('textSearchSubmitBtn');
  assert.equal(document.getElementById('searchQuery').value, '保持する検索条件');
  assert.equal(button.disabled, false);
  assert.equal(button.hasAttribute('aria-busy'), false);
  assert.equal(document.getElementById('loadingOverlay'), null);
  assert.equal(toast.type, 'error');
  assert.match(toast.message, /再度お試しください/);
});

test('AG-UI検索イベントは進捗を表示して結果を描画する', async () => {
  document.body.innerHTML = `
    <fieldset id="dynamicSearchFilters" hidden><div id="dynamicSearchFilterFields"></div></fieldset>
    <label id="minScoreLabel" for="minScore">最小ベクトル類似度</label><input id="minScore" value="0.35">
    <input id="imageSearchQuery">
    <input id="searchVlmVerify" type="checkbox">
    <fieldset id="searchRetrievalModes"><input type="checkbox" name="retrievalMode" value="oracle_text" checked><input type="checkbox" name="retrievalMode" value="visual_vector" checked></fieldset>
    <p id="searchRetrievalModesError" hidden></p>
    <textarea id="searchQuery">天井照明</textarea>
    <input id="filenameFilter" value=""><input id="topK" value="10">
    <button id="textSearchSubmitBtn"><span>検索実行</span></button>
    <details id="searchAgentProgress" hidden><summary><span id="searchAgentStatus"></span><small id="searchAgentElapsed"></small></summary><ol id="searchAgentSteps"></ol><div id="searchAgentDetails" hidden></div></details>
    <div id="searchResults" style="display:none"><span id="searchResultsSummary"></span><div id="searchResultsList"></div></div>
  `;
  window.UIComponents = { showToast() {} };
  let requestBody;
  const result = {
    success: true,
    trace_id: 'trace',
    query: '天井照明',
    results: [{ document_id: 'd1', file_name: 'lighting.pdf', object_name: 'lighting.pdf', bucket: 'bucket', score: 1, profile_slots: [], evidence: [] }],
    total_documents: 1,
    total_evidence: 0,
    processing_time: 0.12,
    diagnostics: { degraded: ['rerank'] }
  };
  const stream = [
    { type: 'RUN_STARTED' },
    { type: 'STATE_SNAPSHOT', snapshot: { status: 'started', message: '検索開始', result: null } },
    { type: 'STEP_STARTED', stepName: 'initialization', message: '検索を準備しています' },
    { type: 'STEP_FINISHED', stepName: 'initialization' },
    { type: 'STEP_STARTED', stepName: 'query_variants', message: '検索バリエーション生成' },
    { type: 'STATE_DELTA', delta: [{ op: 'replace', path: '/queryPlan', value: { variants: ['天井照明', 'ダウンライト'], query_expansion_source: 'deterministic' } }] },
    { type: 'STEP_FINISHED', stepName: 'query_variants' },
    { type: 'STEP_STARTED', stepName: 'keyword_plan', message: '検索キーワード生成' },
    { type: 'STATE_DELTA', delta: [{ op: 'replace', path: '/keywordPlan', value: { terms: ['天井', '照明', 'ダウンライト'], target: 'Oracle Text', max_terms: 20 } }] },
    { type: 'STEP_FINISHED', stepName: 'keyword_plan' },
    { type: 'STEP_STARTED', stepName: 'embedding', message: '検索ベクトルを作成しています' },
    { type: 'STEP_FINISHED', stepName: 'embedding' },
    { type: 'STEP_STARTED', stepName: 'retrieval', message: '候補取得' },
    { type: 'STATE_DELTA', delta: [{ op: 'replace', path: '/retrievalSummary', value: { channels: [{ channel: 'oracle_text', status: 'ok', count: 3, weight: 1 }] } }] },
    { type: 'STEP_FINISHED', stepName: 'retrieval' },
    { type: 'STEP_STARTED', stepName: 'candidate_merge', message: '候補統合' },
    { type: 'STATE_DELTA', delta: [{ op: 'replace', path: '/candidateMerge', value: { method: 'weighted_rrf', source_lists: 1, candidate_count: 1, limit: 100 } }] },
    { type: 'STEP_FINISHED', stepName: 'candidate_merge' },
    { type: 'STEP_STARTED', stepName: 'rerank', message: '再ランキング' },
    { type: 'STATE_DELTA', delta: [{ op: 'replace', path: '/rerankSummary', value: { enabled: true, skipped: false, candidate_count: 1, top_n: 30, degraded: true } }] },
    { type: 'STEP_FINISHED', stepName: 'rerank' },
    { type: 'STEP_STARTED', stepName: 'llm_judge', message: 'LLMが最終候補を判定しています' },
    { type: 'STEP_FINISHED', stepName: 'llm_judge' },
    { type: 'STEP_STARTED', stepName: 'format_results', message: '結果整形' },
    { type: 'STATE_DELTA', delta: [{ op: 'replace', path: '/formatSummary', value: { total_documents: 1, total_evidence: 0 } }] },
    { type: 'STEP_FINISHED', stepName: 'format_results' },
    { type: 'STATE_DELTA', delta: [{ op: 'replace', path: '/result', value: result }] },
    { type: 'RUN_FINISHED', result }
  ].map(event => `data: ${JSON.stringify(event)}\n\n`).join('');
  let searchUrl;
  globalThis.fetch = async (url, options = {}) => {
    if (String(url).endsWith('/search/v2/filters')) {
      return { ok: true, status: 200, json: async () => ({ v2_retrieval_active: true, fields: [] }) };
    }
    searchUrl = String(url);
    requestBody = JSON.parse(options.body);
    return {
      ok: true,
      status: 200,
      body: new ReadableStream({
        start(controller) {
          controller.enqueue(new TextEncoder().encode(stream));
          controller.close();
        }
      })
    };
  };

  searchModule.invalidateDynamicSearchFilters();
  await searchModule.loadDynamicSearchFilters();
  await searchModule.performSearch();

  assert.match(searchUrl, /\/ai\/api\/search\/v2\/events$/);
  assert.equal(requestBody.min_score, 0.35);
  assert.deepEqual(requestBody.retrieval_modes, ['oracle_text', 'visual_vector']);
  assert.equal(requestBody.verify, false);
  assert.equal(document.getElementById('searchAgentProgress').hidden, false);
  assert.equal(document.getElementById('searchAgentProgress').open, false);
  assert.ok(document.querySelector('#searchAgentSteps details > summary'));
  const steps = document.getElementById('searchAgentSteps').textContent;
  assert.match(steps, /検索準備/);
  assert.match(steps, /検索バリエーション生成/);
  assert.match(steps, /検索キーワード生成/);
  assert.match(steps, /候補取得/);
  assert.match(steps, /候補統合/);
  assert.match(steps, /検索バリエーション/);
  assert.match(steps, /検索キーワード/);
  assert.match(steps, /対象: Oracle Text/);
  assert.match(steps, /ダウンライト/);
  assert.match(steps, /ルールベース/);
  assert.match(steps, /weighted_rrf/);
  assert.match(document.getElementById('searchAgentDetails').textContent, /rerank/);
  assert.doesNotMatch(steps, /検索意図/);
  assert.doesNotMatch(steps, /deterministic/);
  assert.doesNotMatch(steps, /AI整理キーワード\/検索語/);
  assert.doesNotMatch(steps, /検索語:/);
  assert.doesNotMatch(steps, /詳細は処理後に表示されます/);
  const stepItems = [...document.querySelectorAll('#searchAgentSteps .search-agent-step')];
  const findStep = label => stepItems.find(item => item.textContent.includes(label));
  for (const label of ['検索準備', 'ベクトル作成', 'LLM最終判定']) {
    const item = findStep(label);
    assert.ok(item);
    assert.equal(item.querySelector('details'), null);
    assert.equal(item.querySelector('.search-agent-step-static').tabIndex, -1);
    assert.match(item.textContent, /完了/);
  }
  for (const label of ['検索バリエーション生成', '検索キーワード生成', '候補取得', '候補統合', '再ランキング', '結果整形']) {
    assert.ok(findStep(label).querySelector('details'));
  }
  assert.match(document.getElementById('searchResultsSummary').textContent, /1ファイル/);
});

test('画像検索の分割SSEはheartbeatを無視して段階進捗を描画する', async () => {
  document.body.innerHTML = `
    <fieldset id="dynamicSearchFilters" hidden><div id="dynamicSearchFilterFields"></div></fieldset>
    <input id="imageSearchQuery"><input id="searchVlmVerify" type="checkbox">
    <fieldset id="searchRetrievalModes"><input type="checkbox" name="retrievalMode" value="text_vector" checked><input type="checkbox" name="retrievalMode" value="visual_vector" checked></fieldset>
    <p id="searchRetrievalModesError" hidden></p>
    <input id="filenameFilter" value=""><input id="topK" value="10"><input id="minScore" value="0.35">
    <button id="imageSearchSubmitBtn"><span>画像検索実行</span></button>
    <details id="searchAgentProgress" hidden><summary><span id="searchAgentStatus"></span><small id="searchAgentElapsed"></small></summary><ol id="searchAgentSteps"></ol><div id="searchAgentDetails" hidden></div></details>
    <div id="searchResults" style="display:none"><span id="searchResultsSummary"></span><div id="searchResultsList"></div></div>
  `;
  window.UIComponents = { showToast() {} };
  const result = {
    success: true,
    trace_id: 'image-trace',
    query: '',
    results: [{
      document_id: 'd1',
      file_name: 'lighting.pdf',
      object_name: 'lighting.pdf',
      bucket: 'bucket',
      score: 0.01,
      rerank_score: null,
      image_similarity_score: 0.923,
      profile_slots: [],
      evidence: [{
        evidence_id: 'e1',
        page_number: 2,
        asset_url: 'lighting_page_2.png',
        score: 0.01,
        rerank_score: null,
        image_similarity_score: 0.876,
        retrieval_channels: ['vector:page_image'],
        verification_status: 'not_requested'
      }]
    }],
    total_documents: 1,
    total_evidence: 1,
    processing_time: 0.12,
    diagnostics: { degraded: [] }
  };
  const eventChunks = [
    `data: ${JSON.stringify({ type: 'RUN_STARTED' })}\n\n`,
    `data: ${JSON.stringify({ type: 'STATE_SNAPSHOT', snapshot: { status: 'started', message: '検索開始', result: null } })}\n\n`,
    `data: ${JSON.stringify({ type: 'STEP_STARTED', stepName: 'initialization', message: '検索を準備しています' })}\n\n`,
    ': heartbeat\n\n',
    `data: ${JSON.stringify({ type: 'STEP_FINISHED', stepName: 'initialization' })}\n\n`,
    `data: ${JSON.stringify({ type: 'STEP_STARTED', stepName: 'embedding', message: '検索ベクトルを作成しています' })}\n\n`,
    `data: ${JSON.stringify({ type: 'STEP_FINISHED', stepName: 'embedding' })}\n\n`,
    `data: ${JSON.stringify({ type: 'STEP_STARTED', stepName: 'format_results', message: '検索結果を整形しています' })}\n\n`,
    `data: ${JSON.stringify({ type: 'STEP_FINISHED', stepName: 'format_results' })}\n\n`,
    `data: ${JSON.stringify({ type: 'STATE_DELTA', delta: [{ op: 'replace', path: '/result', value: result }] })}\n\n`,
    `data: ${JSON.stringify({ type: 'RUN_FINISHED', result })}\n\n`
  ];
  let searchUrl;
  let requestBody;
  globalThis.fetch = async (url, options = {}) => {
    if (String(url).endsWith('/search/v2/filters')) {
      return { ok: true, status: 200, json: async () => ({ v2_retrieval_active: true, fields: [] }) };
    }
    searchUrl = String(url);
    requestBody = options.body;
    return {
      ok: true,
      status: 200,
      body: new ReadableStream({
        start(controller) {
          eventChunks.forEach(chunk => controller.enqueue(new TextEncoder().encode(chunk)));
          controller.close();
        }
      })
    };
  };

  const image = new window.File([new Uint8Array([1, 2, 3])], 'query.png', { type: 'image/png' });
  searchModule.handleSearchImageSelect({ target: { files: [image] } });
  searchModule.invalidateDynamicSearchFilters();
  await searchModule.performImageSearch();

  assert.match(searchUrl, /\/ai\/api\/search\/v2\/image\/events$/);
  assert.equal(requestBody.get('image').name, 'query.png');
  assert.equal(requestBody.get('min_score'), '0.35');
  assert.deepEqual(JSON.parse(requestBody.get('retrieval_modes')), ['text_vector', 'visual_vector']);
  assert.equal(document.getElementById('searchAgentProgress').hidden, false);
  const steps = document.getElementById('searchAgentSteps').textContent;
  assert.match(steps, /検索準備/);
  assert.match(steps, /ベクトル作成/);
  assert.match(steps, /結果整形/);
  assert.match(document.getElementById('searchResultsSummary').textContent, /1ファイル/);
  const resultText = document.getElementById('searchResultsList').textContent;
  assert.match(resultText, /画像類似度:\s*92\.3%/);
  assert.match(resultText, /画像類似度:\s*87\.6%/);
  assert.match(resultText, /画像類似度が高い順/);
  assert.doesNotMatch(resultText, /関連度:/);
  assert.doesNotMatch(resultText, /NaN/);
});

test('検索結果はアップロード用プレフィクスを隠して元ファイル名だけ表示する', async () => {
  document.body.innerHTML = `
    <div id="searchResults" style="display:none"><span id="searchResultsSummary"></span><div id="searchResultsList"></div></div>
  `;

  searchModule.displaySearchResults({
    results: [{
      file_id: 'd1',
      bucket: 'bucket',
      object_name: '20260709_215027_e18cabda_設備・内装商品カタログ2026年1月版.pdf',
      original_filename: null,
      match_percent: null,
      matched_images: []
    }],
    total_files: 1,
    total_images: 0,
    processing_time: 0.1
  });

  assert.equal(document.querySelector('.search-result-filename').textContent.trim(), '設備・内装商品カタログ2026年1月版.pdf');
  assert.equal(document.querySelector('.search-result-path'), null);
  assert.doesNotMatch(document.getElementById('searchResultsList').textContent, /20260709_215027_e18cabda_/);
  assert.match(document.getElementById('searchResultsList').textContent, /ページ単位の画像一致はありません/);
});

test('同じ案件の関連資料は小型サムネイルを表示しクリックで拡大できる', async () => {
  document.body.innerHTML = `
    <div id="searchResults" style="display:none"><span id="searchResultsSummary"></span><div id="searchResultsList"></div></div>
  `;

  searchModule.displaySearchResults({
    results: [{
      file_id: 'direct-1', bucket: 'bucket', object_name: 'direct.pdf',
      original_filename: 'direct.pdf', match_percent: 80, matched_images: [],
      group_key: 'set:set-1'
    }],
    groups: [{
      group_key: 'set:set-1', document_set_id: 'set-1', label: '案件A',
      direct_document_ids: ['direct-1'],
      related_documents: [{
        file_id: 'related-1', bucket: 'bucket', object_name: 'related.pdf',
        original_filename: '関連資料.pdf', matched_images: [],
        thumbnail_object_name: 'documents/related/page_001.png',
        thumbnail_page_number: 1
      }]
    }],
    total_groups: 1, total_files: 1, total_images: 0, processing_time: 0.1
  });

  const thumbnail = document.querySelector('.search-related-thumbnail');
  assert.ok(thumbnail);
  assert.match(thumbnail.querySelector('img').getAttribute('src'), /documents\/related\/page_001\.png/);
  searchModule.showRelatedDocumentThumbnail(0, 0);
  assert.match(document.getElementById('imageModalImg').getAttribute('src'), /documents\/related\/page_001\.png/);
  assert.match(document.getElementById('imageModalImg').getAttribute('alt'), /関連資料\.pdf/);
  document.getElementById('imageModalOverlay')?.remove();

  const css = await readFile(new URL('../src/style.css', import.meta.url), 'utf8');
  assert.match(css, /\.search-related-thumbnail \{[^}]*height: 30px/);
});

test('AI検索候補による文書全体一致は空のページ枠ではなく代表画像を表示する', () => {
  document.body.innerHTML = `
    <div id="searchResults" style="display:none"><span id="searchResultsSummary"></span><div id="searchResultsList"></div></div>
  `;

  searchModule.displaySearchResults({
    results: [{
      file_id: 'concept-document', bucket: 'bucket', object_name: 'proposal.pdf',
      original_filename: '提案資料.pdf', match_percent: 84.2, matched_images: [],
      matched_concept_ids: ['concept-kitchen'],
      thumbnail_object_name: 'documents/proposal/page_001.png',
      thumbnail_page_number: 1
    }],
    total_files: 1, total_images: 0, processing_time: 0.1
  });

  const card = document.querySelector('.search-result-card');
  assert.match(card.textContent, /文書全体一致/);
  assert.match(card.textContent, /文書の代表画像/);
  assert.doesNotMatch(card.textContent, /マッチしたページ画像/);
  const representative = card.querySelector('.search-result-representative-card img');
  assert.match(representative.getAttribute('src'), /documents\/proposal\/page_001\.png/);

  searchModule.showSearchRepresentativeImage(0);
  assert.match(document.getElementById('imageModalImg').getAttribute('src'), /documents\/proposal\/page_001\.png/);
  assert.match(document.getElementById('imageModalImg').getAttribute('alt'), /提案資料\.pdf/);
  document.getElementById('imageModalOverlay')?.remove();
});

test('検索結果がない場合は最小ベクトル類似度を下げるよう案内する', async () => {
  document.body.innerHTML = `
    <input id="minScore" value="0.55">
    <div id="searchResults" style="display:none"><span id="searchResultsSummary"></span><div id="searchResultsList"></div></div>
  `;

  searchModule.displaySearchResults({ results: [] });

  assert.equal(document.getElementById('searchResultsSummary').textContent, '検索結果なし');
  assert.match(document.getElementById('searchResultsList').textContent, /最小ベクトル類似度（現在 0\.55）を下げる/);
});

test('画像類似度と関連度を別々に表示し、無いスコアのバッジは出さない', async () => {
  document.body.innerHTML = `
    <div id="searchResults" style="display:none"><span id="searchResultsSummary"></span><div id="searchResultsList"></div></div>
  `;

  searchModule.displaySearchResults({
    result_order: 'image_similarity',
    results: [{
      file_id: 'd1',
      bucket: 'bucket',
      object_name: 'a.pdf',
      original_filename: 'a.pdf',
      match_percent: 87.3,
      image_similarity_percent: 91.2,
      matched_images: [{
        embed_id: 'e1',
        bucket: 'bucket',
        object_name: 'a_p3.png',
        page_number: 3,
        match_percent: 87.3,
        image_similarity_percent: 84.6,
        url: '/ai/api/object/bucket/a_p3.png'
      }]
    }, {
      file_id: 'd2',
      bucket: 'bucket',
      object_name: 'b.pdf',
      original_filename: 'b.pdf',
      match_percent: null,
      image_similarity_percent: null,
      matched_images: []
    }],
    total_files: 2,
    total_images: 1,
    processing_time: 0.1
  });

  const text = document.getElementById('searchResultsList').textContent;
  assert.match(text, /関連度: 87\.3%/);
  assert.match(text, /画像類似度: 91\.2%/);
  assert.match(text, /画像類似度: 84\.6%/);
  assert.match(text, /画像類似度が高い順/);
  assert.doesNotMatch(text, /マッチ度/);
  assert.doesNotMatch(text, /距離:/);
  const cards = document.querySelectorAll('.search-result-card');
  assert.doesNotMatch(cards[1].textContent, /関連度:/);
  assert.doesNotMatch(cards[1].textContent, /画像類似度:/);
});

test('AG-UI検索エラー後も入力と操作状態を保持する', async () => {
  document.body.innerHTML = `
    <fieldset id="dynamicSearchFilters" hidden><div id="dynamicSearchFilterFields"></div></fieldset>
    <label id="minScoreLabel" for="minScore">最小ベクトル類似度</label><input id="minScore">
    <input id="imageSearchQuery">
    <input id="searchVlmVerify" type="checkbox">
    <textarea id="searchQuery">保持する検索条件</textarea>
    <input id="filenameFilter" value="report"><input id="topK" value="10">
    <button id="textSearchSubmitBtn"><span>検索実行</span></button>
    <details id="searchAgentProgress" hidden><summary><span id="searchAgentStatus"></span><small id="searchAgentElapsed"></small></summary><ol id="searchAgentSteps"></ol><div id="searchAgentDetails" hidden></div></details>
  `;
  let toast;
  window.UIComponents = { showToast(message, type) { toast = { message, type }; } };
  const stream = [
    { type: 'RUN_STARTED' },
    { type: 'STATE_SNAPSHOT', snapshot: { status: 'started', message: '検索開始', result: null } },
    { type: 'RUN_ERROR', message: '検索がタイムアウトしました' }
  ].map(event => `data: ${JSON.stringify(event)}\n\n`).join('');
  globalThis.fetch = async url => String(url).endsWith('/search/v2/filters')
    ? { ok: true, status: 200, json: async () => ({ v2_retrieval_active: true, fields: [] }) }
    : {
        ok: true,
        status: 200,
        body: new ReadableStream({
          start(controller) {
            controller.enqueue(new TextEncoder().encode(stream));
            controller.close();
          }
        })
      };

  searchModule.invalidateDynamicSearchFilters();
  await searchModule.loadDynamicSearchFilters();
  await searchModule.performSearch();

  const button = document.getElementById('textSearchSubmitBtn');
  assert.equal(document.getElementById('searchQuery').value, '保持する検索条件');
  assert.equal(button.disabled, false);
  assert.equal(button.hasAttribute('aria-busy'), false);
  assert.equal(document.getElementById('searchAgentProgress').open, false);
  assert.equal(toast.type, 'error');
  assert.match(toast.message, /再度お試しください/);
});

test('検索中ボタンはキャンセルになり経過時間をローカル更新する', async () => {
  document.body.innerHTML = `
    <fieldset id="dynamicSearchFilters" hidden><div id="dynamicSearchFilterFields"></div></fieldset>
    <label id="minScoreLabel" for="minScore">最小ベクトル類似度</label><input id="minScore">
    <input id="imageSearchQuery">
    <input id="searchVlmVerify" type="checkbox" checked>
    <textarea id="searchQuery">保持する検索条件</textarea>
    <input id="filenameFilter" value=""><input id="topK" value="10">
    <button id="textSearchSubmitBtn"><span>検索実行</span></button>
    <details id="searchAgentProgress" hidden><summary><span id="searchAgentStatus"></span><small id="searchAgentElapsed"></small></summary><ol id="searchAgentSteps"></ol><div id="searchAgentDetails" hidden></div></details>
  `;
  const originalSetInterval = globalThis.setInterval;
  const originalClearInterval = globalThis.clearInterval;
  const originalNow = Date.now;
  let intervalCallback;
  let requestBody;
  let streamController;
  let aborted = false;
  let now = 1000;
  globalThis.setInterval = callback => {
    intervalCallback = callback;
    return 1;
  };
  globalThis.clearInterval = () => {};
  Date.now = () => now;
  window.UIComponents = { showToast() {} };
  globalThis.fetch = async (url, options = {}) => {
    if (String(url).endsWith('/search/v2/filters')) {
      return { ok: true, status: 200, json: async () => ({ v2_retrieval_active: true, fields: [] }) };
    }
    requestBody = JSON.parse(options.body);
    options.signal?.addEventListener('abort', () => {
      aborted = true;
      streamController?.error(new Error('aborted'));
    });
    return {
      ok: true,
      status: 200,
      body: new ReadableStream({
        start(controller) {
          streamController = controller;
          controller.enqueue(new TextEncoder().encode(`data: ${JSON.stringify({ type: 'RUN_STARTED' })}\n\n`));
          controller.enqueue(new TextEncoder().encode(`data: ${JSON.stringify({ type: 'STEP_STARTED', stepName: 'initialization', message: '検索を準備しています' })}\n\n`));
        }
      })
    };
  };

  try {
    searchModule.invalidateDynamicSearchFilters();
    const searchPromise = searchModule.performSearch();
    for (let attempt = 0; attempt < 10 && !intervalCallback; attempt += 1) {
      await new Promise(resolve => setTimeout(resolve, 0));
    }

    now = 2500;
    intervalCallback();
    assert.equal(requestBody.verify, true);
    assert.match(document.getElementById('textSearchSubmitBtn').textContent, /キャンセル/);
    assert.match(document.getElementById('searchAgentElapsed').textContent, /1\.5秒/);
    const runningStep = document.querySelector('#searchAgentSteps .search-agent-step');
    assert.match(runningStep.textContent, /検索準備/);
    assert.match(runningStep.textContent, /処理中/);
    assert.equal(runningStep.querySelector('details'), null);
    assert.equal(runningStep.querySelector('.search-agent-step-static').tabIndex, -1);
    assert.doesNotMatch(runningStep.textContent, /詳細は処理後に表示されます/);

    await searchModule.performSearch();
    await searchPromise;

    assert.equal(aborted, true);
    assert.equal(document.getElementById('searchQuery').value, '保持する検索条件');
    assert.match(document.getElementById('textSearchSubmitBtn').textContent, /検索実行/);
  } finally {
    globalThis.setInterval = originalSetInterval;
    globalThis.clearInterval = originalClearInterval;
    Date.now = originalNow;
  }
});

test('検索とアップロードの進捗は入力カードと結果カードの間に置く', async () => {
  const html = await readFile(new URL('../index.html', import.meta.url), 'utf8');
  const searchHeader = html.indexOf('セマンティック検索');
  const verify = html.indexOf('id="searchVlmVerify"');
  const textPanel = html.indexOf('id="textSearchPanel"');
  const searchProgress = html.indexOf('id="searchAgentProgress"');
  const searchResults = html.indexOf('id="searchResults"');
  const progressMarkup = html.slice(
    searchProgress,
    searchResults
  );
  const uploadHeader = html.indexOf('文書アップロード');
  const uploadButton = html.indexOf('id="uploadMultipleBtn"');
  const uploadProgress = html.indexOf('id="uploadProgress"');
  const processProgress = html.match(/<details id="processProgress"[^>]+>/)?.[0] || '';
  const documentsHeader = html.indexOf('登録済み文書');

  assert.ok(searchHeader < textPanel && textPanel < verify);
  assert.ok(textPanel < searchProgress && searchProgress < searchResults);
  assert.doesNotMatch(progressMarkup, /cancelCurrentSearch|キャンセル/);
  assert.match(progressMarkup, /class="search-agent-progress retrieval-global-section"[\s\S]*<summary>/);
  assert.match(html, /VLM精密確認（時間がかかります）/);
  assert.ok(uploadHeader < uploadButton && uploadButton < uploadProgress && uploadProgress < documentsHeader);
  assert.match(processProgress, /style="margin: 16px 0 24px;"/);
});

test('確認操作は共通モーダルを経由する', async () => {
  let options;
  window.UIComponents = {
    showModal(received) {
      options = received;
      received.onConfirm();
    }
  };

  const confirmed = await utilsModule.showConfirmModal('公開しますか', '公開確認', {
    variant: 'warning',
    confirmText: '公開'
  });

  assert.equal(confirmed, true);
  assert.equal(options.title, '公開確認');
  assert.equal(options.variant, 'warning');
  const source = await readFile(new URL('../src/modules/retrieval-settings.js', import.meta.url), 'utf8');
  assert.doesNotMatch(source, /window\.confirm|\bconfirm\s*\(/);
  assert.match(source, /utilsShowConfirmModal/);
});

test('設定タブはDB・検索・OCIの順で、再ランキングはOCI設定の末尾に置く', async () => {
  const html = await readFile(new URL('../index.html', import.meta.url), 'utf8');
  const app = await readFile(new URL('../app.js', import.meta.url), 'utf8');
  const source = await readFile(new URL('../src/modules/retrieval-settings.js', import.meta.url), 'utf8');
  const retrieval = html.indexOf('id="admin-tab-retrieval"');
  const oci = html.indexOf('id="admin-tab-settings"');
  const database = html.indexOf('id="admin-tab-database"');
  const enterpriseAi = html.indexOf('id="enterpriseAiModel"');
  const rerank = html.indexOf('id="rerankSettingsRoot"');
  const databasePanel = html.indexOf('id="tab-database"');
  const globalPanels = source.slice(source.indexOf('function renderGlobalPanels'), source.indexOf('function renderRerankSettings'));

  assert.ok(database < retrieval && retrieval < oci);
  assert.ok(enterpriseAi < rerank && rerank < databasePanel);
  assert.doesNotMatch(globalPanels, /OCIテキスト再ランキング/);
  assert.match(app, /loadOciSettings\(\), loadRerankSettings\(\)/);
  assert.doesNotMatch(source, /max_chunks_per_document|max_tokens_per_document|rerank-chunks|rerank-tokens/);
});

test('問い合わせ整理を持たずLLM検索バリエーションと画像確認を設定する', async () => {
  const source = await readFile(new URL('../src/modules/retrieval-settings.js', import.meta.url), 'utf8');
  const models = await readFile(new URL('../../backend/app/rag/models.py', import.meta.url), 'utf8');
  const globalPanels = source.slice(source.indexOf('function renderGlobalPanels'), source.indexOf('function renderRerankSettings'));
  const saveHandlersStart = source.indexOf("else if (action === 'save-mineru')");
  const saveHandlers = source.slice(saveHandlersStart, source.indexOf('} catch (error)', saveHandlersStart));

  assert.match(models, /class QueryExpansionSettings/);
  assert.doesNotMatch(models, /query_enabled|query_prompt/);
  assert.doesNotMatch(globalPanels, /OCI Enterprise AI モデル|retrieval-model-status/);
  assert.match(globalPanels, /MinerUで内容を取得できないページで使用する/);
  assert.match(globalPanels, /検索バリエーション/);
  assert.match(globalPanels, /原文のみ/);
  assert.match(globalPanels, /ルールベース/);
  assert.match(globalPanels, /query-expansion-mode-llm/);
  assert.match(globalPanels, /enabled: false, llm_enabled: false/);
  assert.match(globalPanels, /ルールベース同義語/);
  assert.match(globalPanels, /LLM検索バリエーションの指示/);
  assert.match(globalPanels, /VLMの画像確認/);
  assert.doesNotMatch(globalPanels, /問い合わせ整理/);
  assert.doesNotMatch(globalPanels, /画像確認を使用する|vlm-verify-enabled/);
  assert.match(globalPanels, /検索画面の「VLM精密確認」/);
  assert.match(saveHandlers, /save-query-expansion/);
  assert.doesNotMatch(globalPanels, /低テキストページ/);
  assert.doesNotMatch(saveHandlers, /\brender\(\)/);
});

test('検索バリエーション設定はUIから保存できる', async () => {
  document.body.innerHTML = '<div id="retrievalSettingsRoot"></div>';
  window.UIComponents = { showToast() {} };
  const engine = { enabled: true, base_url: 'http://ocr.test/v1', model: 'model', api_key: '', dpi: 200, workers: 1 };
  const settings = {
    schema_ready: true,
    profiles: [1, 2, 3].map(slot_no => ({ slot_no, name: `Profile ${slot_no}`, enabled: slot_no === 1, extraction_prompt: 'Extract facts', apply_status: 'READY', pending_document_count: 0 })),
    mineru: { enabled: true, base_url: 'http://mineru.test', timeout_seconds: 1800 },
    ocr: { enabled: true, dots: engine, glm: engine, unlimited: engine },
    rerank: { enabled: true, model: 'rerank', candidate_count: 100, top_n: 30 },
    vlm: { verify_prompt: 'Verify' },
    query_expansion: { enabled: true, llm_enabled: false, max_variants: 3, llm_prompt: 'Expand', synonym_groups: [['浴室換気乾燥機', '浴乾']] },
    weights: { oracle_text: 1, text_vector: 1, visual_vector: 1, vlm_text: 1, vlm_vector: 1 },
    vlm_model: 'vlm'
  };
  let savedBody;
  let resolveSave;
  const saved = new Promise(resolve => { resolveSave = resolve; });
  globalThis.fetch = async (url, options = {}) => {
    if (String(url).endsWith('/query-expansion') && options.method === 'PUT') {
      savedBody = JSON.parse(options.body);
      resolveSave();
      return { ok: true, status: 200, json: async () => savedBody };
    }
    return { ok: true, status: 200, json: async () => settings };
  };

  await retrievalSettingsModule.loadRetrievalSettings();
  document.getElementById('query-expansion-mode-llm').checked = true;
  document.getElementById('query-expansion-max').value = '4';
  document.getElementById('query-expansion-llm-prompt').value = 'Create variants';
  document.getElementById('query-expansion-synonyms').value = '浴室換気乾燥機, 浴乾\n200V\n1室換気、1室';
  document.querySelector('[data-action="save-query-expansion"]').click();
  await saved;

  assert.equal(savedBody.enabled, true);
  assert.equal(savedBody.llm_enabled, true);
  assert.equal(savedBody.max_variants, 4);
  assert.equal(savedBody.llm_prompt, 'Create variants');
  assert.deepEqual(savedBody.synonym_groups, [['浴室換気乾燥機', '浴乾'], ['1室換気', '1室']]);
});

test('検索バリエーションは原文のみモードを保存できる', async () => {
  document.body.innerHTML = '<div id="retrievalSettingsRoot"></div>';
  window.UIComponents = { showToast() {} };
  const engine = { enabled: true, base_url: 'http://ocr.test/v1', model: 'model', api_key: '', dpi: 200, workers: 1 };
  const settings = {
    schema_ready: true,
    profiles: [1, 2, 3].map(slot_no => ({ slot_no, name: `Profile ${slot_no}`, enabled: slot_no === 1, extraction_prompt: 'Extract facts', apply_status: 'READY', pending_document_count: 0 })),
    mineru: { enabled: true, base_url: 'http://mineru.test', timeout_seconds: 1800 },
    ocr: { enabled: true, dots: engine, glm: engine, unlimited: engine },
    rerank: { enabled: true, model: 'rerank', candidate_count: 100, top_n: 30 },
    vlm: { verify_prompt: 'Verify' },
    query_expansion: { enabled: true, llm_enabled: true, max_variants: 3, llm_prompt: 'Expand', synonym_groups: [['浴室換気乾燥機', '浴乾']] },
    weights: { oracle_text: 1, text_vector: 1, visual_vector: 1, vlm_text: 1, vlm_vector: 1 },
    vlm_model: 'vlm'
  };
  let savedBody;
  let resolveSave;
  const saved = new Promise(resolve => { resolveSave = resolve; });
  globalThis.fetch = async (url, options = {}) => {
    if (String(url).endsWith('/query-expansion') && options.method === 'PUT') {
      savedBody = JSON.parse(options.body);
      resolveSave();
      return { ok: true, status: 200, json: async () => savedBody };
    }
    return { ok: true, status: 200, json: async () => settings };
  };

  await retrievalSettingsModule.loadRetrievalSettings();
  document.getElementById('query-expansion-mode-off').checked = true;
  document.querySelector('[data-action="save-query-expansion"]').click();
  await saved;

  assert.equal(savedBody.enabled, false);
  assert.equal(savedBody.llm_enabled, false);
});

test('ステータスのポーリングは再描画せず、開いた詳細と編集内容を保持する', async () => {
  const source = await readFile(new URL('../src/modules/retrieval-settings.js', import.meta.url), 'utf8');
  const poll = source.slice(source.indexOf('function scheduleStatusRefresh'), source.indexOf('function bindEvents'));

  assert.doesNotMatch(poll, /\brender\(\)/);
  assert.match(poll, /apply_status/);
  assert.match(poll, /retrieval-pending-summary/);
});

test('VLMプロファイルは抽出プロンプト以外の検索設定を持たない', async () => {
  const source = await readFile(new URL('../src/modules/retrieval-settings.js', import.meta.url), 'utf8');
  const profilePanel = source.slice(source.indexOf('function renderProfilePanel'), source.indexOf('function engineCard'));
  const profileTabs = source.slice(source.indexOf('function render()'), source.indexOf('function collectEngine'));

  assert.match(profilePanel, /抽出したい内容/);
  assert.match(profilePanel, /<details class="retrieval-test-details">\s*<summary>テスト用の画像（任意）<\/summary>/);
  assert.match(profilePanel, /保存して反映/);
  assert.match(profilePanel, /テスト用の画像（任意）/);
  assert.match(profilePanel, /border-2 border-dashed border-gray-300/);
  assert.match(profilePanel, /handleDropForInput\(event, 'profile-test-image'\)/);
  assert.match(profilePanel, /profile-test-image-name/);
  assert.match(profilePanel, /profile-test-file-name/);
  assert.match(profilePanel, /profile-test-page-text/);
  assert.match(profilePanel, /元ファイル名/);
  assert.match(profilePanel, /ページテキスト（任意）/);
  assert.ok(profilePanel.indexOf('profile-enabled') < profilePanel.indexOf('profile-prompt'));
  assert.doesNotMatch(profilePanel, /profile-name|profile-test-text|VLM抽出プロファイル|最終反映/);
  assert.doesNotMatch(profileTabs, /<small>|使用中|停止中/);
  assert.match(source, /if \(action === 'test-profile'.*utilsShowToast\('テスト用の画像を選択してください', 'warning'\);\s*return;/s);
  assert.ok(source.indexOf("utilsShowToast('テスト用の画像を選択してください'") < source.indexOf('utilsShowLoading(', source.indexOf('function bindEvents')));
  assert.match(source, /image_media_type: image\.type/);
  assert.match(source, /file_name: document\.getElementById\('profile-test-file-name'\)/);
  assert.match(source, /page_text: document\.getElementById\('profile-test-page-text'\)/);
  assert.match(source, /result\.empty_output/);
  assert.doesNotMatch(profilePanel, /mineru-enabled|ocr-enabled|ocr-dots|oracle_text|text_vector|visual_vector|profile-weight|対象範囲|フィールド定義|関係定義|同義語/);
});

test('検索ルート重みは相対倍率として表示する', async () => {
  const source = await readFile(new URL('../src/modules/retrieval-settings.js', import.meta.url), 'utf8');
  const weightPanel = source.slice(
    source.indexOf('<section class="retrieval-card"><h3>検索ルートの重み'),
    source.indexOf('data-action="save-weights"')
  );

  assert.match(weightPanel, /合計1不要/);
  assert.match(weightPanel, /0で無効/);
  assert.match(weightPanel, /有効Profile数で配分/);
  assert.ok(weightPanel.indexOf("['visual_vector'") < weightPanel.indexOf("['oracle_text'"));
  assert.ok(weightPanel.indexOf("['oracle_text'") < weightPanel.indexOf("['text_vector'"));
  assert.ok(weightPanel.indexOf("['text_vector'") < weightPanel.indexOf("['vlm_text'"));
  assert.ok(weightPanel.indexOf("['vlm_text'") < weightPanel.indexOf("['vlm_vector'"));
});

test('Embeddingレシピの設定UIを提供しない', async () => {
  const source = await readFile(new URL('../src/modules/retrieval-settings.js', import.meta.url), 'utf8');

  assert.doesNotMatch(source, /Embeddingレシピ/);
  assert.doesNotMatch(source, /embedding-recipes/);
  assert.doesNotMatch(source, /embedding_recipes/);
});

test('長時間処理のheartbeatは進捗UIを更新する', async () => {
  const app = await readFile(new URL('../app.js', import.meta.url), 'utf8');
  const documentModule = await readFile(new URL('../src/modules/document.js', import.meta.url), 'utf8');
  const uploadStream = app.slice(app.indexOf('async function processUploadStreamingResponse'), app.indexOf('function updateFileUploadStatus'));
  const documentStream = documentModule.slice(documentModule.indexOf('async function processStreamingResponse'), documentModule.indexOf('function updateLoadingMessage'));

  assert.match(uploadStream, /case 'heartbeat':/);
  assert.match(uploadStream, /updateFileUploadStatus/);
  assert.match(uploadStream, /updateUploadOverallStatus/);
  assert.match(documentStream, /case 'heartbeat':/);
  assert.match(documentStream, /updateProcessProgressUI/);
  assert.match(documentStream, /updateLoadingMessage/);
  assert.match(documentStream, /索引処理 \$\{currentPageIndex\}\/\$\{totalPages\}/);
  assert.doesNotMatch(documentStream, /ページ \$\{currentPageIndex\}\/\$\{totalPages\} をベクトル化中/);
});

test('接続テストのエラーは操作したサービスカード内に表示する', async () => {
  document.body.innerHTML = '<div id="retrievalSettingsRoot"></div>';
  window.UIComponents = { showToast() {} };
  const engine = { enabled: true, base_url: 'http://ocr.test/v1', model: 'model', api_key: '', dpi: 200, workers: 1 };
  const settings = {
    schema_ready: true,
    profiles: [1, 2, 3].map(slot_no => ({ slot_no, name: `Profile ${slot_no}`, enabled: slot_no === 1, extraction_prompt: 'Extract facts', apply_status: 'READY', pending_document_count: 0 })),
    mineru: { enabled: true, base_url: 'http://mineru.test', timeout_seconds: 1800 },
    ocr: { enabled: true, dots: engine, glm: engine, unlimited: engine },
    rerank: { enabled: true, model: 'rerank', candidate_count: 100, top_n: 30 },
    vlm: { verify_prompt: 'Verify' },
    weights: { oracle_text: 1, text_vector: 1, visual_vector: 1, vlm_text: 1, vlm_vector: 1 },
    vlm_model: 'vlm'
  };
  globalThis.fetch = async url => String(url).endsWith('/ocr/test/dots')
    ? { ok: false, status: 502, json: async () => ({ detail: 'Dots connection failed' }) }
    : { ok: true, status: 200, json: async () => settings };

  await retrievalSettingsModule.loadRetrievalSettings();
  document.querySelector('[data-action="test-ocr"][data-engine="dots"]').click();
  await new Promise(resolve => setTimeout(resolve, 0));

  const dotsCard = document.querySelector('[data-ocr-engine="dots"]');
  assert.equal(dotsCard.querySelector('.retrieval-inline-error').textContent, 'Dots connection failed');
  assert.equal(document.getElementById('profile-inline-error').hidden, true);
});

test('OCR設定保存は主スイッチと各エンジンのfalseを送る', async () => {
  document.body.innerHTML = '<div id="retrievalSettingsRoot"></div>';
  window.UIComponents = { showToast() {} };
  const engine = { enabled: true, base_url: 'http://ocr.test/v1', model: 'model', api_key: '', dpi: 200, workers: 1 };
  const settings = {
    schema_ready: true,
    profiles: [1, 2, 3].map(slot_no => ({ slot_no, name: `Profile ${slot_no}`, enabled: slot_no === 1, extraction_prompt: 'Extract facts', apply_status: 'READY', pending_document_count: 0 })),
    mineru: { enabled: true, base_url: 'http://mineru.test', timeout_seconds: 1800 },
    ocr: { enabled: true, dots: engine, glm: engine, unlimited: engine },
    rerank: { enabled: true, model: 'rerank', candidate_count: 100, top_n: 30 },
    vlm: { verify_prompt: 'Verify' },
    weights: { oracle_text: 1, text_vector: 1, visual_vector: 1, vlm_text: 1, vlm_vector: 1 },
    vlm_model: 'vlm'
  };
  let savedBody;
  let resolveSave;
  const saved = new Promise(resolve => { resolveSave = resolve; });
  globalThis.fetch = async (url, options = {}) => {
    if (String(url).endsWith('/ocr') && options.method === 'PUT') {
      savedBody = JSON.parse(options.body);
      resolveSave();
      return { ok: true, status: 200, json: async () => savedBody };
    }
    return { ok: true, status: 200, json: async () => settings };
  };

  await retrievalSettingsModule.loadRetrievalSettings();
  for (const id of ['ocr-enabled', 'ocr-dots-enabled', 'ocr-glm-enabled', 'ocr-unlimited-enabled']) {
    document.getElementById(id).checked = false;
  }
  document.querySelector('[data-action="save-ocr"]').click();
  await saved;

  assert.equal(savedBody.enabled, false);
  assert.equal(savedBody.dots.enabled, false);
  assert.equal(savedBody.glm.enabled, false);
  assert.equal(savedBody.unlimited.enabled, false);
});

test('起動時のフィルター取得は認証確認後に限定される', async () => {
  const source = await readFile(new URL('../app.js', import.meta.url), 'utf8');
  const authCheck = source.indexOf('await authCheckLoginStatus()');
  const filterLoad = source.indexOf('await loadDynamicSearchFilters()', authCheck);
  assert.ok(authCheck >= 0 && filterLoad > authCheck);
  assert.match(source.slice(authCheck, filterLoad), /isLoggedIn.*requireLogin/s);
});

test('システムテーブルの初期化状態をDB管理画面に表示する', async () => {
  document.body.innerHTML = `
    <span id="systemTablesStatusBadge"></span>
    <div id="systemTablesSummary"></div>
  `;
  globalThis.fetch = async () => ({
    ok: true,
    status: 200,
    json: async () => ({
      success: true,
      status: 'ready',
      existing_count: 19,
      total_count: 19,
      missing_tables: []
    })
  });

  const result = await dbModule.loadSystemTableStatus();

  assert.equal(result.status, 'ready');
  assert.equal(document.getElementById('systemTablesStatusBadge').textContent, '初期化済み');
  assert.match(document.getElementById('systemTablesSummary').textContent, /19\/19/);
});

test('システムテーブル再作成は共通確認とサーバー側確認語を使う', async () => {
  const source = await readFile(new URL('../src/modules/db.js', import.meta.url), 'utf8');

  assert.doesNotMatch(source, /window\.confirm|\bconfirm\s*\(/);
  assert.match(source, /utilsShowConfirmModal/);
  assert.match(source, /confirmation=RECREATE/);
});

test('テーブル一覧の統計更新は長時間処理として待つ', async () => {
  const source = await readFile(new URL('../src/modules/document.js', import.meta.url), 'utf8');
  const refresh = source.slice(source.indexOf('export async function refreshDbTables'));

  assert.match(refresh, /await loadDbTables\(\)/);
  assert.match(refresh, /tables\/refresh-statistics/);
  assert.match(refresh, /timeout:\s*180000/);
});

test('Object Storage一覧は既定の10秒で中断しない', async () => {
  const source = await readFile(new URL('../src/modules/document.js', import.meta.url), 'utf8');
  const load = source.slice(
    source.indexOf('export async function loadOciObjects'),
    source.indexOf('export function displayOciObjectsList')
  );

  assert.match(load, /authApiCall\(`\/ai\/api\/oci\/objects\?\$\{params\}`,\s*\{\s*timeout:\s*180000\s*\}\)/s);
});

test('文書管理は一括処理と独立ステージを同じ操作群に表示する', async () => {
  const source = await readFile(new URL('../src/modules/document.js', import.meta.url), 'utf8');
  const entrypoint = await readFile(new URL('../app.js', import.meta.url), 'utf8');
  const styles = await readFile(new URL('../src/style.css', import.meta.url), 'utf8');
  assert.match(source, /すべて処理 \(\$\{selectedOciObjects\.length\}件\)/);
  for (const label of ['ページ画像を再生成', '前処理・解析', 'VLMを再実行', 'Embeddingを再生成', '検索へ反映']) {
    assert.match(source, new RegExp(label));
  }
  assert.doesNotMatch(source, /OCRを再実行/);
  assert.ok(
    source.indexOf('ページ画像を再生成') < source.indexOf('前処理・解析'),
    'ページ画像を再生成が前処理・解析より先に表示される'
  );
  const pipelineSource = await readFile(new URL('../src/modules/pipeline.js', import.meta.url), 'utf8');
  assert.match(pipelineSource, /'PREPROCESS'[\s\S]*?\{ kind: 'NATIVE_PARSE' \}, \{ kind: 'OCR' \},\s*\{ kind: 'NORMALIZE' \}/);
  assert.doesNotMatch(pipelineSource, /'PREPROCESS'[\s\S]{0,200}\{ kind: 'RENDER' \}/);
  assert.match(source, /role="menu"/);
  assert.match(source, /aria-expanded/);
  assert.doesNotMatch(source, /pipeline-stage-menu-group/);
  assert.doesNotMatch(styles, /pipeline-stage-menu-group/);
  assert.match(source, /\/page-images\?release=/);
  assert.match(source, /page-image-child-row/);
  // 子行はサムネイルクリックでプレビュー（専用リンクなし）、ステータスはファイル行に集約（子行バッジなし）
  assert.doesNotMatch(source, /page-image-preview-link/);
  assert.doesNotMatch(source, /pageImageStageBadge|pageImageReleaseBadge/);
  assert.match(styles, /\.page-image-expand-button\[aria-expanded=true\]/);
  assert.doesNotMatch(source, /\/oci\/objects\/convert-to-images/);
  assert.doesNotMatch(entrypoint, /isGeneratedPageImage|convertSelectedOciObjectsToImages|convertToImages/);
});

test('パイプラインタスクは永続IDを復元し、キャンセルと失敗項目再試行を提供する', async () => {
  const source = await readFile(new URL('../src/modules/pipeline.js', import.meta.url), 'utf8');
  const auth = await readFile(new URL('../src/modules/auth.js', import.meta.url), 'utf8');
  assert.match(source, /sdsPipelineJobIds/);
  assert.match(source, /restorePipelineJobs/);
  assert.match(source, /\/pipeline\/jobs\/.*\/cancel/);
  assert.match(source, /\/pipeline\/jobs\/.*\/retry/);
  assert.match(source, /Idempotency-Key/);
  assert.match(source, /poll_error/);
  assert.doesNotMatch(source, /status: 'FAILED', failed_steps: 1/);
  assert.match(source, /job\.job_ids/);
  assert.match(auth, /window\.pipelineModule\?\.restore/);
});

test('失敗タスクを確認済みにすると保存一覧から削除して次回復元しない', async () => {
  document.body.innerHTML = '';
  localStorage.setItem('loginToken', 'token');
  localStorage.setItem('sdsPipelineJobIds', JSON.stringify(['job-failed']));
  window.UIComponents = { showToast() {} };
  let fetchCount = 0;
  globalThis.fetch = async url => {
    fetchCount += 1;
    assert.match(String(url), /\/pipeline\/jobs\/job-failed$/);
    return {
      ok: true,
      status: 200,
      json: async () => ({
        job_id: 'job-failed',
        status: 'FAILED',
        total_steps: 1,
        completed_steps: 0,
        failed_steps: 1,
        steps: [{
          object_name: 'photo.jpg',
          component_key: 'publish',
          error_summary: '未実行: mineru_parse'
        }]
      })
    };
  };

  await pipelineModule.restorePipelineJobs();
  const acknowledge = [...document.querySelectorAll('[data-pipeline-action="acknowledge"]')][0];
  assert.ok(acknowledge);
  acknowledge.click();
  await new Promise(resolve => setTimeout(resolve, 0));

  assert.equal(localStorage.getItem('sdsPipelineJobIds'), '[]');
  assert.equal(document.getElementById('pipelineJobTray').hidden, true);
  await pipelineModule.restorePipelineJobs();
  assert.equal(fetchCount, 1);
  window.UIComponents = undefined;
});

test('生成する両方のNginx API設定はSSEレスポンスをバッファしない', async () => {
  const source = await readFile(new URL('../../init_script.sh', import.meta.url), 'utf8');
  const apiLocations = [
    ...source.matchAll(/location \/ai\/api\/ \{([\s\S]*?)\n    \}/g)
  ].map(match => match[1]);

  assert.equal(apiLocations.length, 2);
  for (const location of apiLocations) {
    assert.match(location, /proxy_buffering off;/);
    assert.match(location, /proxy_cache off;/);
  }
});

test("登録済み文書の手動操作は再取得ではなく更新と表示する", async () => {
  const html = await readFile(new URL("../index.html", import.meta.url), "utf8");
  const source = await readFile(new URL("../src/modules/document-library.js", import.meta.url), "utf8");
  const app = await readFile(new URL("../app.js", import.meta.url), "utf8");
  const refreshButton = html.match(/<button[^>]+onclick="refreshDocumentsWithNotification\(\)"[\s\S]*?<\/button>/)?.[0] || "";

  assert.match(refreshButton, /> 更新\s*<\/button>/);
  assert.doesNotMatch(refreshButton, /再取得/);
  assert.match(source, /文書一覧を更新しました/);
  assert.match(source, /文書一覧の更新に失敗しました/);
  assert.match(app, /登録済み文書の「更新」を押してください/);
});

test('登録済み文書は表示中の選択・一括移動・完全削除・FULL再処理を提供する', async () => {
  const source = await readFile(new URL('../src/modules/document-library.js', import.meta.url), 'utf8');
  const styles = await readFile(new URL('../src/style.css', import.meta.url), 'utf8');

  assert.match(source, /id="librarySelectAll"/);
  assert.match(source, /data-library-document-checkbox/);
  assert.match(source, /bulkMoveSelectedDocuments/);
  assert.match(source, /bulkDeleteSelectedDocuments/);
  assert.match(source, /bulkReprocessSelectedDocuments/);
  assert.match(source, /mode:\s*'FULL'/);
  assert.match(source, /force:\s*true/);
  assert.match(source, /現在有効な設定で初回登録時と同じ索引処理/);
  assert.match(styles, /\.document-library-bulk-toolbar/);
  assert.match(styles, /tr\.is-selected/);
});

test('登録済み文書は操作行右端のプルダウンから4種類の並び順をAPIへ渡し年月編集欄を上揃えにする', async () => {
  const source = await readFile(new URL('../src/modules/document-library.js', import.meta.url), 'utf8');
  const styles = await readFile(new URL('../src/style.css', import.meta.url), 'utf8');

  assert.match(source, /sort:\s*state\.sort/);
  assert.match(source, /export async function setLibrarySort/);
  assert.match(source, /id="librarySortSelect"/);
  assert.match(source, /document-library-bulk-sort/);
  for (const value of ['updated_desc', 'created_desc', 'updated_asc', 'filename_asc']) {
    assert.match(source, new RegExp(`option value="${value}"`));
  }
  assert.match(styles, /\.document-library-bulk-sort\s*\{[^}]*margin-left:\s*auto;/s);
  assert.match(styles, /\.document-library-bulk-toolbar\s*\{[^}]*flex-wrap:\s*nowrap;/s);
  assert.match(styles, /\.metadata-editor \{[^}]*align-items:\s*start;/s);
  assert.match(styles, /\.metadata-editor \.metadata-year-range \{\s*align-items:\s*flex-start;/);
});

test('検索ページ詳細は技術経路をメインから隠し、生成テキストと関連度内訳をポップアップ表示する', async () => {
  document.body.innerHTML = `
    <div id="searchResults" style="display:none"><span id="searchResultsSummary"></span><div id="searchResultsList"></div></div>
  `;
  const result = {
    success: true,
    query: '開放的なLDK',
    total_files: 1,
    total_images: 1,
    total_groups: 1,
    processing_time: 0.2,
    result_order: 'search_rank',
    groups: [{
      group_key: 'set:1',
      label: '案件A',
      document_set_id: 'set-1',
      direct_document_ids: ['doc-1'],
      related_documents: [{
        file_id: 'doc-2', bucket: 'bucket', object_name: 'documents/2/source.pdf',
        original_filename: '関連.pdf', thumbnail_page_number: 1
      }]
    }],
    results: [{
      file_id: 'doc-1', bucket: 'bucket', object_name: 'documents/1/source.pdf',
      original_filename: '提案.pdf', group_key: 'set:1', match_percent: 91,
      matched_concept_ids: [], matched_images: [{
        embed_id: 'evidence-1', bucket: 'bucket', object_name: 'page.png', page_number: 3,
        url: '/ai/api/object/bucket/page.png', match_percent: 88,
        image_similarity_percent: null, retrieval_channels: ['keyword:page_text'],
        match_reasons: ['検索語がページ本文に一致'], verification_status: 'not_requested',
        text_excerpt: '開放的なLDK', caption: ''
      }]
    }]
  };

  searchModule.displaySearchResults(result);
  const list = document.getElementById('searchResultsList');
  assert.ok(list.querySelector('.search-evidence-detail-button'));
  assert.ok(list.querySelector('.search-related-document-actions'));
  assert.match(list.textContent, /関連度: 88\.0%/);
  assert.doesNotMatch(list.textContent, /keyword:page_text/);

  globalThis.fetch = async url => {
    assert.match(String(url), /\/documents\/doc-1\/page-texts/);
    return {
      ok: true,
      status: 200,
      json: async () => ({
        items: [{
          component_key: 'vlm:1', artifact_kind: 'VLM_TEXT', raw_text: '明るいLDK',
          payload_json: { keywords: ['開放的なLDK'] }, stage_status: 'CURRENT'
        }]
      })
    };
  };
  await searchModule.showSearchEvidenceDetails(0, 0);
  await new Promise(resolve => setTimeout(resolve, 0));
  const modal = document.getElementById('textPreviewModalOverlay');
  assert.ok(modal);
  assert.match(modal.textContent, /関連度の詳細/);
  assert.match(modal.textContent, /VLMプロファイル 1/);
  assert.match(modal.querySelector('#textPreviewBody').textContent, /keyword:page_text/);
  modal.remove();
});
