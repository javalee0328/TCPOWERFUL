import React, { useState, useEffect, useRef } from 'react';
import {
  StyleSheet, Text, View, TouchableOpacity, ScrollView,
  Alert, ActivityIndicator, SafeAreaView, Platform, FlatList
} from 'react-native';
import * as MediaLibrary from 'expo-media-library';
import * as FileSystem from 'expo-file-system/legacy';
import axios from 'axios';
import Constants from 'expo-constants';

// ── API URL Detection ──────────────────────────────────────────────────
const getApiUrl = () => {
  const hostUri = Constants.expoConfig?.hostUri || Constants.manifest?.hostUri || '';
  if (hostUri) {
    const host = hostUri.split(':')[0];
    if (host.includes('.') && !/^\d+\.\d+\.\d+\.\d+$/.test(host)) return `https://${host}`;
    if (/^192\.168\./.test(host)) return `http://${host}:8081`;
  }
  return 'http://localhost:8081';
};
const API_URL = getApiUrl();

export default function App() {
  const [hasPermission, setHasPermission] = useState(null);
  const [isSyncing, setIsSyncing] = useState(false);
  const [statusText, setStatusText] = useState('引擎就緒');
  const [progress, setProgress] = useState({ current: 0, total: 0, skipped: 0, failed: 0 });
  const [logs, setLogs] = useState([]);
  const [tab, setTab] = useState('backup');  // 'backup' | 'list'
  const [backedUpFiles, setBackedUpFiles] = useState([]);
  const [failedFiles, setFailedFiles] = useState([]);  // filenames that failed
  const isCancelled = useRef(false);
  const failedAssetsRef = useRef([]);  // full asset objects that failed

  const addLog = (msg) => {
    const t = new Date().toLocaleTimeString('zh-TW', { hour12: false });
    setLogs(prev => [`[${t}] ${msg}`, ...prev].slice(0, 300));
  };

  useEffect(() => {
    (async () => {
      const { status } = await MediaLibrary.requestPermissionsAsync();
      setHasPermission(status === 'granted');
      addLog('🚀 備份引擎初始化完成');
      try {
        await axios.get(`${API_URL}/api/v1/health`, { timeout: 5000 });
        addLog(`✅ 已連線: ${API_URL}`);
        setStatusText('已連線，可開始備份');
      } catch (e) {
        addLog(`⚠️ 伺服器尚未連上 (${API_URL})`);
        setStatusText('等待連線...');
      }
    })();
  }, []);

  // Load backup list from server
  const loadBackedUpFiles = async () => {
    try {
      const res = await axios.get(`${API_URL}/api/v1/list`, { timeout: 8000 });
      setBackedUpFiles(res.data.files || []);
      addLog(`📋 已備份清單: ${res.data.files?.length || 0} 個檔案`);
    } catch (e) {
      addLog(`⚠️ 無法載入清單: ${e.message}`);
    }
  };

  const startSync = async () => {
    setIsSyncing(true);
    isCancelled.current = false;
    failedAssetsRef.current = [];
    setFailedFiles([]);
    setStatusText('備份進行中...');
    const uploadedIds = [];
    let skippedCount = 0, failedCount = 0;

    try {
      // Fetch all media
      let allAssets = [];
      let after = undefined;
      while (true) {
        const page = await MediaLibrary.getAssetsAsync({
          first: 500, after,
          mediaType: ['photo', 'video'],
          sortBy: MediaLibrary.SortBy.creationTime
        });
        allAssets = allAssets.concat(page.assets);
        if (!page.hasNextPage) break;
        after = page.endCursor;
      }

      addLog(`📂 找到 ${allAssets.length} 個媒體檔案`);
      setProgress({ current: 0, total: allAssets.length, skipped: 0, failed: 0 });

      const BATCH = 3;
      for (let i = 0; i < allAssets.length; i += BATCH) {
        if (isCancelled.current) { addLog('🛑 已停止備份'); break; }

        const batch = allAssets.slice(i, Math.min(i + BATCH, allAssets.length));
        await Promise.all(batch.map(async (asset) => {
          const info = await MediaLibrary.getAssetInfoAsync(asset);
          const fileName = info.filename || `file_${asset.id}.jpg`;

          try {
            // 1. Check
            const check = await axios.get(`${API_URL}/api/v1/check`, {
              params: { filename: fileName, size: info.size || 0 },
              timeout: 8000
            });
            if (check.data.exists) {
              addLog(`⏭️ 跳過 (已有): ${fileName}`);
              skippedCount++;
              setProgress(p => ({ ...p, current: p.current + 1, skipped: p.skipped + 1 }));
              return;
            }

            // 2. Resolve URI: on iOS, raw/cloud assets give ph:// which uploadAsync can't read directly.
            // Copy to temp cache first to ensure a proper file:// URI.
            let localUri = info.localUri || info.uri;
            let tempUri = null;
            if (localUri.startsWith('ph://') || Platform.OS === 'ios') {
              tempUri = FileSystem.cacheDirectory + fileName.replace(/[^a-zA-Z0-9.-]/g, '_');
              await FileSystem.copyAsync({ from: localUri, to: tempUri });
              localUri = tempUri;
            }

            const ts = new Date(info.creationTime || Date.now()).toISOString();
            const mimeType = asset.mediaType === 'video' ? 'video/mp4' : 'image/jpeg';

            const uploadResult = await FileSystem.uploadAsync(
              `${API_URL}/api/v1/upload?timestamp=${encodeURIComponent(ts)}&filename=${encodeURIComponent(fileName)}`,
              localUri,
              {
                httpMethod: 'POST',
                uploadType: FileSystem.FileSystemUploadType.MULTIPART,
                fieldName: 'file',
                mimeType,
                headers: {}
              }
            );

            if (tempUri) await FileSystem.deleteAsync(tempUri, { idempotent: true }).catch(()=>{});

            // Parse response — handle non-JSON gracefully
            let body = {};
            try { body = JSON.parse(uploadResult.body || '{}'); } catch (_) {
              addLog(`⚠️ 伺服器回傳非JSON: ${uploadResult.body?.substring(0, 80)}`);
            }
            const ok = uploadResult.status >= 200 && uploadResult.status < 300;
            if (ok && body.status !== 'error') {
              addLog(`✅ 備份: ${fileName}`);
              uploadedIds.push(asset.id);
            } else {
              addLog(`❌ 失敗 (${uploadResult.status}): ${fileName} → ${body.message || uploadResult.body?.substring(0, 60) || '未知'}`);
              failedCount++;
              setProgress(p => ({ ...p, current: p.current + 1, failed: p.failed + 1 }));
              return;
            }
          } catch (e) {
            addLog(`❌ 錯誤: ${fileName} → ${e.message}`);
            failedCount++;
            failedAssetsRef.current.push(asset);  // track for retry
            setProgress(p => ({ ...p, current: p.current + 1, failed: p.failed + 1 }));
            return;
          }
          setProgress(p => ({ ...p, current: p.current + 1 }));
        }));
      }

      if (!isCancelled.current) {
        const failedNames = failedAssetsRef.current.map(a => a.filename);
        setFailedFiles(failedNames);
        addLog(`🏁 完成！成功 ${uploadedIds.length}，跳過 ${skippedCount}，失敗 ${failedCount}`);
        
        let finalStatus = '✅ 備份完成！';
        if (failedCount > 0) finalStatus = `⚠️ ${failedCount} 個失敗，可重試`;
        setStatusText(finalStatus);
        
        loadBackedUpFiles();
        
        if (failedCount === 0) {
          // Auto-switch to album tab upon complete success
          setTab('list');
        }

        if (uploadedIds.length > 0) {
          Alert.alert('備份成功', `已備份 ${uploadedIds.length} 個\n跳過 ${skippedCount} 個（已有）\n\n是否要釋放手機空間？`, [
            { text: '不要', style: 'cancel' },
            { text: '釋放空間', style: 'destructive', onPress: async () => {
              await MediaLibrary.deleteAssetsAsync(uploadedIds);
              addLog(`🗑️ 已刪除 ${uploadedIds.length} 個已備份檔案`);
            }}
          ]);
        } else if (failedCount > 0) {
           Alert.alert('部份失敗', `有 ${failedCount} 個檔案備份失敗\n請點擊「重試失敗」按鈕再試一次`, [{text: '知道了'}]);
        }
      }

    } catch (e) {
      addLog(`💥 系統錯誤: ${e.message}`);
      setStatusText('發生錯誤');
    } finally {
      setIsSyncing(false);
    }
  };

  const retryFailed = async () => {
    const toRetry = failedAssetsRef.current;
    if (!toRetry.length) return;
    setIsSyncing(true);
    isCancelled.current = false;
    setFailedFiles([]);
    failedAssetsRef.current = [];
    const uploadedIds = [];
    let failedCount = 0;
    addLog(`🔄 重試 ${toRetry.length} 個失敗檔案`);
    setProgress({ current: 0, total: toRetry.length, skipped: 0, failed: 0 });

    const BATCH = 2;
    for (let i = 0; i < toRetry.length; i += BATCH) {
      if (isCancelled.current) break;
      const batch = toRetry.slice(i, Math.min(i + BATCH, toRetry.length));
      await Promise.all(batch.map(async (asset) => {
        const info = await MediaLibrary.getAssetInfoAsync(asset);
        const fileName = info.filename || `file_${asset.id}.jpg`;
        let localUri = info.localUri || info.uri;
        let tempUri = null;
        if (localUri.startsWith('ph://') || Platform.OS === 'ios') {
          tempUri = FileSystem.cacheDirectory + fileName.replace(/[^a-zA-Z0-9.-]/g, '_');
          await FileSystem.copyAsync({ from: localUri, to: tempUri });
          localUri = tempUri;
        }

        const ts = new Date(info.creationTime || Date.now()).toISOString();
        const mimeType = asset.mediaType === 'video' ? 'video/mp4' : 'image/jpeg';
        try {
          const uploadResult = await FileSystem.uploadAsync(
            `${API_URL}/api/v1/upload?timestamp=${encodeURIComponent(ts)}&filename=${encodeURIComponent(fileName)}`,
            localUri,
            { httpMethod: 'POST', uploadType: FileSystem.FileSystemUploadType.MULTIPART, fieldName: 'file', mimeType }
          );
          if (tempUri) await FileSystem.deleteAsync(tempUri, { idempotent: true }).catch(()=>{});
          let body = {};
          try { body = JSON.parse(uploadResult.body || '{}'); } catch (_) {}
          const ok = uploadResult.status >= 200 && uploadResult.status < 300;
          if (ok && body.status !== 'error') {
            addLog(`✅ 重試成功: ${fileName}`);
            uploadedIds.push(asset.id);
          } else {
            addLog(`❌ 仍失敗(${uploadResult.status}): ${fileName} → ${body.message || uploadResult.body?.substring(0, 60) || '未知'}`);
            failedCount++;
            failedAssetsRef.current.push(asset);
            setProgress(p => ({ ...p, current: p.current + 1, failed: p.failed + 1 }));
            return;
          }
        } catch (e) {
          addLog(`❌ 重試錯誤: ${fileName} → ${e.message}`);
          failedCount++;
          failedAssetsRef.current.push(asset);
          setProgress(p => ({ ...p, current: p.current + 1, failed: p.failed + 1 }));
          return;
        }
        setProgress(p => ({ ...p, current: p.current + 1 }));
      }));
    }
    const failedNames = failedAssetsRef.current.map(a => a.filename);
    setFailedFiles(failedNames);
    addLog(`🏁 重試完成：成功 ${uploadedIds.length}，仍失敗 ${failedCount}`);
    setStatusText(failedCount > 0 ? `⚠️ ${failedCount} 個仍失敗` : '✅ 全部備份完成！');
    setIsSyncing(false);
    loadBackedUpFiles();
    if (failedCount === 0) setTab('list');
  };


  const stopSync = () => { isCancelled.current = true; setIsSyncing(false); setStatusText('已停止'); };

  if (hasPermission === false) {
    return <SafeAreaView style={s.container}><Text style={{ color: '#ef4444', margin: 40 }}>請開啟相冊存取權限</Text></SafeAreaView>;
  }

  const pct = progress.total > 0 ? (progress.current / progress.total) * 100 : 0;

  return (
    <SafeAreaView style={s.container}>
      {/* Header */}
      <View style={s.header}>
        <Text style={s.title}>📱 專業照片備份中心</Text>
      </View>

      {/* Tabs */}
      <View style={s.tabs}>
        <TouchableOpacity style={[s.tab, tab === 'backup' && s.tabActive]} onPress={() => setTab('backup')}>
          <Text style={[s.tabTxt, tab === 'backup' && s.tabActiveTxt]}>📤 備份</Text>
        </TouchableOpacity>
        <TouchableOpacity style={[s.tab, tab === 'list' && s.tabActive]}
          onPress={() => { setTab('list'); loadBackedUpFiles(); }}>
          <Text style={[s.tabTxt, tab === 'list' && s.tabActiveTxt]}>🖼️ 相簿</Text>
        </TouchableOpacity>
      </View>

      {tab === 'backup' ? (
        <ScrollView contentContainerStyle={s.content}>
          {/* Status Card */}
          <View style={s.card}>
            <View style={s.row}>
              {isSyncing ? <ActivityIndicator color="#3b82f6" style={{transform:[{scale:1.2}]}} /> : <Text style={{ fontSize: 24 }}>{failedFiles.length>0?'⚠️':'✅'}</Text>}
              <Text style={s.statusTxt}>{statusText}</Text>
            </View>
            <View style={s.statsRow}>
               <Text style={s.statBox}>📦 總進度: {progress.current}/{progress.total}</Text>
               <Text style={s.statBox}>⏭ 跳過: {progress.skipped}</Text>
               <Text style={[s.statBox, progress.failed>0 && {color:'#ef4444'}]}>❌ 失敗: {progress.failed}</Text>
            </View>
            <View style={s.bar}><View style={[s.fill, { width: `${pct}%`, backgroundColor: progress.failed>0 && !isSyncing ? '#ef4444' : '#10b981' }]} /></View>
            
            {!isSyncing && failedFiles.length === 0
              ? <TouchableOpacity style={s.btnBlue} onPress={startSync}><Text style={s.btnTxt}>🚀 啟動增量備份</Text></TouchableOpacity>
              : null}
            {!isSyncing && failedFiles.length > 0
              ? <TouchableOpacity style={[s.btnBlue, { backgroundColor: '#f59e0b' }]} onPress={retryFailed}>
                  <Text style={s.btnTxt}>🔄 重試失敗 ({failedFiles.length} 個)</Text>
                </TouchableOpacity>
              : null}
            {isSyncing 
              ? <TouchableOpacity style={s.btnRed} onPress={stopSync}><Text style={s.btnTxt}>⏹ 停止備份</Text></TouchableOpacity>
              : null}
          </View>

          {/* Log Panel */}
          <View style={s.logCard}>
            <Text style={s.logTitle}>備份日誌（含檔名與狀態）</Text>
            {logs.map((l, i) => (
              <Text key={i} style={[s.logLine,
                l.includes('✅') && { color: '#10b981' },
                l.includes('❌') && { color: '#f87171' },
                l.includes('⏭️') && { color: '#64748b' },
                l.includes('🛑') && { color: '#f59e0b' },
              ]}>{l}</Text>
            ))}
          </View>
        </ScrollView>
      ) : (
        <View style={{ flex: 1 }}>
          <Text style={s.listHeader}>共 {backedUpFiles.length} 個已備份檔案</Text>
          <FlatList
            data={backedUpFiles}
            keyExtractor={(item, i) => i.toString()}
            renderItem={({ item }) => (
              <View style={s.listItem}>
                <Text style={s.listIcon}>{item.endsWith('.mp4') || item.endsWith('.mov') ? '🎬' : '🖼️'}</Text>
                <View style={{ flex: 1 }}>
                  <Text style={s.listName}>{item.split('/').pop()}</Text>
                  <Text style={s.listPath}>{item}</Text>
                </View>
              </View>
            )}
            ListEmptyComponent={
              <Text style={{ color: '#64748b', textAlign: 'center', marginTop: 60 }}>
                尚無備份記錄{'\n'}請先執行備份
              </Text>
            }
          />
        </View>
      )}
    </SafeAreaView>
  );
}

const s = StyleSheet.create({
  container: { flex: 1, backgroundColor: '#f8fafc' },
  header: { padding: 20, paddingBottom: 12, backgroundColor: 'white', borderBottomWidth: 1, borderBottomColor: '#e2e8f0' },
  title: { fontSize: 18, fontWeight: '800', color: '#0f172a' },
  tabs: { flexDirection: 'row', backgroundColor: 'white', borderBottomWidth: 1, borderBottomColor: '#e2e8f0' },
  tab: { flex: 1, padding: 14, alignItems: 'center' },
  tabActive: { borderBottomWidth: 3, borderBottomColor: '#3b82f6' },
  tabTxt: { fontSize: 15, color: '#64748b', fontWeight: '700' },
  tabActiveTxt: { color: '#3b82f6' },
  content: { padding: 15, paddingBottom: 40 },
  card: { backgroundColor: 'white', borderRadius: 20, padding: 22, marginBottom: 16, elevation: 4, shadowColor: '#000', shadowOpacity: 0.08, shadowOffset: { width: 0, height: 3 }, shadowRadius: 10 },
  row: { flexDirection: 'row', alignItems: 'center', marginBottom: 16 },
  statusTxt: { fontSize: 18, fontWeight: '800', color: '#1e293b', marginLeft: 12, flex: 1 },
  statsRow: { flexDirection: 'row', justifyContent: 'space-between', marginBottom: 12 },
  statBox: { fontSize: 13, color: '#475569', fontWeight: '600' },
  bar: { height: 10, backgroundColor: '#f1f5f9', borderRadius: 5, overflow: 'hidden', marginBottom: 20 },
  fill: { height: '100%', borderRadius: 5 },
  btnBlue: { backgroundColor: '#3b82f6', padding: 16, borderRadius: 14, alignItems: 'center' },
  btnRed: { backgroundColor: '#ef4444', padding: 16, borderRadius: 14, alignItems: 'center' },
  btnTxt: { color: 'white', fontSize: 16, fontWeight: '800' },

  logCard: { backgroundColor: '#0f172a', borderRadius: 16, padding: 14, minHeight: 200 },
  logTitle: { color: '#475569', fontSize: 11, fontWeight: '700', marginBottom: 8, textTransform: 'uppercase' },
  logLine: { color: '#10b981', fontSize: 11, fontFamily: Platform.OS === 'ios' ? 'Menlo' : 'monospace', marginBottom: 3, lineHeight: 16 },
  listHeader: { padding: 14, fontSize: 13, color: '#64748b', fontWeight: '700', backgroundColor: '#f1f5f9' },
  listItem: { flexDirection: 'row', alignItems: 'center', padding: 14, borderBottomWidth: 1, borderBottomColor: '#f1f5f9', backgroundColor: 'white' },
  listIcon: { fontSize: 24, marginRight: 12 },
  listName: { fontSize: 13, fontWeight: '600', color: '#1e293b' },
  listPath: { fontSize: 11, color: '#94a3b8', marginTop: 2 },
});
