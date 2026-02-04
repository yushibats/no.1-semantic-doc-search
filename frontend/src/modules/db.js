
import { apiCall } from './auth.js';
import { showLoading, hideLoading, showToast } from './utils.js';

export async function loadDbConnectionSettings() {
  try {
    const data = await apiCall('/ai/api/settings/database');
    const settings = data.settings;
    
    document.getElementById('dbUser').value = settings.username || '';
    
    // Walletアップロード状況を表示
    if (settings.wallet_uploaded) {
      const walletStatus = document.getElementById('walletStatus');
      walletStatus.style.display = 'block';
      walletStatus.innerHTML = '<span class="text-green-600">✅ Walletアップロード済み</span>';
      
      // 利用可能なDSNを表示
      if (settings.available_services && settings.available_services.length > 0) {
        const dsnDisplay = document.getElementById('dsnDisplay');
        const dsnSelect = document.getElementById('dbDsn');
        dsnDisplay.style.display = 'block';
        
        dsnSelect.innerHTML = '<option value="">選択してください</option>';
        settings.available_services.forEach(dsn => {
          const option = document.createElement('option');
          option.value = dsn;
          option.textContent = dsn;
          if (dsn === settings.dsn) {
            option.selected = true;
          }
          dsnSelect.appendChild(option);
        });
      }
    }
    
    const statusBadge = document.getElementById('dbConnectionStatusBadge');
    if (data.is_connected) {
      statusBadge.textContent = '接続済み';
      statusBadge.style.background = '#10b981';
      statusBadge.style.color = '#fff';
    } else {
      statusBadge.textContent = '未設定';
      statusBadge.style.background = '#e2e8f0';
      statusBadge.style.color = '#64748b';
    }
    
  } catch (error) {
    console.error('DB設定読み込みエラー:', error);
    // エラーを再スローしてswitchTabでキャッチさせる（トーストは表示しない）
    throw error;
  }
}

export async function refreshDbConnectionFromEnv() {
  try {
    showLoading('接続設定を再取得中...');
    
    // 環境変数から情報を取得
    const envData = await apiCall('/ai/api/settings/database/env');
    
    if (!envData.success) {
      hideLoading();
      showToast(envData.message, 'error');
      return;
    }
    
    // ユーザー名を設定
    if (envData.username) {
      document.getElementById('dbUser').value = envData.username;
    }
    
    // Wallet情報を表示
    const walletStatus = document.getElementById('walletStatus');
    if (envData.wallet_exists) {
      walletStatus.style.display = 'block';
      walletStatus.innerHTML = '<span class="text-green-600">✅ Wallet検出済み (' + envData.wallet_location + ')</span>';
      
      // 利用可能なDSNを表示
      if (envData.available_services && envData.available_services.length > 0) {
        const dsnDisplay = document.getElementById('dsnDisplay');
        const dsnSelect = document.getElementById('dbDsn');
        dsnDisplay.style.display = 'block';
        
        dsnSelect.innerHTML = '<option value="">選択してください</option>';
        envData.available_services.forEach(dsn => {
          const option = document.createElement('option');
          option.value = dsn;
          option.textContent = dsn;
          // 環境変数のDSNを選択
          if (dsn === envData.dsn) {
            option.selected = true;
          }
          dsnSelect.appendChild(option);
        });
      }
    } else {
      walletStatus.style.display = 'block';
      // ダウンロードエラーがあれば表示
      if (envData.download_error) {
        walletStatus.innerHTML = '<span class="text-red-600">❌ Wallet自動ダウンロード失敗: ' + envData.download_error + '</span><br><span class="text-gray-600">手動でZIPファイルをアップロードしてください。</span>';
      } else {
        walletStatus.innerHTML = '<span class="text-yellow-600">⚠️ Walletが見つかりません。ZIPファイルをアップロードしてください。</span>';
      }
    }
    
    // ステータスバッジを更新（設定ファイルの有無で判定、実際の接続確認はしない）
    const statusBadge = document.getElementById('dbConnectionStatusBadge');
    
    if (envData.username && envData.dsn && envData.wallet_exists) {
      statusBadge.textContent = '設定済み';
      statusBadge.style.background = '#10b981';
      statusBadge.style.color = '#fff';
    } else {
      statusBadge.textContent = '未設定';
      statusBadge.style.background = '#e2e8f0';
      statusBadge.style.color = '#64748b';
    }
    
    hideLoading();
    showToast('接続設定を再取得しました', 'success');
    
  } catch (error) {
    hideLoading();
    showToast(`接続設定再取得エラー: ${error.message}`, 'error');
  }
}


/**
 * DB設定再読み込み(リトライ機能)
 */
export async function retryLoadDbSettings() {
  try {
    // 警告メッセージを削除
    const alerts = document.querySelectorAll('#tab-database > .bg-yellow-50');
    alerts.forEach(alert => alert.remove());
    
    utilsShowLoading('データベース設定を再読み込み中...');
    
    await loadDbConnectionSettings();
    
    // ADB OCIDのみを自動取得
    try {
      await loadAdbOcidOnly();
    } catch (error) {
      console.warn('ADB OCID取得エラー（スキップ）:', error);
    }
    
    // .envからDB接続情報を自動取得
    try {
      await loadDbConnectionInfoFromEnv();
    } catch (error) {
      console.warn('DB接続情報取得エラー（スキップ）:', error);
    }
    
    utilsHideLoading();
    utilsShowToast('データベース設定を読み込みました', 'success');
  } catch (error) {
    utilsHideLoading();
    
    if (error.message.includes('タイムアウト')) {
      utilsShowToast('まだデータベースが起動していません。もう一度お試しください。', 'warning');
      
      // 警告メッセージを再表示
      const dbContent = document.getElementById('tab-database');
      if (dbContent && !dbContent.querySelector('.bg-yellow-50')) {
        const retryHtml = `
          <div class="bg-yellow-50 border-l-4 border-yellow-400 p-4 mb-4" role="alert">
            <div class="flex items-start">
              <div class="flex-shrink-0">
                <svg class="h-5 w-5 text-yellow-400" viewBox="0 0 20 20" fill="currentColor">
                  <path fill-rule="evenodd" d="M8.257 3.099c.765-1.36 2.722-1.36 3.486 0l5.58 9.92c.75 1.334-.213 2.98-1.742 2.98H4.42c-1.53 0-2.493-1.646-1.743-2.98l5.58-9.92zM11 13a1 1 0 11-2 0 1 1 0 012 0zm-1-8a1 1 0 00-1 1v3a1 1 0 002 0V6a1 1 0 00-1-1z" clip-rule="evenodd" />
                </svg>
              </div>
              <div class="ml-3 flex-1">
                <p class="text-sm text-yellow-700">
                  データベース設定の読み込みに失敗しました。データベースが起動していない可能性があります。
                </p>
                <p class="mt-2 text-sm text-yellow-700">
                  データベースを起動してから、下のボタンをクリックして再読み込みしてください。
                </p>
                <div class="mt-3">
                  <button 
                    onclick="window.retryLoadDbSettings()" 
                    class="bg-yellow-500 hover:bg-yellow-600 text-white px-4 py-2 rounded transition-colors"
                  >
                    🔄 再読み込み
                  </button>
                </div>
              </div>
            </div>
          </div>
        `;
        dbContent.insertAdjacentHTML('afterbegin', retryHtml);
      }
    } else {
      utilsShowToast(`再読み込みエラー: ${error.message}`, 'error');
    }
  }
};


let selectedWalletFile = null;

export function handleWalletFileSelect(event) {
  const file = event.target.files[0];
  if (!file) return;
  
  if (!file.name.toLowerCase().endsWith('.zip')) {
    utilsShowToast('ZIPファイルを選択してください', 'error');
    return;
  }
  
  selectedWalletFile = file;
  const fileNameDiv = document.getElementById('walletFileName');
  fileNameDiv.style.display = 'block';
  fileNameDiv.textContent = `選択されたファイル: ${file.name}`;
  
  // Walletを自動アップロード
  uploadWalletFile(file);
}

export async function uploadWalletFile(file) {
  try {
    utilsShowLoading('Walletをアップロード中...');
    
    const formData = new FormData();
    formData.append('file', file);
    
    const headers = {};
    if (loginToken) {
      headers['Authorization'] = `Bearer ${loginToken}`;
    }
    
    const response = await fetch(API_BASE ? `${API_BASE}/api/settings/database/wallet` : '/ai/api/settings/database/wallet', {
      method: 'POST',
      headers: headers,
      body: formData
    });
    
    utilsHideLoading();
    
    if (!response.ok) {
      const error = await response.json();
      throw new Error(error.detail || 'Walletアップロードに失敗しました');
    }
    
    const data = await response.json();
    
    if (data.success) {
      const walletStatus = document.getElementById('walletStatus');
      walletStatus.style.display = 'block';
      walletStatus.innerHTML = '<span class="text-green-600">✅ Walletアップロード成功</span>';
      
      utilsShowToast(data.message, 'success');
      
      // 利用可能なDSNを表示
      if (data.available_services && data.available_services.length > 0) {
        const dsnDisplay = document.getElementById('dsnDisplay');
        const dsnSelect = document.getElementById('dbDsn');
        dsnDisplay.style.display = 'block';
        
        dsnSelect.innerHTML = '<option value="">選択してください</option>';
        data.available_services.forEach(dsn => {
          const option = document.createElement('option');
          option.value = dsn;
          option.textContent = dsn;
          dsnSelect.appendChild(option);
        });
      }
    }
    
  } catch (error) {
    utilsHideLoading();
    utilsShowToast(`Walletアップロードエラー: ${error.message}`, 'error');
    
    const walletStatus = document.getElementById('walletStatus');
    walletStatus.style.display = 'block';
    walletStatus.innerHTML = `<span class="text-red-600">❌ ${error.message}</span>`;
  }
}

export async function saveDbConnection() {
  const username = document.getElementById('dbUser').value.trim();
  const password = document.getElementById('dbPassword').value;
  const dsn = document.getElementById('dbDsn').value;
  
  if (!username || !password) {
    utilsShowToast('ユーザー名とパスワードを入力してください', 'warning');
    return;
  }
  
  if (!dsn) {
    utilsShowToast('サービス名/DSNを選択してください', 'warning');
    return;
  }
  
  const settings = {
    username: username,
    password: password,
    dsn: dsn
  };
  
  try {
    utilsShowLoading('DB設定を保存中...');
    
    await authApiCall('/ai/api/settings/database', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(settings)
    });
    
    utilsHideLoading();
    utilsShowToast('DB設定を保存しました', 'success');
    
    await loadDbConnectionSettings();
    
  } catch (error) {
    utilsHideLoading();
    utilsShowToast(`保存エラー: ${error.message}`, 'error');
  }
}

export async function testDbConnection() {
  try {
    // パスワードフィールドを取得
    const passwordField = document.getElementById('dbPassword');
    
    // 少し待ってから値を取得（スクロール防止のためfocus/blurは削除）
    await new Promise(resolve => setTimeout(resolve, 100));
    
    // 入力されている値を取得（保存前でもテストできるように）
    const username = document.getElementById('dbUser').value.trim();
    let password = passwordField.value;
    const dsn = document.getElementById('dbDsn').value;
    
    // パスワードが入力されていない場合、環境変数から取得
    if (!password) {
      utilsShowLoading('環境変数からパスワードを取得中...');
      try {
        const envData = await authApiCall('/ai/api/settings/database/env?include_password=true');
        if (envData.success && envData.password && envData.password !== '[CONFIGURED]') {
          password = envData.password;
        }
        utilsHideLoading();
      } catch (error) {
        utilsHideLoading();
        // console.warn('環境変数からパスワード取得エラー:', error);
      }
    }
    
    // デバッグログ
    // デバッグ情報（本番環境ではコメントアウト）
    // console.log('=== 接続テスト情報 ===');
    // console.log('Username:', username);
    // console.log('Password length:', password ? password.length : 0);
    // console.log('DSN:', dsn);
    // console.log('Password exists:', !!password);
    // console.log('Password from env:', !passwordField.value && !!password);
    // console.log('=====================');
    
    // 入力チェック
    if (!username || !password || !dsn) {
      utilsShowToast('ユーザー名、パスワード、DSNを入力してください', 'warning');
      return;
    }
    
    utilsShowLoading('接続テスト中...');
    
    const requestBody = {
      settings: {
        username: username,
        password: password,
        dsn: dsn
      }
    };
    
    // console.log('Request body:', JSON.stringify({...requestBody, settings: {...requestBody.settings, password: '[HIDDEN]'}}));
    
    // タイムアウト処理を追加（20秒）- バックエンド側も15秒でタイムアウトする
    const timeoutPromise = new Promise((_, reject) => 
      setTimeout(() => reject(new Error('接続テストがタイムアウトしました（20秒）')), 20000)
    );
    
    const apiPromise = authApiCall('/ai/api/settings/database/test', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(requestBody)
    });
    
    const data = await Promise.race([apiPromise, timeoutPromise]);
    
    utilsHideLoading();
    
    if (data.success) {
      utilsShowToast(data.message, 'success');
      
      // 接続成功時、DB情報を自動読み込み
      // await loadDbInfo();
    } else {
      utilsShowToast(data.message, 'error');
    }
    
  } catch (error) {
    utilsHideLoading();
    utilsShowToast(`接続テストエラー: ${error.message}`, 'error');
  }
}

export async function loadDbInfo() {
  try {
    utilsShowLoading('データベース情報を取得中...');
    
    const data = await authApiCall('/ai/api/database/info');
    
    utilsHideLoading();
    
    const infoDiv = document.getElementById('dbInfoContent');
    const statusBadge = document.getElementById('dbInfoStatusBadge');
    
    if (!data.info) {
      infoDiv.innerHTML = `
        <div style="text-align: center; padding: 40px; color: #64748b;">
          <div style="font-size: 48px; margin-bottom: 16px;">🗄️</div>
          <div style="font-size: 16px; font-weight: 500;">データベースに接続してください</div>
          <div style="font-size: 14px; margin-top: 8px;">接続後、データベース情報が表示されます</div>
        </div>
      `;
      if (statusBadge) {
        statusBadge.textContent = '未取得';
        statusBadge.style.background = '#e2e8f0';
        statusBadge.style.color = '#64748b';
      }
      return;
    }
    
    // ステータスバッジを更新
    if (statusBadge) {
      statusBadge.textContent = '取得済み';
      statusBadge.style.background = '#10b981';
      statusBadge.style.color = '#fff';
    }
    
    const info = data.info;
    infoDiv.innerHTML = `
      <div style="display: grid; grid-template-columns: repeat(2, 1fr); gap: 16px;">
        <div class="card">
          <div class="card-body">
            <div style="font-size: 13px; color: #64748b; margin-bottom: 4px;">データベースバージョン</div>
            <div style="font-size: 16px; font-weight: 600; color: #1e293b;">${info.version || '-'}</div>
          </div>
        </div>
        
        <div class="card">
          <div class="card-body">
            <div style="font-size: 13px; color: #64748b; margin-bottom: 4px;">接続ユーザー</div>
            <div style="font-size: 16px; font-weight: 600; color: #1e293b;">${info.current_user || '-'}</div>
          </div>
        </div>
        
        <div class="card">
          <div class="card-body">
            <div style="font-size: 13px; color: #64748b; margin-bottom: 4px;">インスタンス名</div>
            <div style="font-size: 16px; font-weight: 600; color: #1e293b;">${info.instance_name || '-'}</div>
          </div>
        </div>
        
        <div class="card">
          <div class="card-body">
            <div style="font-size: 13px; color: #64748b; margin-bottom: 4px;">データベース名</div>
            <div style="font-size: 16px; font-weight: 600; color: #1e293b;">${info.database_name || '-'}</div>
          </div>
        </div>
      </div>
    `;
    
  } catch (error) {
    utilsHideLoading();
    utilsShowToast(`データベース情報取得エラー: ${error.message}`, 'error');
  }
}