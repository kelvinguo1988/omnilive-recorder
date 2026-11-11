/* Live Recorder - 前端交互逻辑 */

// ---- 统一 API 层 ----
// F-22: 原来 API.get/post/put/delete 只 then(r=>r.json())，4xx/5xx 也当成功返回，
// 后端 detail（507 磁盘水位、"未在直播中"、"需先填写主页地址"）全部丢失；
// 列表接口失败时返回的是错误对象，调用点 .length 变 undefined 又被当成"暂无数据"。
// 现在所有请求都走 api()：校验 res.ok，失败抛 HttpError{status, detail}。

class HttpError extends Error {
    constructor(status, detail, url) {
        super(detail);
        this.name = 'HttpError';
        this.status = status;   // HTTP 状态码
        this.detail = detail;   // 后端 JSON 的 detail 字段（取不到时为状态码兜底文案）
        this.url = url || '';
    }
}

// 从错误响应体里取后端 detail：FastAPI 的 HTTPException 是 {"detail": "..."}，
// 422 参数校验错误是 {"detail": [{msg, loc}, ...]}
function _detailOf(data, status) {
    let detail = data && data.detail;
    if (Array.isArray(detail)) {
        detail = detail.map(d => (d && (d.msg || d.message)) || String(d)).join('；');
    }
    if (typeof detail === 'object' && detail !== null) detail = JSON.stringify(detail);
    if (detail) return String(detail);
    return '请求失败（HTTP ' + status + '）';
}

async function api(method, url, body) {
    const opts = { method, headers: { 'Accept': 'application/json' } };
    if (body !== undefined) {
        opts.headers['Content-Type'] = 'application/json';
        opts.body = JSON.stringify(body);
    }
    const res = await fetch(url, opts);
    // 先读文本再解析：204/空体、以及反代返回非 JSON（如 502 HTML）都不会抛 SyntaxError
    const text = await res.text();
    let data = null;
    if (text) {
        try { data = JSON.parse(text); } catch (e) { data = null; }
    }
    if (!res.ok) throw new HttpError(res.status, _detailOf(data, res.status), url);
    return data;
}

// 兼容原有调用点（29 处 API.get/post/put/delete 无需改动）
const API = {
    get: (url) => api('GET', url),
    post: (url, data) => api('POST', url, data),
    put: (url, data) => api('PUT', url, data),
    delete: (url) => api('DELETE', url),
};

// 统一错误文案：HttpError 直接透出后端 detail，网络错误单独提示
function errorDetail(e, fallback) {
    if (e && e.name === 'HttpError' && e.detail) return e.detail;
    if (e instanceof TypeError) return (fallback || '请求失败') + '（无法连接后端服务）';
    if (e && e.message) return e.message;
    return fallback || '操作失败';
}

// 列表接口约定返回数组；拿到对象/字符串说明后端异常或反代出错，
// 必须显式失败，不能靠 undefined.length 静默走进"暂无数据"
function asList(data, what) {
    if (Array.isArray(data)) return data;
    throw new Error('接口返回格式异常（' + what + '）');
}

// 表格加载失败占位行：不假装"暂无数据"
function tableErrorRow(colspan, message) {
    return '<tr><td colspan="' + colspan + '" class="empty-state error-state">' + escapeHtml(message) + '</td></tr>';
}

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
    else if (page === 'works') loadWorksPage();
    else if (page === 'files') loadFiles();
    else if (page === 'collections') loadCollections();
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

// HTML 转义：主播名/标题等来自平台 API（主播方可控），文件名/日志含特殊字符，
// 拼进 innerHTML 或 title 属性前必须转义，防止注入
function escapeHtml(s) {
    return String(s ?? '')
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
}

// F-19: 统一的安全日期/大小格式化
// 之前 formatTime 对非法值输出字面 "Invalid Date"，formatSize(undefined) 输出 "NaN MB"，
// 而 new Date(坏值).toISOString() 会抛 RangeError 直接中断整块渲染（一个坏时间戳毁掉整页）。
// safeDate 统一把非法值收敛为 null，由各 format* 输出 "-"。
function safeDate(value) {
    if (value === null || value === undefined || value === '') return null;
    const d = value instanceof Date ? value : new Date(value);
    return Number.isNaN(d.getTime()) ? null : d;
}

// 秒级时间戳（后端文件列表的 modified_time 是秒）→ 合法 Date；非法返回 null
function safeDateFromSeconds(seconds) {
    const n = Number(seconds);
    if (!Number.isFinite(n)) return null;
    return safeDate(n * 1000);
}

// 格式化时间
function formatTime(iso) {
    const d = safeDate(iso);
    if (!d) return '-';
    return d.toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' });
}

function formatDuration(seconds) {
    // F-19: 非数值（undefined/null/NaN）统一 "-"，避免出现 "NaNs"
    const total = Number(seconds);
    if (!Number.isFinite(total) || total <= 0) return '-';
    const h = Math.floor(total / 3600);
    const m = Math.floor((total % 3600) / 60);
    const s = Math.floor(total % 60);
    if (h > 0) return `${h}h${m}m`;
    if (m > 0) return `${m}m${s}s`;
    return `${s}s`;
}

// 大小格式化：非数值（undefined/null/NaN/字符串垃圾）统一 "-"，不再出现 "NaN MB"
function formatSize(mb) {
    // 注意 Number(null) === 0，会误判成"0 MB"；null/undefined/空串都是"大小未知"
    if (mb === null || mb === undefined || mb === '') return '-';
    const n = Number(mb);
    if (!Number.isFinite(n) || n < 0) return '-';
    if (n >= 1024) return (n / 1024).toFixed(2) + ' GB';
    return n.toFixed(1) + ' MB';
}

// 仪表盘
async function refreshDashboard() {
    try {
        // 顺带刷新 Cookie 健康状态（失败不影响仪表盘主体）
        loadCookieStatus().catch(() => {});
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
        const rooms = asList(await API.get('/api/rooms'), '主播列表');
        const activeRooms = rooms.filter(r => r.is_recording);
        const container = document.getElementById('activeRecordings');

        if (activeRooms.length === 0) {
            container.innerHTML = '<div class="empty-state">暂无录制任务</div>';
        } else {
            container.innerHTML = activeRooms.map(r => `
                <div class="recording-item">
                    <div class="info">
                        <div class="name">${escapeHtml(r.streamer_name || r.title || '未知')}</div>
                        <div class="detail">${escapeHtml(PLATFORM_NAMES[r.platform] || r.platform)} · ${formatTime(r.last_live_time)}</div>
                    </div>
                    <div class="recording-pulse" title="录制中"></div>
                </div>
            `).join('');
        }
    } catch (e) {
        console.error('Dashboard error:', e);
        const container = document.getElementById('activeRecordings');
        if (container) container.innerHTML = '<div class="empty-state error-state">' + escapeHtml(errorDetail(e, '加载仪表盘失败')) + '</div>';
    }
}

// 主播管理（统一实体：直播间地址=直播录制，主页地址=作品订阅）
async function loadRooms() {
    try {
        const rooms = asList(await API.get('/api/rooms'), '主播列表');
        const tbody = document.getElementById('roomsTableBody');

        if (rooms.length === 0) {
            tbody.innerHTML = '<tr><td colspan="7" class="empty-state">还没有添加主播，点击右上角"添加主播"</td></tr>';
            return;
        }

        // 同步作品页的主播过滤下拉
        const filter = document.getElementById('worksCreatorFilter');
        if (filter) {
            const cur = filter.value;
            filter.innerHTML = '<option value="">全部主播</option>' + rooms
                .filter(r => r.works_enabled)
                .map(r => `<option value="${r.id}">${escapeHtml(r.streamer_name || r.platform_user_id || r.id)}</option>`)
                .join('');
            filter.value = cur;
        }

        tbody.innerHTML = rooms.map(r => `
            <tr>
                <td><span class="platform-tag ${PLATFORM_TAGS[r.platform] || ''}">${escapeHtml(PLATFORM_NAMES[r.platform] || r.platform)}</span></td>
                <td title="${escapeHtml(r.title || '')}">${escapeHtml(r.streamer_name || '-')}</td>
                <td>
                    ${r.url
                        ? (r.is_live
                            ? '<span class="status-badge status-live"><span class="dot dot-green"></span>直播中</span>'
                            : '<span class="status-badge status-offline">未直播</span>')
                        : '<span class="status-badge status-offline">未配置</span>'}
                </td>
                <td>
                    ${r.is_recording
                        ? '<span class="status-badge status-recording">录制中</span>'
                        : '<span class="status-badge status-offline">空闲</span>'}
                </td>
                <td title="已下载 ${r.works_completed} / 共 ${r.works_total}，失败 ${r.works_failed}">
                    ${r.works_enabled
                        ? `<label class="checkbox-label"><input type="checkbox" checked onchange="toggleWorks(${r.id}, this.checked)" /> ${r.works_completed}/${r.works_total}${r.backfill_done ? '' : ' <span class="status-badge status-recording">回填中</span>'}</label>`
                        : (r.home_url || r.platform_user_id
                            ? `<label class="checkbox-label"><input type="checkbox" onchange="toggleWorks(${r.id}, this.checked)" /> 未订阅</label>`
                            : '<span class="status-badge status-offline">无主页</span>')}
                </td>
                <td>${formatTime(r.last_check_time)}</td>
                <td>
                    <div class="action-group">
                        ${r.url ? `<button class="btn btn-sm btn-secondary" onclick="checkRoom(${r.id})" title="检测直播">检测</button>` : ''}
                        ${r.is_recording
                            ? `<button class="btn btn-sm btn-danger" onclick="stopRecording(${r.id})">停止</button>`
                            : (r.is_live ? `<button class="btn btn-sm btn-primary" onclick="startRecording(${r.id})">录制</button>` : '')}
                        ${r.works_enabled ? `<button class="btn btn-sm btn-secondary" onclick="checkWorksNow(${r.id})" title="检查新作品">查作品</button>` : ''}
                        ${r.works_enabled ? `<button class="btn btn-sm btn-icon" onclick="refillWorks(${r.id})" title="重新全量回填历史作品(已下载的不重复)">重回填</button>` : ''}
                        <button class="btn btn-sm btn-secondary" onclick="showEditRoomModal(${r.id})" title="编辑主播信息">编辑</button>
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
        const msg = errorDetail(e, '加载主播列表失败');
        const tbody = document.getElementById('roomsTableBody');
        if (tbody) tbody.innerHTML = tableErrorRow(7, msg);
        showToast(msg, 'error');
    }
}

async function toggleWorks(id, enabled) {
    try {
        const res = await API.put(`/api/rooms/${id}`, { works_enabled: enabled });
        showToast(res.message || (enabled ? '已订阅作品，正在后台回填...' : '已取消作品订阅'), enabled ? 'success' : 'info');
        loadRooms();
    } catch (e) {
        showToast(errorDetail(e, '操作失败'), 'error');
        loadRooms();   // 失败时也要刷新，避免勾选框停留在用户点击后的错误状态
    }
}

async function refillWorks(id) {
    if (!confirm('重新全量回填该主播的历史作品？已入库/已下载的不会重复，仅补全之前缺失的（如配置 Cookie 后补全被截断的分页）。')) return;
    try {
        const res = await API.put(`/api/rooms/${id}`, { refill: true });
        showToast(res.message || '已开始重新回填，请稍后刷新作品列表查看进度', 'success');
        setTimeout(() => loadRooms(), 5000);
    } catch (e) { showToast(errorDetail(e, '操作失败'), 'error'); }
}

async function checkWorksNow(id) {
    try {
        await API.post(`/api/works/check/${id}`, {});
        showToast('正在检查新作品...', 'info');
        setTimeout(() => loadRooms(), 5000);
    } catch (e) { showToast(errorDetail(e, '检查失败'), 'error'); }
}

// 添加主播
function showAddRoomModal() {
    document.getElementById('addRoomModal').style.display = 'flex';
    document.getElementById('roomUrl').value = '';
    document.getElementById('roomHomeUrl').value = '';
    document.getElementById('roomStreamerName').value = '';
    document.getElementById('roomRemark').value = '';
    document.getElementById('roomWorksEnabled').checked = false;
    document.getElementById('platformHint').textContent = '直播间与主页地址至少填一个';
    document.getElementById('roomUrl').focus();
}

function hideAddRoomModal() {
    document.getElementById('addRoomModal').style.display = 'none';
}

function detectPlatform() {
    const url = document.getElementById('roomUrl').value;
    const hint = document.getElementById('platformHint');
    if (url.includes('douyin.com')) hint.textContent = '检测到: 抖音直播间';
    else if (url.includes('bilibili.com')) hint.textContent = '检测到: B站直播间';
    else if (url.includes('kuaishou.com')) hint.textContent = '检测到: 快手直播间';
    else hint.textContent = '直播间与主页地址至少填一个';
}

function detectHomePlatform() {
    const url = document.getElementById('roomHomeUrl').value;
    const hint = document.getElementById('roomHomeUrl').parentElement.querySelector('.form-hint');
    if (!hint) return;
    if (url.includes('douyin.com/user/')) hint.textContent = '检测到: 抖音主页';
    else if (url.includes('space.bilibili.com/')) hint.textContent = '检测到: B站空间';
    else if (url.includes('kuaishou.com/profile/')) hint.textContent = '检测到: 快手主页';
    else hint.textContent = '抖音: douyin.com/user/MS4w... · B站: space.bilibili.com/数字 · 快手: kuaishou.com/profile/xxx';
}

async function submitAddRoom() {
    const url = document.getElementById('roomUrl').value.trim();
    const home_url = document.getElementById('roomHomeUrl').value.trim();
    if (!url && !home_url) { showToast('直播间地址与主页地址至少填一个', 'error'); return; }

    const quality = document.getElementById('roomQuality').value;
    const remark = document.getElementById('roomRemark').value.trim();
    const streamer_name = document.getElementById('roomStreamerName').value.trim();
    const works_enabled = document.getElementById('roomWorksEnabled').checked;

    try {
        const result = await API.post('/api/rooms', {
            url: url || null, home_url: home_url || null, quality, remark,
            streamer_name: streamer_name || null, works_enabled, enabled: true,
        });
        // F-4: 后端 create_room 里 works_enabled = room.works_enabled and bool(platform_user_id)，
        // 只填直播间地址（未填主页、也解析不出用户ID）时会静默降级为 False，
        // 但响应文案仍是"添加成功，正在后台检测..."。前端按同样的规则算出实际生效值，
        // 降级时用 warning 提示，避免用户以为订阅已开启。
        // 优先用后端回传的 works_enabled（若未来加上该字段），否则用 platform_user_id 复算，
        // 规则与 rooms.py:182 一致。
        const worksEnabledActual = typeof result.works_enabled === 'boolean'
            ? result.works_enabled
            : (!!works_enabled && !!result.platform_user_id);

        if (works_enabled && !worksEnabledActual) {
            showToast('主播已添加，但作品订阅未开启：需要先填写主页地址（或等直播间检测识别出主播后再订阅）', 'warning');
            hideAddRoomModal();
            loadRooms();
            return;
        }
        showToast(result.message || '添加成功', 'success');
        hideAddRoomModal();
        loadRooms();
    } catch (e) {
        showToast(errorDetail(e, '添加失败'), 'error');
    }
}

// 编辑主播
let editingRoom = null;

async function showEditRoomModal(id) {
    try {
        const rooms = asList(await API.get('/api/rooms'), '主播列表');
        const room = rooms.find(r => r.id === id);
        if (!room) { showToast('主播不存在，请刷新列表', 'error'); return; }

        editingRoom = room;
        document.getElementById('editRoomUrl').value = room.url || '';
        document.getElementById('editRoomHomeUrl').value = room.home_url || '';
        document.getElementById('editRoomQuality').value = room.quality || 'origin';
        document.getElementById('editRoomStreamerName').value = room.streamer_name || '';
        document.getElementById('editRoomRemark').value = room.remark || '';
        document.getElementById('editRoomWorksEnabled').checked = !!room.works_enabled;
        document.getElementById('editRoomModal').style.display = 'flex';
    } catch (e) {
        console.error('Load room for edit error:', e);
        showToast(errorDetail(e, '加载主播信息失败'), 'error');
    }
}

function hideEditRoomModal() {
    document.getElementById('editRoomModal').style.display = 'none';
    editingRoom = null;
}

async function submitEditRoom() {
    if (!editingRoom) return;

    const url = document.getElementById('editRoomUrl').value.trim();
    const home_url = document.getElementById('editRoomHomeUrl').value.trim();
    if (!url && !home_url) { showToast('直播间地址与主页地址至少保留一个', 'error'); return; }
    const quality = document.getElementById('editRoomQuality').value;
    const streamer_name = document.getElementById('editRoomStreamerName').value.trim();
    const remark = document.getElementById('editRoomRemark').value.trim();
    const works_enabled = document.getElementById('editRoomWorksEnabled').checked;

    // 只提交有变化的字段；地址未变时不传，避免触发平台缓存重建
    const payload = {};
    if (url !== (editingRoom.url || '')) payload.url = url;
    if (home_url !== (editingRoom.home_url || '')) payload.home_url = home_url;
    if (quality !== editingRoom.quality) payload.quality = quality;
    if (streamer_name !== (editingRoom.streamer_name || '')) payload.streamer_name = streamer_name;
    if (remark !== (editingRoom.remark || '')) payload.remark = remark;
    if (works_enabled !== !!editingRoom.works_enabled) payload.works_enabled = works_enabled;

    if (Object.keys(payload).length === 0) {
        showToast('没有修改任何内容', 'info');
        hideEditRoomModal();
        return;
    }

    try {
        const res = await API.put(`/api/rooms/${editingRoom.id}`, payload);
        // 同 F-4：清空主页地址时后端会连带把 works_enabled 静默置 False（rooms.py 主页变化分支），
        // 响应仍只有"更新成功"。这里按同一规则提示，避免用户以为订阅还在。
        if (payload.home_url === '' && editingRoom.works_enabled) {
            showToast('已保存，但清空主页地址会同时关闭作品订阅', 'warning');
        } else {
            showToast(res.message || '保存成功', 'success');
        }
        hideEditRoomModal();
        loadRooms();
        if (payload.url || payload.works_enabled) setTimeout(() => loadRooms(), 5000);
    } catch (e) {
        showToast(errorDetail(e, '保存失败'), 'error');
    }
}

async function checkRoom(id) {
    try {
        await API.post(`/api/rooms/${id}/check`, {});
        showToast('正在检测...', 'info');
        setTimeout(() => loadRooms(), 5000);
    } catch (e) { showToast(errorDetail(e, '检测失败'), 'error'); }
}

async function startRecording(id) {
    try {
        await API.post(`/api/rooms/${id}/start-recording`, {});
        showToast('录制已启动', 'success');
        loadRooms();
    } catch (e) {
        // F-2: 原来这里是 `await e.json?.()`（e 是 TypeError，永远没有 json 方法），
        // 后端 detail 全丢 —— 包括 507 磁盘水位和"未在直播中"。改为读 HttpError.detail。
        showToast(errorDetail(e, '启动录制失败'), 'error');
        loadRooms();
    }
}

async function stopRecording(id) {
    try {
        await API.post(`/api/rooms/${id}/stop-recording`, {});
        showToast('录制已停止', 'success');
        loadRooms();
    } catch (e) {
        showToast(errorDetail(e, '停止录制失败'), 'error');
        loadRooms();
    }
}

async function toggleRoom(id, enabled) {
    try {
        await API.put(`/api/rooms/${id}`, { enabled });
        showToast(enabled ? '已启用' : '已禁用', 'success');
        loadRooms();
    } catch (e) { showToast(errorDetail(e, '操作失败'), 'error'); }
}

async function deleteRoom(id) {
    if (!confirm('确认删除这个房间？相关的录制记录也会被删除。')) return;
    try {
        await API.delete(`/api/rooms/${id}`);
        showToast('删除成功', 'success');
        loadRooms();
    } catch (e) { showToast(errorDetail(e, '删除失败'), 'error'); }
}

// 录制记录
// F-5: 后端 GET /api/recordings 默认 limit=50（recordings.py:23），前端不传分页参数也没有翻页 UI，
// 老记录会凭空消失。这里显式带上 limit 并把"仅显示最近 N 条"的提示渲染在表格下方（不改后端）。
const RECORDINGS_LIMIT = 50;

async function loadRecordings() {
    try {
        const recordings = asList(await API.get(`/api/recordings?limit=${RECORDINGS_LIMIT}`), '录制记录');
        const tbody = document.getElementById('recordingsTableBody');
        renderRecordingsHint(recordings.length);

        if (recordings.length === 0) {
            tbody.innerHTML = '<tr><td colspan="7" class="empty-state">暂无录制记录</td></tr>';
            return;
        }

        tbody.innerHTML = recordings.map(r => {
            const statusClass = r.status === 'recording' ? 'status-recording'
                : r.status === 'completed' ? 'status-completed'
                : r.status === 'failed' ? 'status-failed' : 'status-offline';
            const statusText = { recording: '录制中', completed: '已完成', failed: '失败', pending: '等待中' }[r.status] || String(r.status ?? '');
            // F-19 同类：file_size_mb 先做数值校验，避免 undefined/垃圾值走进 formatSize
            const sizeMb = Number(r.file_size_mb);

            return `
                <tr>
                    <td><span class="platform-tag ${PLATFORM_TAGS[r.platform] || ''}">${escapeHtml(PLATFORM_NAMES[r.platform] || r.platform)}</span></td>
                    <td>${escapeHtml(r.streamer_name || '-')}</td>
                    <td title="${escapeHtml(r.file_name || '')}">${r.file_name ? escapeHtml(r.file_name.length > 30 ? r.file_name.substring(0, 30) + '...' : r.file_name) : '-'}</td>
                    <td>${Number.isFinite(sizeMb) && sizeMb > 0 ? formatSize(sizeMb) : '-'}</td>
                    <td>${formatDuration(r.duration)}</td>
                    <td><span class="status-badge ${statusClass}">${escapeHtml(statusText)}</span></td>
                    <td>${formatTime(r.started_at)}</td>
                </tr>
            `;
        }).join('');
    } catch (e) {
        console.error('Load recordings error:', e);
        const msg = errorDetail(e, '加载录制记录失败');
        const tbody = document.getElementById('recordingsTableBody');
        if (tbody) tbody.innerHTML = tableErrorRow(7, msg);
        renderRecordingsHint(0);
        showToast(msg, 'error');
    }
}

// 录制记录列表下方的"仅显示最近 N 条"提示（满额才提示，避免空列表时误报）
function renderRecordingsHint(count) {
    const box = document.getElementById('recordingsHint');
    if (!box) return;
    if (count >= RECORDINGS_LIMIT) {
        box.textContent = `仅显示最近 ${RECORDINGS_LIMIT} 条录制记录（按开始时间倒序），更早的记录仍保存在磁盘与数据库中。`;
        box.style.display = '';
    } else {
        box.style.display = 'none';
    }
}

// 文件管理：按主播分组的归档卡片（卡片内分「直播间」「作品」两栏）
const ARCHIVE_COLS = { live: '直播间', works: '作品' };
const PLATFORM_KEYS = Object.fromEntries(Object.entries(PLATFORM_NAMES).map(([k, v]) => [v, k]));
// 折叠状态跨刷新保留（键为主播名）
const openStreamers = new Set();

function _locLabel(f) {
    // 直播录像的日期层级、旧布局的平台层级，作为文件名前的浅色定位
    const segs = (f.sub_dir || '').split('/').filter(Boolean);
    return segs.filter(s => s !== f.streamer && s !== ARCHIVE_COLS.live && s !== ARCHIVE_COLS.works
        && s !== 'works').join('/');
}

function _fileRow(f) {
    const loc = _locLabel(f);
    const name = String(f.name ?? '');
    return `
            <tr>
                <td class="col-check"><input type="checkbox" class="file-check" data-path="${escapeHtml(f.path)}" onchange="updateMergeBtn()"></td>
                <td title="${escapeHtml(f.path)}">${loc ? `<span class="file-loc">${escapeHtml(loc)}</span>` : ''}${escapeHtml(name.length > 35 ? name.substring(0, 35) + '...' : name)}</td>
                <td>${formatSize(f.size_mb)}</td>
                <td>${formatTime(safeDateFromSeconds(f.modified_time))}</td>
                <td>
                    <div class="action-group">
                        ${f.is_video ? `<button class="btn btn-sm btn-secondary" data-act="play" data-path="${escapeHtml(f.path)}">播放</button>` : ''}
                        <a class="btn btn-sm btn-secondary" href="/api/files/download/${f.path.split('/').map(encodeURIComponent).join('/')}" download>下载</a>
                        <button class="btn btn-sm btn-danger" data-act="delete" data-path="${escapeHtml(f.path)}">删除</button>
                    </div>
                </td>
            </tr>`;
}

function _sectionHtml(category, list) {
    if (!list.length) return '';
    const totalMb = list.reduce((s, f) => s + (Number(f.size_mb) || 0), 0);
    return `
        <section class="archive-col">
            <h4>${ARCHIVE_COLS[category] || '其他'}<small>${list.length} 个 · ${formatSize(totalMb)}</small></h4>
            <table class="data-table archive-table">
                <tbody>
                    ${list.map(_fileRow).join('')}
                </tbody>
            </table>
        </section>`;
}

async function loadFiles() {
    try {
        const platform = document.getElementById('filePlatformFilter')?.value || '';
        const url = '/api/files' + (platform ? `?platform=${encodeURIComponent(platform)}` : '');
        const files = asList(await API.get(url), '文件列表');
        const box = document.getElementById('filesGroups');
        const selAll = document.getElementById('selectAll');
        if (selAll) selAll.checked = false;

        if (!files.length) {
            box.innerHTML = '<div class="card"><div class="empty-state">暂无录制文件</div></div>';
            updateMergeBtn();
            return;
        }

        const groups = new Map();
        for (const f of files) {
            const name = f.streamer || '未归档';
            if (!groups.has(name)) {
                groups.set(name, { name, platform: f.platform || '', live: [], works: [], other: [] });
            }
            const g = groups.get(name);
            if (!g.platform && f.platform) g.platform = f.platform;
            (g[f.category] || g.other).push(f);
        }
        // 卡片按各主播最后写入时间倒序，最近录制的排在最前
        // F-19: 任一文件的 modified_time 非法会让 Math.max 得到 NaN（排序失效），先收敛为 0
        const lastOf = g => Math.max(0, ...g.live.concat(g.works, g.other).map(f => {
            const n = Number(f.modified_time);
            return Number.isFinite(n) ? n : 0;
        }));
        const list = [...groups.values()].sort((a, b) => lastOf(b) - lastOf(a));

        box.innerHTML = list.map((g, idx) => {
            const all = g.live.concat(g.works, g.other);
            const platKey = PLATFORM_KEYS[g.platform];
            const open = openStreamers.has(g.name);
            return `
        <div class="card streamer-card">
            <div class="streamer-head${open ? ' open' : ''}">
                <input type="checkbox" class="streamer-check"
                       onchange="selectGroup(${idx}, this.checked)" title="选中该主播全部文件">
                <button class="streamer-toggle" onclick="toggleStreamer(${idx})">
                    <span class="streamer-caret">▶</span>
                    <span class="streamer-name">${escapeHtml(g.name)}</span>
                </button>
                ${g.platform ? `<span class="platform-tag ${platKey ? PLATFORM_TAGS[platKey] : ''}">${escapeHtml(g.platform)}</span>` : ''}
                <span class="streamer-meta">
                    直播间 ${g.live.length} · 作品 ${g.works.length} · ${formatSize(all.reduce((s, f) => s + (Number(f.size_mb) || 0), 0))}
                    · 更新于 ${formatTime(safeDateFromSeconds(lastOf(g)))}
                </span>
            </div>
            <div class="streamer-body" id="sbody-${idx}" data-key="${escapeHtml(g.name)}" style="${open ? '' : 'display:none'}">
                ${_sectionHtml('live', g.live)}
                ${_sectionHtml('works', g.works)}
                ${_sectionHtml('other', g.other)}
            </div>
        </div>`;
        }).join('');

        updateMergeBtn();
    } catch (e) {
        console.error('Load files error:', e);
        const msg = errorDetail(e, '加载文件列表失败');
        const box = document.getElementById('filesGroups');
        if (box) box.innerHTML = '<div class="card"><div class="empty-state error-state">' + escapeHtml(msg) + '</div></div>';
        const selAll = document.getElementById('selectAll');
        if (selAll) selAll.checked = false;
        updateMergeBtn();
        showToast(msg, 'error');
    }
}

function toggleStreamer(idx) {
    const body = document.getElementById('sbody-' + idx);
    if (!body) return;
    const head = body.previousElementSibling;
    const open = body.style.display === 'none';
    body.style.display = open ? '' : 'none';
    if (head) head.classList.toggle('open', open);
    if (open) openStreamers.add(body.dataset.key);
    else openStreamers.delete(body.dataset.key);
}

function expandAllStreamers(open) {
    document.querySelectorAll('.streamer-body').forEach(body => {
        body.style.display = open ? '' : 'none';
        if (body.previousElementSibling) body.previousElementSibling.classList.toggle('open', open);
        if (open) openStreamers.add(body.dataset.key);
        else openStreamers.delete(body.dataset.key);
    });
}

function selectGroup(idx, checked) {
    const body = document.getElementById('sbody-' + idx);
    if (!body) return;
    body.querySelectorAll('.file-check').forEach(c => { c.checked = checked; });
    updateMergeBtn();
}

function playFile(path) {
    const video = document.getElementById('videoPlayer');
    // 与下载链接一致按段编码：文件名含 # ? 空格时不编码会 404
    video.src = '/api/files/play/' + path.split('/').map(encodeURIComponent).join('/');
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
        await API.delete('/api/files/' + path.split('/').map(encodeURIComponent).join('/'));
        showToast('删除成功', 'success');
        loadFiles();
    } catch (e) { showToast(errorDetail(e, '删除失败'), 'error'); }
}

// 合并选中文件
function updateMergeBtn() {
    const checked = document.querySelectorAll('.file-check:checked');
    const countEl = document.getElementById('selCount');
    if (countEl) countEl.textContent = checked.length;
    const btn = document.getElementById('mergeBtn');
    if (btn) btn.disabled = checked.length < 2;

    // 批量删除按钮：选中任意数量即启用
    const delCountEl = document.getElementById('delCount');
    if (delCountEl) delCountEl.textContent = checked.length;
    const delBtn = document.getElementById('delBtn');
    if (delBtn) delBtn.disabled = checked.length < 1;

    // 同步每个主播卡片的勾选框（部分选中显示为半选态）
    document.querySelectorAll('.streamer-card').forEach(card => {
        const group = card.querySelector('.streamer-check');
        const items = card.querySelectorAll('.file-check');
        if (!group || !items.length) return;
        const n = card.querySelectorAll('.file-check:checked').length;
        group.checked = n === items.length;
        group.indeterminate = n > 0 && n < items.length;
    });
}

// 批量删除选中文件
async function deleteSelected() {
    const checked = [...document.querySelectorAll('.file-check:checked')];
    if (checked.length === 0) {
        showToast('请先勾选要删除的文件', 'error');
        return;
    }
    if (!confirm(`确认删除选中的 ${checked.length} 个文件？此操作不可恢复。`)) return;

    const file_paths = checked.map(c => c.dataset.path);
    const btn = document.getElementById('delBtn');
    // F-10: 按钮文案保存原值（含 <span id="delCount"> 结构），成功分支也必须恢复，
    // 否则按钮会长期显示"删除中..."直到刷新，用户会重复点击。
    const prevHtml = btn ? btn.innerHTML : '';
    if (btn) { btn.disabled = true; btn.textContent = '删除中...'; }

    try {
        const res = await API.post('/api/files/batch-delete', { file_paths });
        if (res.failed_count > 0) {
            const failedDetail = (res.failed || [])
                .slice(0, 3)
                .map(x => `${x.path}: ${x.error}`)
                .join('；');
            showToast(`已删除 ${res.deleted_count} 个，失败 ${res.failed_count} 个` + (failedDetail ? `（${failedDetail}）` : ''), 'error');
        } else {
            showToast(`已批量删除 ${res.deleted_count} 个文件`, 'success');
        }
        await loadFiles();
    } catch (e) {
        showToast(errorDetail(e, '批量删除失败'), 'error');
    } finally {
        // F-10: 无论成功/失败都恢复按钮文案（按钮在页头工具栏，loadFiles 不会重建它）
        if (btn) btn.innerHTML = prevHtml;
        updateMergeBtn();
    }
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
            showToast(`合并成功：${res.output_name}（${formatSize(res.file_size_mb)}，${res.input_count} 个文件）`, 'success');
            loadFiles();
            return;
        }
        showToast(res.detail || res.error || '合并失败', 'error');
    } catch (e) {
        // F-2: 同 startRecording —— 原来的 `await e.json?.()` 是死代码，
        // 后端 400 的 detail（"请至少选择 2 个文件" / 合并错误原因）全丢
        showToast(errorDetail(e, '合并失败'), 'error');
    } finally {
        btn.disabled = false;
        btn.innerHTML = prevHtml;
        updateMergeBtn();
    }
}

// 作品订阅（列表页；主播的订阅入口在「主播管理」页）
async function loadWorksPage() {
    // 直接进入本页时主播过滤下拉可能还没填充过
    try {
        const filter = document.getElementById('worksCreatorFilter');
        if (filter && filter.options.length <= 1) {
            const rooms = asList(await API.get('/api/rooms'), '主播列表');
            filter.innerHTML = '<option value="">全部主播</option>' + rooms
                .filter(r => r.works_enabled)
                .map(r => `<option value="${r.id}">${escapeHtml(r.streamer_name || r.platform_user_id || r.id)}</option>`)
                .join('');
        }
    } catch (e) { console.error('Load streamer filter error:', e); }
    await loadWorksTable();
}

async function loadWorksTable() {
    try {
        const roomId = document.getElementById('worksCreatorFilter')?.value || '';
        const status = document.getElementById('worksStatusFilter')?.value || '';
        let url = '/api/works?limit=200';
        if (roomId) url += `&room_id=${roomId}`;
        if (status) url += `&status=${status}`;
        const works = asList(await API.get(url), '作品列表');
        const tbody = document.getElementById('worksTableBody');

        if (works.length === 0) {
            tbody.innerHTML = '<tr><td colspan="8" class="empty-state">暂无作品记录</td></tr>';
            return;
        }

        const STATUS = {
            pending: ['status-offline', '待下载'],
            downloading: ['status-recording', '下载中'],
            completed: ['status-completed', '已完成'],
            failed: ['status-failed', '失败'],
        };
        tbody.innerHTML = works.map(w => {
            const [cls, text] = STATUS[w.status] || ['status-offline', w.status];
            const canPlay = w.status === 'completed' && w.work_type === 'video' && w.file_path;
            const title = String(w.title ?? '');
            return `
                <tr>
                    <td><span class="platform-tag">${escapeHtml(w.platform_name)}</span></td>
                    <td>${escapeHtml(w.nickname || '-')}</td>
                    <td title="${escapeHtml(title)}">${title ? escapeHtml(title.length > 30 ? title.substring(0, 30) + '...' : title) : '-'}</td>
                    <td>${w.work_type === 'images' ? '图集' : '视频'}</td>
                    <td>${formatTime(w.publish_time)}</td>
                    <td>${w.file_size_mb > 0 ? formatSize(w.file_size_mb) : '-'}</td>
                    <td title="${escapeHtml(w.error_message || '')}"><span class="status-badge ${cls}">${escapeHtml(text)}</span></td>
                    <td>
                        <div class="action-group">
                            ${canPlay ? `<button class="btn btn-sm btn-secondary" data-act="play-work" data-path="${escapeHtml(w.file_path)}">播放</button>` : ''}
                            ${w.file_path ? `<a class="btn btn-sm btn-secondary" href="/api/files/download/${w.file_path.split('/').map(encodeURIComponent).join('/')}" download>下载</a>` : ''}
                            ${w.status === 'failed' || w.status === 'completed' ? `<button class="btn btn-sm btn-secondary" onclick="retryWork(${w.id})">重新下载</button>` : ''}
                            <button class="btn btn-sm btn-danger" onclick="deleteWork(${w.id})">删除</button>
                        </div>
                    </td>
                </tr>
            `;
        }).join('');
    } catch (e) {
        console.error('Load works error:', e);
        const msg = errorDetail(e, '加载作品列表失败');
        const tbody = document.getElementById('worksTableBody');
        if (tbody) tbody.innerHTML = tableErrorRow(8, msg);
        showToast(msg, 'error');
    }
}

async function retryWork(id) {
    try {
        const res = await API.post(`/api/works/${id}/retry`, {});
        showToast(res.message || '已加入下载队列', 'success');
        setTimeout(() => { loadRooms(); loadWorksTable(); }, 3000);
    } catch (e) { showToast(errorDetail(e, '操作失败'), 'error'); }
}

async function deleteWork(id) {
    if (!confirm('确认删除该作品记录？已下载的文件也会从磁盘删除。')) return;
    try {
        await API.delete(`/api/works/${id}`);
        showToast('删除成功', 'success');
        loadRooms();
        loadWorksTable();
    } catch (e) { showToast(errorDetail(e, '删除失败'), 'error'); }
}

// 系统设置
// 敏感项后端只回掩码（登录态/代理账密不外泄），输入框因此永远留空：
// 空=保持不变，粘贴新值=覆盖，单独输入 -=清除
const SENSITIVE_FIELDS = {
    webhook_url: 'set_webhook_url',
    proxy_addr: 'set_proxy_addr',
    douyin_cookie: 'set_douyin_cookie',
    bilibili_cookie: 'set_bilibili_cookie',
    kuaishou_cookie: 'set_kuaishou_cookie',
};

function fillSensitive(s) {
    for (const [key, id] of Object.entries(SENSITIVE_FIELDS)) {
        const el = document.getElementById(id);
        if (!el) continue;
        el.value = '';
        if (!el.dataset.origPh) el.dataset.origPh = el.placeholder;
        el.placeholder = s[key] ? `${s[key]}，留空=保持不变 · 粘贴新值=覆盖 · 输入 -=清除` : el.dataset.origPh;
    }
}

async function loadSettings() {
    try {
        loadCookieStatus().catch(() => {});
        const info = await API.get('/api/system/info');
        const s = info.settings || {};

        setField('set_record_format', s.record_format);
        setField('set_segment_time', s.segment_time);
        setField('set_monitor_interval', s.monitor_interval);
        setField('set_check_timeout', s.check_timeout);
        setField('set_max_disk_usage', s.max_disk_usage);
        setField('set_daily_merge_max_gb', s.daily_merge_max_gb);
        setField('set_output_dir', s.output_dir);
        setField('set_filename_template', s.filename_template);
        fillSensitive(s);
        setCheck('set_enable_notification', s.enable_notification);
        setCheck('set_enable_proxy', s.enable_proxy);
        setField('set_works_poll_interval', s.works_poll_interval);
        setField('set_works_backfill_limit', s.works_backfill_limit);
        // 风控保护项
        setCheck('set_works_rate_limit_enabled', s.works_rate_limit_enabled);
        setField('set_works_check_jitter_min', s.works_check_jitter_min);
        setField('set_works_check_jitter_max', s.works_check_jitter_max);
        setField('set_works_page_interval', s.works_page_interval);
        setField('set_works_download_gap_min', s.works_download_gap_min);
        setField('set_works_download_gap_max', s.works_download_gap_max);
        setField('set_works_backoff_base', s.works_backoff_base);
        setField('set_works_backoff_max', s.works_backoff_max);
        // 合集下载：画质码（select 用字符串匹配）与通道（布尔转 'true'/'false'）
        setField('set_collection_qn', String(s.collection_qn ?? 127));
        setField('set_collection_prefer_dash', s.collection_prefer_dash ? 'true' : 'false');
        setCheck('set_works_auto_download', s.works_auto_download);
        setCheck('set_sync_enabled', s.sync_enabled);
        setField('set_sync_root', s.sync_root);
        setField('set_sync_interval', s.sync_interval);

        const platforms = asList(await API.get('/api/system/platforms'), '平台列表');
        document.getElementById('cfgPlatforms').innerHTML = platforms.map(p =>
            // 顺带修未转义插值：数据源是后端硬编码的平台表，当前不可利用，但颜色/名称
            // 一旦改为可配置即成为注入点。color 只允许 #RRGGBB 才拼进 style。
            `<span class="platform-tag" style="background:${_safeColor(p.color)}22;color:${_safeColor(p.color)}">${escapeHtml(p.name)}</span>`
        ).join(' ');

        updateFilenamePreview();

        // 加载日志
        const logs = asList(await API.get('/api/system/logs?limit=50'), '系统日志');
        const container = document.getElementById('logContainer');
        if (logs.length === 0) {
            container.innerHTML = '<div class="empty-state">暂无日志</div>';
        } else {
            // level 用于 class 名（只允许字母），message 已经 escapeHtml；
            // 原实现 l.level 直接拼进 class 与文本，属未转义插值
            container.innerHTML = logs.map(l => {
                const level = _safeLevel(l.level);
                return `
                <div class="log-entry">
                    <span class="log-time">${formatTime(l.created_at)}</span>
                    <span class="log-level ${level}">${escapeHtml(level.toUpperCase())}</span>
                    <span class="log-msg">${escapeHtml(l.message)}</span>
                </div>
            `;
            }).join('');
        }
    } catch (e) {
        console.error('Load settings error:', e);
        showToast(errorDetail(e, '加载设置失败'), 'error');
    }
}

// 颜色白名单：只接受 #RGB / #RRGGBB，其余回退中性灰（防止 style 属性注入）
function _safeColor(c) {
    return /^#[0-9a-fA-F]{3,8}$/.test(String(c || '')) ? c : '#888';
}

// 日志级别白名单：拼进 class 名的值必须是已知级别，否则回退 info
function _safeLevel(level) {
    const lv = String(level || '').toLowerCase();
    return ['debug', 'info', 'warning', 'error', 'critical'].includes(lv) ? lv : 'info';
}

// 表单辅助：根据 id 设置值/勾选状态
function setField(id, value) {
    const el = document.getElementById(id);
    if (el) el.value = value ?? '';
}
function setCheck(id, checked) {
    const el = document.getElementById(id);
    if (el) el.checked = !!checked;
}
function getField(id) {
    const el = document.getElementById(id);
    return el ? el.value : '';
}
function getCheck(id) {
    const el = document.getElementById(id);
    return el ? el.checked : false;
}

// 保存设置
// F-3: daily_merge_max_gb / works_backfill_limit 的 0 有语义（0=关闭当日合并 / 0=回填不限制）。
// 原实现 parseFloat(...) || 0 把"清空输入框"也变成 0 提交 —— 等于静默关掉当日合并、
// 把回填改成全量。改为：只有输入框非空（用户显式输入了数字）才提交，留空一律不提交。
// 返回 undefined 表示"未填写/非法"，由调用方从 payload 中剔除。
function numberField(id) {
    const raw = getField(id).trim();
    if (raw === '') return undefined;              // 留空 = 不提交（保持后端现值）
    const n = Number(raw);
    return Number.isFinite(n) ? n : undefined;     // 非法输入同样不提交，避免提交 NaN
}

async function saveSettings(e) {
    const payload = {
        record_format: getField('set_record_format'),
        segment_time: parseInt(getField('set_segment_time'), 10) || 0,
        monitor_interval: parseInt(getField('set_monitor_interval'), 10) || 0,
        check_timeout: parseInt(getField('set_check_timeout'), 10) || 0,
        max_disk_usage: parseInt(getField('set_max_disk_usage'), 10) || 0,
        output_dir: getField('set_output_dir'),
        filename_template: getField('set_filename_template'),
        enable_notification: getCheck('set_enable_notification'),
        enable_proxy: getCheck('set_enable_proxy'),
        works_auto_download: getCheck('set_works_auto_download'),
        sync_enabled: getCheck('set_sync_enabled'),
        sync_root: getField('set_sync_root').trim(),
        sync_interval: parseInt(getField('set_sync_interval'), 10) || 0,
    };

    // 0 有语义的两个字段：留空不提交，显式输入 0 才提交 0
    const dailyMerge = numberField('set_daily_merge_max_gb');
    if (dailyMerge !== undefined) payload.daily_merge_max_gb = dailyMerge;
    const backfillLimit = numberField('set_works_backfill_limit');
    if (backfillLimit !== undefined) payload.works_backfill_limit = Math.trunc(backfillLimit);

    // 正整数字段留空/为 0 时不提交（后端仅更新传入项），避免保存 Cookie 等其它
    // 设置时被无关字段的"必须为正数"校验误拦
    for (const k of ["segment_time", "monitor_interval", "check_timeout",
                     "max_disk_usage", "works_check_count", "sync_interval"]) {
        if (!(payload[k] > 0)) delete payload[k];
    }
    // 风控保护项：0 有语义（=该间隔不等待），因此留空才不提交、显式 0 要提交
    payload.works_rate_limit_enabled = getCheck('set_works_rate_limit_enabled');
    for (const k of ["works_check_jitter_min", "works_check_jitter_max",
                     "works_page_interval", "works_download_gap_min",
                     "works_download_gap_max", "works_backoff_base",
                     "works_backoff_max"]) {
        const v = numberField('set_' + k);
        if (v !== undefined) payload[k] = Math.trunc(v);
    }

    // 合集下载：画质码是正整数（留空/0 不提交），通道是布尔
    const collQn = parseInt(getField('set_collection_qn'), 10);
    if (collQn > 0) payload.collection_qn = collQn;
    payload.collection_prefer_dash = getField('set_collection_prefer_dash') === 'true';

    // 检测间隔留空则不提交（后端只更新传入字段），避免 0 被"必须为正数"校验拒绝
    const pollInterval = parseInt(getField('set_works_poll_interval'), 10);
    if (pollInterval > 0) payload.works_poll_interval = pollInterval;
    // 敏感项未改动则不提交，否则保存其它设置时会把空输入框当成"清除"写掉凭据
    for (const [key, id] of Object.entries(SENSITIVE_FIELDS)) {
        const val = getField(id).trim();
        if (val === '-') payload[key] = '';
        else if (val) payload[key] = val;
    }

    const btn = e && e.target;
    const oldLabel = btn ? btn.textContent : '';
    if (btn) { btn.disabled = true; btn.textContent = '保存中...'; }

    try {
        const res = await API.put('/api/system/settings', payload);
        // F-22 之后失败会抛 HttpError，这里 success 分支基本必达；
        // 保留 res.success 判断以防后端返回非 success 的 200
        if (res && res.success === false) {
            showToast('保存失败: ' + (res.detail || res.message || '未知错误'), 'error');
        } else {
            showToast('设置已保存并生效', 'success');
            await loadSettings();
        }
    } catch (e) {
        showToast('保存失败: ' + errorDetail(e, '未知错误'), 'error');
    } finally {
        if (btn) { btn.disabled = false; btn.textContent = oldLabel; }
    }
}

// 立即执行 NAS 同步
async function syncNow() {
    const btn = document.getElementById('syncNowBtn');
    const old = btn ? btn.textContent : '';
    if (btn) { btn.disabled = true; btn.textContent = '同步中...'; }
    try {
        const res = await API.post('/api/system/sync/run', {});
        showToast(res.message || '同步完成', 'success');
    } catch (e) {
        // 后端错误（未配置同步根目录 / 同步失败）现在是 HttpError.detail，不再走网络错误文案
        showToast(errorDetail(e, '同步失败'), 'error');
    } finally {
        if (btn) { btn.disabled = false; btn.textContent = old; }
    }
}

// 重置表单为当前已保存值
function resetSettingsForm() {
    loadSettings();
    showToast('已重置为已保存的设置', 'info');
}

// 实时预览最终输出路径（基于模板 + 当前格式 + 示例值）
function updateFilenamePreview() {
    const tmpl = (document.getElementById('set_filename_template')?.value || '{streamer}_{time}').trim();
    const fmt = document.getElementById('set_record_format')?.value || 'ts';
    const outputDir = document.getElementById('set_output_dir')?.value || '/app/recordings';

    const sample = {
        streamer: '主播名',
        room_id: '123456',
        platform: '抖音',
        title: '测试标题',
        date: '2026-07-26',
        time: '193000',
        datetime: '20260726_193000',
    };

    let base = tmpl;
    for (const [k, v] of Object.entries(sample)) {
        base = base.split('{' + k + '}').join(v);
    }
    // 粗略清洗，与后端 _sanitize_filename 对齐
    base = base.replace(/[\\/:*?"<>|\n\r\t]/g, '_').trim().replace(/^[._]+|[._]+$/g, '').slice(0, 100) || 'untitled';

    const el = document.getElementById('filenamePreview');
    if (el) {
        el.textContent = `输出示例：${outputDir}/抖音/主播名/2026-07-26/${base}.${fmt}`;
    }
}

// 导入 / 导出辅助
function dateStr() {
    const d = new Date();
    return d.getFullYear() + String(d.getMonth() + 1).padStart(2, '0') + String(d.getDate()).padStart(2, '0');
}

// F-22: 导出接口返回的是文件文本（非 JSON），不能走 api()；但它同样必须校验 res.ok，
// 并且抛同一种 HttpError，这样 catch 分支的 errorDetail 能统一透出后端 detail。
async function fetchText(url) {
    const r = await fetch(url, { headers: { 'Accept': 'application/json, text/plain' } });
    const text = await r.text();
    if (!r.ok) {
        let data = null;
        try { data = JSON.parse(text); } catch (e) { data = null; }
        throw new HttpError(r.status, _detailOf(data, r.status), url);
    }
    return text;
}

function downloadText(content, filename, mime = 'application/json') {
    const blob = new Blob([content], { type: mime });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
}

// 房间管理：导出
async function exportRooms() {
    try {
        const text = await fetchText('/api/rooms/export');
        downloadText(text, 'rooms_export_' + dateStr() + '.json');
        showToast('房间已导出', 'success');
    } catch (e) {
        console.error('Export rooms error:', e);
        showToast(errorDetail(e, '导出失败'), 'error');
    }
}

// 房间管理：从文件导入
async function importRoomsFromFile(input) {
    const file = input.files[0];
    if (!file) return;
    try {
        const text = await file.text();
        const data = JSON.parse(text);
        const rooms = Array.isArray(data) ? data : (data.rooms || []);
        if (!rooms.length) {
            showToast('文件中没有房间数据', 'error');
            return;
        }
        const res = await API.post('/api/rooms/import', { rooms });
        showToast(res.message || `导入完成：新增 ${res.imported}`, res.failed ? 'error' : 'success');
        if (res.errors && res.errors.length) console.warn('房间导入错误:', res.errors);
        loadRooms();
    } catch (e) {
        console.error('Import rooms error:', e);
        // HttpError 说明文件本身合法、后端拒绝了内容（detail 有具体原因）；
        // 其余（JSON.parse 失败 / TypeError）才是格式问题
        showToast(e && e.name === 'HttpError'
            ? errorDetail(e, '导入失败')
            : '导入失败：文件格式错误', 'error');
    } finally {
        input.value = '';
    }
}

// 系统设置：导出
async function exportSettings() {
    try {
        const text = await fetchText('/api/system/settings/export');
        downloadText(text, 'settings_export_' + dateStr() + '.json');
        showToast('设置已导出', 'success');
    } catch (e) {
        console.error('Export settings error:', e);
        showToast(errorDetail(e, '导出失败'), 'error');
    }
}

// 系统设置：从文件导入
async function importSettingsFromFile(input) {
    const file = input.files[0];
    if (!file) return;
    try {
        const text = await file.text();
        const data = JSON.parse(text);
        const payload = data.settings ? data.settings : data;
        const res = await API.post('/api/system/settings/import', payload);
        showToast(res.message || '设置已导入并生效', 'success');
        await loadSettings();
    } catch (e) {
        console.error('Import settings error:', e);
        showToast(e && e.name === 'HttpError'
            ? errorDetail(e, '导入失败')
            : '导入失败：文件格式错误', 'error');
    } finally {
        input.value = '';
    }
}

// 初始化
document.addEventListener('DOMContentLoaded', () => {
    refreshDashboard();
    // 自动刷新仪表盘
    refreshTimer = setInterval(() => {
        if (currentPage === 'dashboard') refreshDashboard();
    }, 15000);

    // 文件卡片操作按钮：事件委托（路径含引号时内联 onclick 字符串拼接会被破坏）
    const filesBox = document.getElementById('filesGroups');
    if (filesBox) {
        filesBox.addEventListener('click', (e) => {
            const btn = e.target.closest('button[data-act]');
            if (!btn) return;
            const p = btn.dataset.path || '';
            if (btn.dataset.act === 'play') playFile(p);
            else if (btn.dataset.act === 'delete') deleteFile(p);
        });
    }

    // 作品表格播放按钮（文件位于 主播/作品/ 下，复用文件播放接口）
    const worksBody = document.getElementById('worksTableBody');
    if (worksBody) {
        worksBody.addEventListener('click', (e) => {
            const btn = e.target.closest('button[data-act="play-work"]');
            if (btn) playFile(btn.dataset.path || '');
        });
    }
});

// 键盘快捷键
document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') {
        hideAddRoomModal();
        hideEditRoomModal();
        hidePlayer();
    }
});

// ============================================================ 合集下载
// 与「作品订阅」互补：作品订阅按主播周期拉新作品；这里按**一个合集链接**一次性
// 把该合集全部集数导入 works 表，复用同一套下载队列（断点续传/去重/限速/水位）。
// 流程：粘贴链接 -> 解析预览（不落库）-> 勾选 -> 导入 -> 后台队列下载 -> 进度轮询。

let collectionPreview = null;     // 最近一次解析结果
let collectionTimer = null;       // 已导入合集的刷新定时器

async function parseCollection() {
    const input = document.getElementById('collectionUrl');
    const btn = document.getElementById('parseCollectionBtn');
    const url = (input?.value || '').trim();
    if (!url) { showToast('请先粘贴合集链接', 'warning'); input?.focus(); return; }

    const oldLabel = btn ? btn.textContent : '';
    if (btn) { btn.disabled = true; btn.textContent = '解析中...'; }
    try {
        const data = await API.post('/api/collections/preview', { url });
        collectionPreview = data;
        renderCollectionPreview(data);
        showToast(`解析成功：${data.kind_name} 共 ${data.total} 集`, 'success');
    } catch (e) {
        collectionPreview = null;
        const card = document.getElementById('collectionPreviewCard');
        if (card) card.style.display = 'none';
        showToast(errorDetail(e, '解析失败'), 'error');
    } finally {
        if (btn) { btn.disabled = false; btn.textContent = oldLabel || '解析'; }
    }
}

function renderCollectionPreview(data) {
    const card = document.getElementById('collectionPreviewCard');
    const title = document.getElementById('collectionPreviewTitle');
    const meta = document.getElementById('collectionMeta');
    const tbody = document.getElementById('collectionPreviewBody');
    if (!card || !tbody) return;

    card.style.display = '';
    if (title) title.textContent = `${data.kind_name}：${data.title || '(无标题)'}`;
    if (meta) {
        const totalSec = data.items.reduce((s, it) => s + (Number(it.duration) || 0), 0);
        meta.textContent = `共 ${data.total} 集 · 总时长 ${formatDuration(totalSec) || '-'}`;
    }
    tbody.innerHTML = data.items.map(it => `
        <tr>
            <td class="col-check">
                <input type="checkbox" class="collection-check" data-idx="${it.episode_index}" checked>
            </td>
            <td>${escapeHtml(String(it.episode_index))}</td>
            <td title="${escapeHtml(it.title || '')}">${escapeHtml((it.title || '').slice(0, 60) || '-')}</td>
            <td>${formatDuration(it.duration)}</td>
        </tr>
    `).join('');
    const all = document.getElementById('collectionCheckAll');
    if (all) all.checked = true;
    updateCollectionImportBtn();
}

function collectionSelectAll(checked) {
    document.querySelectorAll('.collection-check').forEach(c => { c.checked = !!checked; });
    const all = document.getElementById('collectionCheckAll');
    if (all) all.checked = !!checked;
    updateCollectionImportBtn();
}

function updateCollectionImportBtn() {
    const btn = document.getElementById('collectionImportBtn');
    if (!btn) return;
    const n = document.querySelectorAll('.collection-check:checked').length;
    btn.disabled = n < 1;
    btn.textContent = n >= 1 ? `下载选中（${n}）` : '下载选中';
}

async function importCollection() {
    if (!collectionPreview) { showToast('请先解析链接', 'warning'); return; }
    const picked = [...document.querySelectorAll('.collection-check:checked')]
        .map(c => parseInt(c.dataset.idx, 10))
        .filter(n => Number.isInteger(n));
    if (!picked.length) { showToast('请至少选择 1 集', 'warning'); return; }

    const btn = document.getElementById('collectionImportBtn');
    const oldLabel = btn ? btn.textContent : '';
    if (btn) { btn.disabled = true; btn.textContent = '导入中...'; }
    try {
        const res = await API.post('/api/collections/import', {
            url: (document.getElementById('collectionUrl')?.value || '').trim(),
            indices: picked,
        });
        showToast(res.message || '已加入下载队列', 'success');
        await loadCollections();
        startCollectionPolling();
    } catch (e) {
        showToast(errorDetail(e, '导入失败'), 'error');
    } finally {
        if (btn) { btn.disabled = false; btn.textContent = oldLabel || '下载选中'; }
        updateCollectionImportBtn();
    }
}

async function loadCollections() {
    const tbody = document.getElementById('collectionsBody');
    if (!tbody) return;
    try {
        const list = asList(await API.get('/api/collections'), '合集列表');
        // 还有下载中/待下载的集 -> 保持轮询；全部结束则停
        collectionBusy = list.some(c => (c.pending || 0) > 0 || (c.downloading || 0) > 0);
        if (!list.length) {
            tbody.innerHTML = '<tr><td colspan="8" class="empty-state">还没有导入过合集，粘贴链接解析后下载</td></tr>';
            return;
        }
        tbody.innerHTML = list.map(c => {
            const done = c.completed || 0;
            const total = c.total || 0;
            const pct = total ? Math.round(done * 100 / total) : 0;
            return `
            <tr>
                <td title="${escapeHtml(c.collection_title || '')}">${escapeHtml((c.collection_title || c.collection_key || '-').slice(0, 40))}</td>
                <td>${total}</td>
                <td>${done}${pct ? ` <span class="form-hint">(${pct}%)</span>` : ''}</td>
                <td>${c.downloading || 0}</td>
                <td>${c.pending || 0}</td>
                <td>${c.failed ? `<span class="status-badge status-failed">${c.failed}</span>` : 0}</td>
                <td>${formatSize(c.size_mb)}</td>
                <td>
                    <div class="action-group">
                        <button class="btn btn-sm btn-secondary" onclick="toggleCollectionItems('${escapeHtml(c.collection_key)}')">详情</button>
                        ${c.failed ? `<button class="btn btn-sm btn-secondary" onclick="retryCollection('${escapeHtml(c.collection_key)}')">重试失败</button>` : ''}
                    </div>
                </td>
            </tr>
            <tr class="collection-items-row" id="ci-${escapeHtml(c.collection_key)}" style="display:none">
                <td colspan="8"><div class="form-hint">加载中...</div></td>
            </tr>`;
        }).join('');
    } catch (e) {
        const msg = errorDetail(e, '加载合集列表失败');
        tbody.innerHTML = tableErrorRow(8, msg);
        showToast(msg, 'error');
    }
}

async function toggleCollectionItems(key) {
    const row = document.getElementById('ci-' + key);
    if (!row) return;
    const showing = row.style.display !== 'none';
    if (showing) { row.style.display = 'none'; return; }
    row.style.display = '';
    try {
        const items = asList(await API.get(`/api/collections/${encodeURIComponent(key)}/items`), '合集明细');
        const STATUS = { pending: ['status-offline', '待下载'], downloading: ['status-recording', '下载中'],
                         completed: ['status-completed', '已完成'], failed: ['status-failed', '失败'] };
        row.cells[0].innerHTML = items.map(it => {
            const [cls, text] = STATUS[it.status] || ['status-offline', it.status];
            return `<div style="display:flex;gap:8px;align-items:center;padding:2px 0">
                <span style="min-width:44px">#${escapeHtml(String(it.episode_index))}</span>
                <span class="status-badge ${cls}">${escapeHtml(text)}</span>
                <span title="${escapeHtml(it.title || '')}">${escapeHtml((it.title || '').slice(0, 46) || '-')}</span>
                <span class="form-hint">${formatSize(it.file_size_mb)}</span>
                ${it.error_message ? `<span class="form-hint" title="${escapeHtml(it.error_message)}">${escapeHtml(it.error_message.slice(0, 40))}</span>` : ''}
            </div>`;
        }).join('');
    } catch (e) {
        row.cells[0].innerHTML = `<div class="form-hint">${escapeHtml(errorDetail(e, '加载失败'))}</div>`;
    }
}

async function retryCollection(key) {
    try {
        const res = await API.post(`/api/collections/${encodeURIComponent(key)}/retry`, {});
        showToast(res.message || '已重新入队', 'success');
        await loadCollections();
        startCollectionPolling();
    } catch (e) {
        showToast(errorDetail(e, '重试失败'), 'error');
    }
}

// 有在下载的集时每 5s 刷新一次；全部结束后自动停止，避免无谓轮询。
// 是否继续轮询由后端返回的 pending/downloading 计数决定（不靠猜 DOM 文本）。
let collectionBusy = false;

function startCollectionPolling() {
    collectionBusy = true;
    if (collectionTimer) return;
    collectionTimer = setInterval(async () => {
        if (currentPage !== 'collections') { stopCollectionPolling(); return; }
        await loadCollections();
        if (!collectionBusy) stopCollectionPolling();
    }, 5000);
}

function stopCollectionPolling() {
    if (collectionTimer) { clearInterval(collectionTimer); collectionTimer = null; }
}

// 预览勾选框变化时更新按钮
document.addEventListener('change', (e) => {
    if (e.target && e.target.classList && e.target.classList.contains('collection-check')) {
        updateCollectionImportBtn();
    }
});

// ================================================== Cookie 健康状态
// 解决「Cookie 什么时候失效了不知道」：每次作品拉取后由后端记录实际结果，
// 这里负责把「已失效」变成用户看得见的告警（仪表盘红条 + 设置页状态点）。

async function loadCookieStatus() {
    try {
        const data = await API.get('/api/system/cookie-status');
        renderCookieAlert(data);
        renderCookieStatusBox(data);
        return data;
    } catch (e) {
        console.error('Cookie status error:', e);
        return null;
    }
}

function renderCookieAlert(data) {
    const box = document.getElementById('cookieAlert');
    if (!box) return;
    const bad = (data?.items || []).filter(i => i.status === 'invalid');
    const limited = (data?.items || []).filter(i => i.status === 'rate_limited');

    if (bad.length) {
        const names = bad.map(i => PLATFORM_NAMES[i.platform] || i.platform).join('、');
        box.className = 'alert-bar alert-error';
        box.innerHTML = `
            <span class="alert-icon">⚠</span>
            <span class="alert-body">
                <strong>${escapeHtml(names)} Cookie 已失效</strong>，该平台的作品订阅与下载已暂停。
                ${bad[0].last_error ? `<br><span class="form-hint">${escapeHtml(bad[0].last_error.slice(0, 160))}</span>` : ''}
            </span>
            <span class="alert-actions">
                <button class="btn btn-sm btn-primary" onclick="switchPage('settings')">去设置</button>
            </span>`;
        box.style.display = '';
        return;
    }
    if (limited.length) {
        const names = limited.map(i => PLATFORM_NAMES[i.platform] || i.platform).join('、');
        box.className = 'alert-bar alert-warn';
        box.innerHTML = `
            <span class="alert-icon">⏳</span>
            <span class="alert-body">
                <strong>${escapeHtml(names)} 被平台限流</strong>，已自动放慢请求节奏。
                这不是 Cookie 问题，稍后会自动恢复；若持续出现可调大「作品检测间隔」。
            </span>`;
        box.style.display = '';
        return;
    }
    box.style.display = 'none';
}

function renderCookieStatusBox(data) {
    const box = document.getElementById('cookieStatusBox');
    if (!box) return;
    const items = data?.items || [];
    if (!items.length) { box.textContent = '暂无数据'; return; }
    box.innerHTML = items.map(i => {
        const name = PLATFORM_NAMES[i.platform] || i.platform;
        const ago = i.last_ok_ago != null ? formatDuration(i.last_ok_ago) + '前验证通过' : '尚未验证';
        const extra = i.status === 'invalid' && i.paused_remaining
            ? ` · 暂停 ${formatDuration(i.paused_remaining)}`
            : '';
        return `<span class="cookie-status-item" title="${escapeHtml(i.last_error || '')}">
            <span class="cookie-dot ${escapeHtml(i.status)}"></span>
            <span>${escapeHtml(name)}</span>
            <span class="form-hint">${escapeHtml(i.status_text)}${i.last_ok_ago != null ? ' · ' + ago : ''}${extra}</span>
        </span>`;
    }).join('');
}
