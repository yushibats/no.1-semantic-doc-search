
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