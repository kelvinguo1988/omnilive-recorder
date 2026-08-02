# OmniLive Recorder · 多平台直播录制平台

> 支持 **抖音 / Bilibili(哔哩哔哩) / 快手** 三大平台的直播自动监控与录制，提供 Web 管理界面，一键 Docker 部署。

本项目参考了社区优秀的开源方案：
- [BililiveRecorder/BililiveRecorder](https://github.com/BililiveRecorder/BililiveRecorder) — B 站直播录制思路
- [ihmily/DouyinLiveRecorder](https://github.com/ihmily/DouyinLiveRecorder) — 抖音直播拉流录制思路

在二者基础上，整合为**统一的多平台录制服务 + Web 管理后台**，并用 FFmpeg 直接拉流拷贝（`-c copy`），几乎零性能损耗。

---

## ✨ 功能特性

- **多平台支持**：抖音、B 站、快手，URL 自动识别平台（无需手动选择）。
- **开播自动录制**：监控调度器定时检测房间状态，开播即自动开始录制，下播自动停止。
- **Web 管理后台**：
  - 仪表盘：房间数 / 直播中 / 录制中 / 系统资源（CPU、内存、磁盘）实时展示。
  - 房间管理：添加 / 删除 / 启用停用、手动检测、手动开始 / 停止录制。
  - 录制记录：查看历史录制文件、时长、大小、状态。
  - 文件管理：在线播放、下载、**批量删除**、合并碎片录制文件。
- **断流重连续写同一场**：录制进程意外退出但主播仍在直播时，自动重连并**续写同一场录制**（追加一个新分片，不新建记录）；下播时把所有分片自动合并为单个文件，从根源消除「断流产生碎片」问题。
- **断流自恢复**：录制进程意外退出时自动重连，直播中则重新开始录制。
- **分段录制**：支持按时间切片（默认 30 分钟一段），避免单文件过大。**TS / MP4 支持自动分段**；FLV 因封装特性始终为单文件。
- **RESTful API**：所有功能均暴露为 API，便于二次开发与自动化。
- **Docker 一键部署**：内置 `Dockerfile` 与 `docker-compose.yml`，开箱即用。

---

## 🧱 架构

```
┌──────────────┐   HTTP    ┌─────────────────────────────┐
│  Web 浏览器   │ ───────▶ │  FastAPI (Web + API)        │
└──────────────┘           │  ┌───────────────────────┐  │
                           │  │  Monitor 监控调度器    │  │
                           │  │  (定时检测房间状态)    │  │
                           │  └───────────┬───────────┘  │
                           │              │ 发现开播     │
                           │  ┌───────────▼───────────┐  │
                           │  │  Platform Adapters    │  │
                           │  │  douyin/bilibili/      │  │
                           │  │  kuaishou (取流地址)  │  │
                           │  └───────────┬───────────┘  │
                           │              │ 流地址       │
                           │  ┌───────────▼───────────┐  │
                           │  │  FFmpeg Recorder       │  │
                           │  │  (拉流 -c copy 录制)   │  │
                           │  └───────────┬───────────┘  │
                           └──────────────┼──────────────┘
                                          ▼
                                recordings/<平台>/<主播>/<日期>/*.ts
```

- **FastAPI**：Web 界面静态托管 + 后端 API。
- **SQLite (aiosqlite)**：轻量存储房间、录制记录、系统日志。
- **FFmpeg**：核心录制引擎，直接拉流拷贝，支持 FLV / HLS(m3u8) 输入。
- **平台适配器**：各平台独立实现，遵循统一接口（`get_room_info`），易于扩展。
- **监控调度器**：后台异步循环，按配置间隔检测所有启用房间。

---

## 🚀 快速开始（Docker 推荐）

### 1. 使用 docker-compose

```bash
# 克隆仓库
git clone https://github.com/<your-username>/omnilive-recorder.git
cd omnilive-recorder

# 启动（后台运行）
docker compose up -d

# 查看日志
docker compose logs -f
```

打开浏览器访问 **http://localhost:8000** 即可使用。

### 2. 仅用 Docker 运行

```bash
docker build -t omnilive-recorder .
docker run -d \
  --name omnilive-recorder \
  -p 8000:8000 \
  -v $(pwd)/recordings:/app/recordings \
  -v $(pwd)/data:/app/data \
  -v $(pwd)/config:/app/config \
  omnilive-recorder
```

> 建议挂载 `recordings`、`data`、`config` 三个卷，保证录制文件与数据库在容器重建后不丢失。

### NAS / 小容器磁盘部署（推荐）

容器自身分配磁盘较小，而 Docker 数据根目录就在 NAS 存储池（`CACHEDEV1_DATA`），因此**使用 named volume 即可让录制文件自动落 NAS，且部署零额外配置**：

- **方式一 · named volume（推荐，已默认启用）**：`docker-compose.yml` 中把录制卷声明为 named volume：
  ```yaml
  services:
    live-recorder:
      volumes:
        - omni-recordings:/app/recordings
  volumes:
    omni-recordings:
      driver: local
  ```
  `docker compose up` 时 Docker 自动在 NAS 盘的 Docker 卷目录（`/share/CACHEDEV1_DATA/Container/.../volumes/omni-recordings/_data`）创建该卷，文件直接写 NAS、不占容器磁盘——与 bililiverecorder 行为完全一致。系统设置里 `output_dir` 填 `/app/recordings` 即可，无需填任何 NAS 物理路径。
- **方式二 · 精确指定到已有目录 / 复用其它卷**：把录制卷改为 bind mount 宿主机路径，例如复用你已有的 `744c...` 卷物理目录：
  ```yaml
  volumes:
    - /share/CACHEDEV1_DATA/Container/.../volumes/744c.../_data:/app/recordings
  ```
  或在 Container Station UI 的"卷"里把主机路径改为该 NAS 目录、容器路径保持 `/app/recordings`，应用并重建容器。
- 系统设置里 `output_dir` 始终填容器内的挂载点 `/app/recordings`（或其子目录）。只要该挂载点来源是 NAS 卷，文件即直写 NAS，不占容器磁盘。

---

## 🔧 配置

配置文件位于 `config/config.ini`，或通过环境变量（前缀 `LIVE_RECORDER_`）覆盖。

> **Web 界面配置**：系统设置页现在支持可视化配置（录制格式、监控间隔、分段时长、输出目录、**输出文件名模板**、代理、抖音 / B站 / 快手 Cookie、通知等）。保存后**立即生效**，并自动写回 `config.ini`，容器重启后依然保留。后端接口：`PUT /api/system/settings`。

| 配置项 | 说明 | 默认值 |
| --- | --- | --- |
| `record_format` | 录制格式：`ts` / `flv` / `mp4` | `ts` |
| `monitor_interval` | 监控检测间隔（秒） | `120` |
| `segment_time` | 分段时长（秒），`0` 为不分段。**仅 TS / MP4 生效**，FLV 始终单文件 | `1800` |
| `filename_template` | 输出文件名模板，支持占位符（见下） | `{streamer}_{time}` |
| `max_retries` | 录制失败最大重试次数 | `3` |
| `output_dir` | 录制输出根目录（实际文件位于 `{output_dir}/{平台}/{主播}/{日期}/` 下） | `/app/recordings` |
| ⚠️ 路径说明 | `output_dir` 填**容器内部已挂载的目录**（默认 `/app/recordings`）。能否落 NAS 取决于部署时该目录是否绑定到 NAS 卷：把宿主机 NAS 目录以卷形式挂到 `/app/recordings`（如 compose 写 `/Container/.../volumes/.../_data:/app/recordings`，或 Container Station "卷"里改主机路径为 NAS 目录），此处填 `/app/recordings` 即可直写 NAS、不占容器磁盘；切勿填容器内不存在的路径（会落到临时层、重建即丢） | — |
| `douyin_cookie` | 抖音 Cookie（提高解析成功率，可选） | 空 |
| `bilibili_cookie` | B站 Cookie（**可选**；留空则自动获取游客 buvid3 绕过风控。原画 `qn=10000` 需带游客标识才能拿到流地址） | 空 |
| `kuaishou_cookie` | 快手 Cookie（可选；公共直播间通常无需登录态，仅个别受限网络手动填写） | 空 |
| `enable_proxy` / `proxy_addr` | 代理开关与地址（海外/受限网络可用） | 关闭 |

> **B站 Cookie 说明**：B站裸请求会被风控拦截（`code=-352`），必须带一个真实有效的 `buvid3` 游客标识。**默认（留空）会自动访问 `bilibili.com` 获取游客 `buvid3`**，无需登录即可录制原画；若自动获取仍被风控，可在「系统设置 → B站 Cookie」手动粘贴浏览器中的 `buvid3` / `SESSDATA` 等。快手公共直播间一般无需 Cookie，仅在受限网络失败时可手动填写。

**输出文件名模板 `filename_template` 占位符**：

| 占位符 | 含义 | 示例 |
| --- | --- | --- |
| `{streamer}` | 主播名（自动清洗，无则回退房间 ID） | `主播A` |
| `{room_id}` | 房间 ID | `465921689483` |
| `{platform}` | 平台中文名 | `抖音` |
| `{title}` | 直播标题（自动清洗） | `周末演唱会` |
| `{remark}` | 添加房间时填写的备注（无则留空） | `晚会录制` |
| `{date}` | 日期 `YYYY-MM-DD` | `2026-07-26` |
| `{time}` | 时间 `HHMMSS` | `193000` |
| `{datetime}` | 日期时间 `YYYYMMDD_HHMMSS` | `20260726_193000` |

> 模板不能包含路径分隔符 `/` 或 `\`，且替换后不能为空。建议至少包含 `{streamer}` 或 `{room_id}`，避免不同直播生成相同文件名被覆盖。
> 系统设置页「输出文件名模板」输入框下方有**实时路径预览**，可直接看到最终文件落点。

环境变量示例：

```bash
export LIVE_RECORDER_RECORD_FORMAT=mp4
export LIVE_RECORDER_MONITOR_INTERVAL=60
export LIVE_RECORDER_DOUYIN_COOKIE="your_cookie_here"
# B站 Cookie 留空即自动获取游客 buvid3；受限网络可手动指定
export LIVE_RECORDER_BILIBILI_COOKIE="buvid3=xxxx; SESSDATA=xxxx"
export LIVE_RECORDER_KUAISHOU_COOKIE="did=xxxx; clientid=xxxx"
```

---

## 📡 API 速览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/health` | 健康检查 |
| GET | `/api/rooms` | 房间列表 |
| POST | `/api/rooms` | 添加房间（自动识别平台） |
| PUT | `/api/rooms/{id}` | 更新房间 |
| DELETE | `/api/rooms/{id}` | 删除房间 |
| POST | `/api/rooms/{id}/check` | 手动检测房间 |
| POST | `/api/rooms/{id}/start-recording` | 手动开始录制 |
| POST | `/api/rooms/{id}/stop-recording` | 手动停止录制 |
| GET | `/api/rooms/export` | 导出全部房间为 JSON（备份 / 迁移） |
| POST | `/api/rooms/import` | 从 JSON 批量导入房间（自动跳过已存在 URL） |
| GET | `/api/recordings` | 录制记录列表（含 `part_count` 断流重连分片数） |
| GET | `/api/recordings/stats` | 录制统计 |
| GET | `/api/system/info` | 系统信息（含当前全部设置） |
| PUT | `/api/system/settings` | 更新系统设置（可视化设置页的保存动作，立即生效并持久化） |
| GET | `/api/system/settings/export` | 导出当前系统设置为 JSON（备份 / 迁移） |
| POST | `/api/system/settings/import` | 从 JSON 批量导入系统设置 |
| GET | `/api/system/platforms` | 支持的平台列表 |
| GET | `/api/system/logs` | 系统日志 |
| GET | `/api/files` | 文件列表 |
| GET | `/api/files/download/{path}` | 下载文件 |
| GET | `/api/files/play/{path}` | 在线播放 |
| DELETE | `/api/files/{path}` | 删除单个文件 |
| POST | `/api/files/batch-delete` | 批量删除文件（`{file_paths: [...]}`） |
| POST | `/api/files/merge` | 合并多个碎片文件 |

---

## 🧩 添加房间示例

在 Web 界面「房间管理」点击「添加房间」，粘贴直播间地址即可：

- 抖音：`https://live.douyin.com/123456789`
- B 站：`https://live.bilibili.com/12345`
- 快手：`https://live.kuaishou.com/u/xxxxxx`

平台会自动识别，添加后会立即检测一次直播状态。

---

## 📁 目录结构

```
omnilive-recorder/
├── app/
│   ├── main.py                 # FastAPI 入口
│   ├── config.py               # 配置管理
│   ├── database.py             # 数据库（SQLite）
│   ├── models.py               # ORM 模型
│   ├── routers/                # API 路由
│   │   ├── rooms.py
│   │   ├── recordings.py
│   │   ├── system.py
│   │   └── files.py
│   ├── services/
│   │   ├── recorder.py         # FFmpeg 录制引擎
│   │   ├── monitor.py          # 监控调度器
│   │   ├── file_manager.py     # 文件管理
│   │   └── platform/           # 平台适配器
│   │       ├── base.py
│   │       ├── douyin.py
│   │       ├── bilibili.py
│   │       └── kuaishou.py
│   └── static/                 # 前端界面
│       ├── index.html
│       ├── css/style.css
│       └── js/app.js
├── config/config.ini           # 配置文件
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
└── README.md
```

---

## ⚠️ 免责声明

本项目仅供个人学习与技术研究使用。请遵守各直播平台的服务条款与当地法律法规，
录制内容仅限个人留存，不得用于商业用途或侵犯他人权益。

---

## 📄 License

MIT
