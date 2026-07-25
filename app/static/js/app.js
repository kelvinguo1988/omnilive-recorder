/* Live Recorder - 前端交互逻辑 */

const API = {
    get: (url) => fetch(url).then(r => r.json()),
    post: (url, data) => fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(data) }).then(r => r.json()),
    put: (url, data) => fetch(url, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(data) }).then(r => r.json()),
    delete: (url) => fetch(url, { method: 'DELETE' }).then(r => r.json()),
};

const PLATFORM_NAMES = { douyin: '抖音', bilibili: 'B站', kuaishou: '快手' };
const PLATFORM_TAGS = { douyin: 'tag-douyin', bilibili: 'tag-bilibili', kuaishou: 'tag-kuaishou' };

let currentPage = 'dashboard';
let refreshTimer = null;

// 页面切换
function switchPage(page) {
    currentPage = page;
    document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
    document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
    document.getElementById('page-' + page).classList.add('active');
    document.querySelector(`.nav-item[data-page="${page}"]`).classList.add('active');

    if (page === 'dashboard') refreshDashboard();
    else if (page === 'rooms') loadRooms();
    else if (page === 'recordings') loadRecordings();
    else if (page === 'files') loadFiles();
    else if (page === 'settings') loadSettings();
}

// Toast通知
function showToast(message, type = 'info') {
    const container = document.getElementById('toastContainer');
    const toast = document.createElement('div');
    toast.className = 'toast ' + type;
    toast.textContent = message;
    container.appendChild(toast);
    setTimeout(() => toast.remove(), 3000);
}

// 格式化时间
function formatTime(iso) {
    if (!iso) return '-';
    const d = new Date(iso);
    return d.toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' });
}

function formatDuration(seconds) {
    if (!seconds || seconds === 0) return '-';
    const h = Math.floor(seconds / 3600);
    const m = Math.floor((seconds % 3600) / 60);
    const s = Math.floor(seconds % 60);
    if (h > 0) return `${h}h${m}m`;
    if (m > 0) return `${m}m${s}s`;
    return `${s}s`;
}

function formatSize(mb) {
    if (mb >= 1024) return (mb / 1024).toFixed(2) + ' GB';
    return mb.toFixed(1) + ' MB';
}

// 仪表盘
async function refreshDashboard() {
    try {
        const info = await API.get('/api/system/info');
        document.getElementById('statRooms').textContent = info.rooms.total;
        document.getElementById('statLive').textContent = info.rooms.live;
        document.getElementById('statRecording').textContent = info.rooms.recording;
        document.getElementById('statCompleted').textContent = info.recordings.completed;

        document.getElementById('cpuPercent').textContent = info.cpu_percent + '%';
        document.getElementById('cpuBar').style.width = info.cpu_percent + '%';

        document.getElementById('memPercent').textContent = info.memory_percent + '%';
        document.getElementById('memBar').style.width = info.memory_percent + '%';

        const disk = info.disk || {};
        document.getElementById('diskPercent').textContent = (disk.percent || 0) + '%';
        document.getElementById('diskBar').style.width = (disk.percent || 0) + '%';
        document.getElementById('recordingSize').textContent = (disk.recording_size_gb || 0) + ' GB';

        // 加载活跃录制
        const rooms = await API.get('/api/rooms');
        const activeRooms = rooms.filter(r => r.is_recording);
        const container = document.getElementById('activeRecordings');

        if (activeRooms.length === 0) {
            container.innerHTML = '<div class="empty-state">暂无录制任务</div>';
        } else {
            container.innerHTML = activeRooms.map(r => `
                <div class="recording-item">
                    <div class="info">
                        <div class="name">${r.streamer_name || r.title || '未知'}</div>
                        <div class="detail">${PLATFORM_NAMES[r.platform] || r.platform} · ${formatTime(r.last_live_time)}</div>
                    </div>
                    <div class="recording-pulse" title="录制中"></div>
                </div>
            `).join('');
        }
    } catch (e) {
        console.error('Dashboard error:', e);
    }
}

// 房间管理
async function loadRooms() {
    try {
        const rooms = await API.get('/api/rooms');
        const tbody = document.getElementById('roomsTableBody');

        if (rooms.length === 0) {
            tbody.innerHTML = '<tr><td colspan="7" class="empty-state">还没有添加房间，点击右上角"添加房间"</td></tr>';
            return;
        }

        tbody.innerHTML = rooms.map(r => `
            <tr>
                <td><span class="platform-tag ${PLATFORM_TAGS[r.platform] || ''}">${PLATFORM_NAMES[r.platform] || r.platform}</span></td>
                <td>${r.streamer_name || '-'}</td>
                <td title="${r.title || ''}">${r.title ? (r.title.length > 25 ? r.title.substring(0, 25) + '...' : r.title) : '-'}</td>
                <td>
                    ${r.is_live
                        ? '<span class="status-badge status-live"><span class="dot dot-green"></span>直播中</span>'
                        : '<span class="status-badge status-offline">未直播</span>'}
                </td>
                <td>
                    ${r.is_recording
                        ? '<span class="status-badge status-recording">录制中</span>'
                        : '<span class="status-badge status-offline">空闲</span>'}
                </td>
                <td>${formatTime(r.last_check_time)}</td>
                <td>
                    <div class="action-group">
                        <button class="btn btn-sm btn-secondary" onclick="checkRoom(${r.id})" title="检测">检测</button>
                        ${r.is_recording
                            ? `<button class="btn btn-sm btn-danger" onclick="stopRecording(${r.id})">停止</button>`
                            : (r.is_live ? `<button class="btn btn-sm btn-primary" onclick="startRecording(${r.id})">录制</button>` : '')}
                        <button class="btn btn-sm btn-icon" onclick="toggleRoom(${r.id}, ${!r.enabled})" title="${r.enabled ? '禁用' : '启用'}">
                            ${r.enabled ? '禁用' : '启用'}
                        </button>
                        <button class="btn btn-sm btn-icon" onclick="deleteRoom(${r.id})" title="删除">删除</button>
                    </div>
                </td>
            </tr>
        `).join('');
    } catch (e) {
        console.error('Load rooms error:', e);
        showToast('加载房间列表失败', 'error');
    }
}

// 添加房间
function showAddRoomModal() {
    document.getElementById('addRoomModal').style.display = 'flex';
    document.getElementById('roomUrl').value = '';
    document.getElementById('roomRemark').value = '';
    document.getElementById('platformHint').textContent = '支持抖音、B站、快手 — 自动识别平台';
    document.getElementById('roomUrl').focus();
}

function hideAddRoomModal() {
    document.getElementById('addRoomModal').style.display = 'none';
}

function detectPlatform() {
    const url = document.getElementById('roomUrl').value;
    const hint = document.getElementById('platformHint');
    if (url.includes('douyin.com')) hint.textContent = '检测到: 抖音平台';
    else if (url.includes('bilibili.com')) hint.textContent = '检测到: B站平台';
    else if (url.includes('kuaishou.com')) hint.textContent = '检测到: 快手平台';
    else hint.textContent = '支持抖音、B站、快手 — 自动识别平台';
}

async function submitAddRoom() {
    const url = document.getElementById('roomUrl').value.trim();
    if (!url) { showToast('请输入直播间地址', 'error'); return; }

    const quality = document.getElementById('roomQuality').value;
    const remark = document.getElementById('roomRemark').value.trim();

    try {
        const result = await API.post('/api/rooms', { url, quality, remark, enabled: true });
        if (result.message) {
            showToast('添加成功', 'success');
            hideAddRoomModal();
            loadRooms();
        } else if (result.detail) {
            showToast(result.detail, 'error');
        }
    } catch (e) {
        showToast('添加失败', 'error');
    }
}

async function checkRoom(id) {
    try {
        await API.post(`/api/rooms/${id}/check`, {});
        showToast('正在检测...', 'info');
        setTimeout(() => loadRooms(), 5000);
    } catch (e) { showToast('检测失败', 'error'); }
}

async function startRecording(id) {
    try {
        await API.post(`/api/rooms/${id}/start-recording`, {});
        showToast('录制已启动', 'success');
        loadRooms();
    } catch (e) {
        const msg = await e.json?.() || {};
        showToast(msg.detail || '启动录制失败', 'error');
    }
}

async function stopRecording(id) {
    try {
        await API.post(`/api/rooms/${id}/stop-recording`, {});
        showToast('录制已停止', 'success');
        loadRooms();
    } catch (e) { showToast('停止录制失败', 'error'); }
}

async function toggleRoom(id, enabled) {
    try {
        await API.put(`/api/rooms/${id}`, { enabled });
        showToast(enabled ? '已启用' : '已禁用', 'success');
        loadRooms();
    } catch (e) { showToast('操作失败', 'error'); }
}

async function deleteRoom(id) {
    if (!confirm('确认删除这个房间？相关的录制记录也会被删除。')) return;
    try {
        await API.delete(`/api/rooms/${id}`);
        showToast('删除成功', 'success');
        loadRooms();
    } catch (e) { showToast('删除失败', 'error'); }
}

// 录制记录
async function loadRecordings() {
    try {
        const recordings = await API.get('/api/recordings');
        const tbody = document.getElementById('recordingsTableBody');

        if (recordings.length === 0) {
            tbody.innerHTML = '<tr><td colspan="7" class="empty-state">暂无录制记录</td></tr>';
            return;
        }

        tbody.innerHTML = recordings.map(r => {
            const statusClass = r.status === 'recording' ? 'status-recording'
                : r.status === 'completed' ? 'status-completed'
                : r.status === 'failed' ? 'status-failed' : 'status-offline';
            const statusText = { recording: '录制中', completed: '已完成', failed: '失败', pending: '等待中' }[r.status] || r.status;

            return `
                <tr>
                    <td><span class="platform-tag ${PLATFORM_TAGS[r.platform] || ''}">${PLATFORM_NAMES[r.platform] || r.platform}</span></td>
                    <td>${r.streamer_name || '-'}</td>
                    <td title="${r.file_name || ''}">${r.file_name ? (r.file_name.length > 30 ? r.file_name.substring(0, 30) + '...' : r.file_name) : '-'}</td>
                    <td>${r.file_size_mb > 0 ? formatSize(r.file_size_mb) : '-'}</td>
                    <td>${formatDuration(r.duration)}</td>
                    <td><span class="status-badge ${statusClass}">${statusText}</span></td>
                    <td>${formatTime(r.started_at)}</td>
                </tr>
            `;
        }).join('');
    } catch (e) {
        console.error('Load recordings error:', e);
    }
}

// 文件管理
async function loadFiles() {
    try {
        const platform = document.getElementById('filePlatformFilter')?.value || '';
        const url = '/api/files' + (platform ? `?platform=${platform}` : '');
        const files = await API.get(url);
        const tbody = document.getElementById('filesTableBody');

        if (files.length === 0) {
            tbody.innerHTML = '<tr><td colspan="6" class="empty-state">暂无录制文件</td></tr>';
            return;
        }

        tbody.innerHTML = files.map(f => `
            <tr>
                <td class="col-check"><input type="checkbox" class="file-check" data-path="${f.path}" onchange="updateMergeBtn()"></td>
                <td title="${f.name}">${f.name.length > 35 ? f.name.substring(0, 35) + '...' : f.name}</td>
                <td><span class="platform-tag">${f.platform}</span></td>
                <td>${f.streamer}</td>
                <td>${formatSize(f.size_mb)}</td>
                <td>${formatTime(new Date(f.modified_time * 1000).toISOString())}</td>
                <td>
                    <div class="action-group">
                        ${f.is_video ? `<button class="btn btn-sm btn-secondary" onclick="playFile('${f.path}')">播放</button>` : ''}
                        <a class="btn btn-sm btn-secondary" href="/api/files/download/${f.path}" download>下载</a>
                        <button class="btn btn-sm btn-danger" onclick="deleteFile('${f.path}')">删除</button>
                    </div>
                </td>
            </tr>
        `).join('');

        // 重置选择状态
        const selAll = document.getElementById('selectAll');
        if (selAll) selAll.checked = false;
        updateMergeBtn();
    } catch (e) {
        console.error('Load files error:', e);
    }
}

function playFile(path) {
    const video = document.getElementById('videoPlayer');
    video.src = '/api/files/play/' + path;
    document.getElementById('playerTitle').textContent = path.split('/').pop();
    document.getElementById('playerModal').style.display = 'flex';
}

function hidePlayer() {
    const video = document.getElementById('videoPlayer');
    video.pause();
    video.src = '';
    document.getElementById('playerModal').style.display = 'none';
}

async function deleteFile(path) {
    if (!confirm('确认删除这个文件？此操作不可恢复。')) return;
    try {
        await API.delete('/api/files/' + path);
        showToast('删除成功', 'success');
        loadFiles();
    } catch (e) { showToast('删除失败', 'error'); }
}

// 合并选中文件
function updateMergeBtn() {
    const checked = document.querySelectorAll('.file-check:checked');
    const countEl = document.getElementById('selCount');
    if (countEl) countEl.textContent = checked.length;
    const btn = document.getElementById('mergeBtn');
    if (btn) btn.disabled = checked.length < 2;
}

function toggleSelectAll(el) {
    document.querySelectorAll('.file-check').forEach(c => { c.checked = el.checked; });
    updateMergeBtn();
}

async function mergeSelected() {
    const checked = [...document.querySelectorAll('.file-check:checked')];
    if (checked.length < 2) { showToast('请至少选择 2 个文件', 'error'); return; }

    const file_paths = checked.map(c => c.dataset.path);
    const output_format = document.getElementById('mergeFormat').value;
    const btn = document.getElementById('mergeBtn');
    const prevHtml = btn.innerHTML;
    btn.disabled = true;
    btn.textContent = '合并中...';

    try {
        const res = await API.post('/api/files/merge', { file_paths, output_format });
        if (res.success) {
            showToast(`合并成功：${res.output_name}（${res.file_size_mb} MB，${res.input_count} 个文件）`, 'success');
            loadFiles();
            return;
        }
        showToast(res.detail || res.error || '合并失败', 'error');
    } catch (e) {
        const msg = await e.json?.() || {};
        showToast(msg.detail || '合并失败', 'error');
    } finally {
        btn.disabled = false;
        btn.innerHTML = prevHtml;
        updateMergeBtn();
    }
}

// 系统设置
async function loadSettings() {
    try {
        const info = await API.get('/api/system/info');
        const s = info.settings || {};
        document.getElementById('cfgFormat').textContent = s.record_format || '-';
        document.getElementById('cfgInterval').textContent = (s.monitor_interval || '-') + ' 秒';
        document.getElementById('cfgSegment').textContent = (s.segment_time || '-') + ' 秒';
        document.getElementById('cfgOutput').textContent = s.output_dir || '-';

        const platforms = await API.get('/api/system/platforms');
        document.getElementById('cfgPlatforms').innerHTML = platforms.map(p =>
            `<span class="platform-tag" style="background:${p.color}22;color:${p.color}">${p.name}</span>`
        ).join(' ');

        // 加载日志
        const logs = await API.get('/api/system/logs?limit=50');
        const container = document.getElementById('logContainer');
        if (logs.length === 0) {
            container.innerHTML = '<div class="empty-state">暂无日志</div>';
        } else {
            container.innerHTML = logs.map(l => `
                <div class="log-entry">
                    <span class="log-time">${formatTime(l.created_at)}</span>
                    <span class="log-level ${l.level}">${l.level.toUpperCase()}</span>
                    <span class="log-msg">${l.message}</span>
                </div>
            `).join('');
        }
    } catch (e) {
        console.error('Load settings error:', e);
    }
}

// 初始化
document.addEventListener('DOMContentLoaded', () => {
    refreshDashboard();
    // 自动刷新仪表盘
    refreshTimer = setInterval(() => {
        if (currentPage === 'dashboard') refreshDashboard();
    }, 15000);
});

// 键盘快捷键
document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') {
        hideAddRoomModal();
        hidePlayer();
    }
});
