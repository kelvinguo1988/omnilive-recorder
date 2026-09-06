# OmniLive Recorder · 多平台直播录制 + 作品订阅平台

> 支持 **抖音 / Bilibili(哔哩哔哩) / 快手** 三大平台的**直播自动录制**与**主播作品订阅下载**，统一主播管理（一个主播同时配置直播间与主页地址），可选 NAS 定期同步，提供 Web 管理界面，一键 Docker 部署。

本项目参考了社区优秀的开源方案：
- [BililiveRecorder/BililiveRecorder](https://github.com/BililiveRecorder/BililiveRecorder) — B 站直播录制思路
- [ihmily/DouyinLiveRecorder](https://github.com/ihmily/DouyinLiveRecorder) — 抖音直播拉流录制思路
- [Johnserf-Seed/f2](https://github.com/Johnserf-Seed/f2)（Apache-2.0）— 抖音 a_bogus 签名算法（已内化 `vendor/abogus.py`）
- [SocialSisterYi/bilibili-API-collect](https://github.com/SocialSisterYi/bilibili-API-collect) — B 站 API 文档（wbi 签名）

在二者基础上，整合为**统一的多平台录制服务 + Web 管理后台**，用 FFmpeg 直接拉流拷贝（`-c copy`），几乎零性能损耗。

---

## ✨ 功能特性

**直播录制**
- 开播自动录制、下播自动停止；断流自动重连续写同一场（多分片最终合并为单文件）
- 分段录制（TS/MP4 按时间切片，FLV 单文件），录制中流地址主动刷新消除「录几分钟就断」
- **当日自动合并**：同主播当天的多段录制（多次开播/下播），下播后自动合并为一个视频（总大小 ≤ 5GB 可配）
- 手动检测 / 手动开始 / 停止录制

**作品订阅**
- 添加主播主页链接，自动识别平台、解析昵称、**全量回填历史作品**
- 每 10 分钟（可配）检测新作品，自动下载视频与图集（图集打包 zip），抖音无水印直链
- 下载队列按平台串行 + 随机间隔限速防风控；检测与下载并行（回填期间下载不停摆）

**统一主播管理**
- 一个主播一条记录：直播间地址（直播录制）+ 主页地址（作品订阅）+ 各自开关
- 直播间检测自动识别主播平台用户 ID（B站已实测双向互通），之后无需主页地址也能订阅作品

**NAS 定期同步**
- 配置一个同步根目录，自动按主播名生成文件夹（`主播名/直播|作品/`），增量幂等复制到 QNAP 等挂载目录

**通用**
- Web 管理后台（仪表盘 / 主播管理 / 录制记录 / 作品列表 / 文件管理 / 系统设置）
- 文件在线播放、下载、合并、批量删除；导入导出主播与设置；可选 API Token 鉴权
- Docker 一键部署，SQLite 免运维

---

## 🚀 快速开始

### 1. Docker Compose 部署（推荐，含 QNAP Container Station「应用程序」方式）

```bash
git clone https://github.com/kelvinguo1988/omnilive-recorder.git
cd omnilive-recorder
docker compose up -d --build
docker compose logs -f
```

打开浏览器访问 **http://localhost:8000**（NAS 部署则为 `http://<NAS_IP>:8000`）。

> ⚠️ **代码更新后必须 `--build`**：仅 `docker compose up -d` 会沿用旧镜像，新代码不生效。

> ⚠️ **`data/` 是每台机器独立的**：部署机上添加的主播、写入的 Cookie 不会自动同步到另一台机器的 `./data`。迁移配置请用「主播管理 → 导出/导入」与「系统设置 → 导出/导入」。

### 2. 裸 Docker 运行

```bash
docker build -t omnilive-recorder .
docker run -d --name omnilive-recorder -p 8000:8000 \
  -v $(pwd)/recordings:/app/recordings \
  -v $(pwd)/data:/app/data \
  -v $(pwd)/config:/app/config \
  omnilive-recorder
```

### 3. QNAP NAS 部署（数据落 NAS 盘）

容器自身磁盘较小，推荐让录制文件直接写 NAS：

- **方式一 · named volume（docker-compose 默认已启用）**：`omni-recordings` 卷由 Docker 自动创建在 NAS 存储池（`CACHEDEV1_DATA`）的 Docker 卷目录，文件直写 NAS、零额外配置。设置页 `output_dir` 保持 `/app/recordings` 即可。
- **方式二 · bind mount 精确指定**：compose 里改为宿主机路径挂载，如 `- /share/CACHEDEV1_DATA/Container/Share/我的录制:/app/recordings`。

---

## 🧭 使用指南

### 第 1 步：配置 Cookie（决定各平台能用什么功能）

「系统设置 → 通知 / 代理 / Cookie」中按平台粘贴登录态 Cookie（获取方法：浏览器登录对应网站 → F12 开发者工具 → Network 任意请求 → 复制请求头 Cookie）。

| 平台 | 不填 Cookie | 填了 Cookie |
| --- | --- | --- |
| **抖音** | 直播可录但主播名为空（文件名退化为房间号）；**作品列表/下载游客态当前可用**，稳定性随平台策略浮动 | 直播返回主播名；作品接口最稳 |
| **B站** | **全功能可用**（自动获取游客 buvid3，直播可录原画、作品列表无需登录） | 画质/接口更稳，作品下载可达更高清晰度 |
| **快手** | **直播检测与作品订阅均不可用**（游客态被风控） | 直播检测/录制、作品订阅正常 |

快手直播间地址必须是 `https://live.kuaishou.com/u/<ID>` 形式（含 `/u/`）。

### 第 2 步：添加主播（统一主播模型）

「主播管理 → 添加主播」，**直播间地址与主页地址至少填一个**：

- 直播间地址：`https://live.douyin.com/123456789`、`https://live.bilibili.com/12345`、`https://live.kuaishou.com/u/xxx`
- 主页地址：`https://www.douyin.com/user/MS4wLjABAAAA...`、`https://space.bilibili.com/123456`、`https://www.kuaishou.com/profile/xxx`

| 字段 | 说明 |
| --- | --- |
| 直播间地址 | 填了才监控直播；清空则不监控 |
| 主页地址 | 作品订阅依据；清空则同时关闭作品订阅 |
| 主播名 | 建议手动填写——部分平台游客态拿不到昵称，缺失时文件/目录名会退化。优先级：手动填写 > 平台探测 > 备注 > 房间号 |
| 订阅作品 | 需要主页地址（或已检测过的直播间自动回填主播 ID）；开启后自动全量回填历史作品 |
| 备注 / 同步路径 / 画质 | 备注可作主播名回退；画质默认原画 |

保存后后台立即检测一次。只填了直播间地址的主播，检测到直播后会自动回填主播平台用户 ID，之后在编辑里也能开启作品订阅（无需主页地址）。

### 第 3 步：直播录制

- 开播后自动录制，主播行「录制状态」变录制中；也可在主播开播时点「录制」手动开始
- 断流自动重连续写同一场；下播自动停止并合并全场分片
- **当日自动合并**：同一主播当天的多段录制（多次开播/下播），下播结束后台自动合并为 `{主播名}_{日期}_合并.{格式}`，录制记录收敛为一条。上限默认 5GB（「系统设置 → 当日自动合并上限」，0 关闭）
- 录制文件落盘：`{output_dir}/{平台}/{主播名}/{日期}/{模板}.ts`，文件名模板支持 `{streamer} {room_id} {platform} {title} {remark} {date} {time} {datetime}` 占位符（设置页有实时预览）

### 第 4 步：作品订阅

- 主播行勾选「作品订阅」即开始后台回填历史作品（设置页可设回填条数上限，0 = 全部）
- 回填与下载**并行进行**：作品入库后 10 秒内进入下载队列，按平台串行 + 3~7 秒随机间隔下载（防风控）
- 作品量大时（回填全部 + 每页防风控延迟）整体耗时按小时计是正常现象，作品页可见状态逐步流转
- 「作品列表」页支持按主播/状态过滤，可播放、下载、重新下载、删除单个作品
- 某作品失败（Cookie 过期/风控/已删除）不影响队列其它作品；修复 Cookie 后点「重新下载」即可

### 第 5 步：NAS 定期同步（QNAP 实测步骤）

把已完成的直播录制与作品按主播归拢备份到 NAS：

**① File Station 建目标文件夹**：如 `Container/Share/直播录制备份`。

**② 给容器追加卷挂载**（按部署方式二选一）：

- Container Station「应用程序」（docker-compose）方式，编辑 YAML 在 `volumes:` 下加一行：
  ```yaml
  - /share/CACHEDEV1_DATA/Container/Share/直播录制备份:/mnt/qnap-recordings
  ```
  （路径前缀不确定可 SSH 执行 `ls -d /share/*/Container/Share` 确认；UI 路径浏览器选择亦可）
- 单容器方式：容器停止 → 重新创建 → 存储步骤添加卷（主机路径选 `Container/Share/直播录制备份`，容器路径 `/mnt/qnap-recordings`）

**③ 容器内验证**：终端执行
```sh
ls /mnt/qnap-recordings && touch /mnt/qnap-recordings/.t && rm /mnt/qnap-recordings/.t && echo 可写
```

**④ 设置页配置**：「NAS 定期同步」勾选启用，同步根目录填 **`/mnt/qnap-recordings`（容器内路径，不是 NAS 路径）**，保存后点「立即同步」。

**⑤ 验证**：File Station 查看 `直播录制备份/{主播名}/直播|作品/`。同步为增量幂等（已存在且大小一致跳过）、单向累积备份（源删除不影响已同步副本）、录制中的文件绝不同步（写完后下轮自动带上）。默认每 1 小时一轮。

---

## ⚙️ 设置项参考

「系统设置」页保存后立即生效并持久化（写入 `data/runtime_config.ini`），也可用环境变量前缀 `LIVE_RECORDER_` 覆盖。

| 设置项 | 说明 | 默认值 |
| --- | --- | --- |
| `record_format` | 录制格式：`ts` / `flv` / `mp4` | `ts` |
| `segment_time` | 分段时长（秒）。仅 TS/MP4 生效，FLV 单文件 | `1800` |
| `monitor_interval` | 直播检测间隔（秒） | `120` |
| `check_timeout` | 平台接口请求超时（秒） | `15` |
| `stream_url_refresh_interval` | 录制中流地址主动刷新间隔（秒），0 关闭。短时效签名 URL 过期前换新地址续写，消除「录几分钟就断」 | `90` |
| `daily_merge_max_gb` | 当日自动合并上限（GB），0 关闭 | `5` |
| `filename_template` | 输出文件名模板（占位符见设置页预览） | `{streamer}_{time}` |
| `max_disk_usage` | 磁盘用量上限提示（%） | `90` |
| `output_dir` | 录制输出根目录（填容器内已挂载目录） | `/app/recordings` |
| `works_poll_interval` | 作品新作品检测间隔（秒） | `600` |
| `works_check_count` | 每次拉取作品条数 | `20` |
| `works_auto_download` | 检测到新作品自动下载 | 开 |
| `works_backfill_limit` | 首次订阅回填条数上限，0 = 全部 | `0` |
| `sync_enabled` / `sync_root` / `sync_interval` | NAS 同步开关 / 同步根目录（容器内路径）/ 间隔（秒） | 关 / 空 / `3600` |
| `douyin_cookie` / `bilibili_cookie` / `kuaishou_cookie` | 各平台登录态 Cookie（作用见使用指南第 1 步） | 空 |
| `enable_notification` / `webhook_url` | 开播/结束/合并完成 Webhook 通知 | 关 |
| `enable_proxy` / `proxy_addr` | 代理访问平台接口（下载不走代理） | 关 |

---

## 📡 API 速览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/health` | 健康检查 |
| GET / POST | `/api/rooms` | 主播列表（含作品统计）/ 添加主播（双地址自动识别平台） |
| PUT / DELETE | `/api/rooms/{id}` | 编辑主播（双地址/开关/主播名等）/ 删除（级联清理记录，文件保留） |
| POST | `/api/rooms/{id}/check` | 手动检测直播状态 |
| POST | `/api/rooms/{id}/start-recording` / `stop-recording` | 手动开始 / 停止录制 |
| GET / POST | `/api/rooms/export` · `/api/rooms/import` | 主播配置导出 / 导入 |
| GET | `/api/recordings` · `/api/recordings/stats` | 录制记录 / 统计 |
| GET | `/api/works?room_id=&status=` | 作品列表（按主播/状态过滤） |
| POST | `/api/works/check/{room_id}` | 立即检查该主播新作品 |
| POST | `/api/works/{id}/retry` | 重新下载 |
| DELETE | `/api/works/{id}` | 删除作品（含文件） |
| GET | `/api/files` · `/api/files/download/{path}` · `/api/files/play/{path}` | 文件列表 / 下载 / 在线播放 |
| DELETE / POST | `/api/files/{path}` · `/api/files/batch-delete` · `/api/files/merge` | 删除 / 批量删除 / 合并 |
| GET / PUT | `/api/system/info` · `/api/system/settings` | 系统信息 / 更新设置 |
| GET / POST | `/api/system/settings/export` · `/import` | 设置导出 / 导入 |
| POST | `/api/system/sync/run` | 立即执行一次 NAS 同步 |
| GET | `/api/system/logs` · `/api/system/platforms` | 系统日志 / 支持平台 |

---

## 📁 目录结构

```
recordings/
├── {平台}/{主播名}/{日期}/        # 直播录制（当日合并后为 *_合并.ts）
└── works/{平台}/{主播名}/          # 订阅作品（视频 mp4 / 图集 zip）
同步根目录（若启用）/               # NAS 备份
└── {主播名}/直播|作品/...          # 按主播归拢的副本

omnilive-recorder/
├── app/
│   ├── main.py                    # FastAPI 入口（生命周期管理四个服务）
│   ├── config.py / database.py / models.py
│   ├── routers/                   # rooms(主播) / works / recordings / files / system
│   ├── services/
│   │   ├── monitor.py             # 直播监控 + 当日合并
│   │   ├── works_monitor.py       # 作品订阅（检查/下载并行双循环）
│   │   ├── sync_service.py        # NAS 同步
│   │   ├── platform_manager.py    # 平台适配器统一缓存
│   │   ├── recorder.py / file_manager.py
│   │   └── platform/              # douyin / bilibili / kuaishou 适配器 + vendor/abogus.py
│   └── static/                    # Web 前端
├── config/config.ini              # 基础配置（运行时配置在 data/runtime_config.ini）
├── Dockerfile / docker-compose.yml
└── README.md
```

---

## ❓ 常见问题

**Q: 作品一直「待下载」？**
旧版本存在下载饥饿（回填期间下载队列不运行），更新到最新代码重建即可。修复后回填与下载并行，作品入库 10 秒内开始下载。

**Q: 抖音下载批量失败 / 列表拉取报错？**
更新「系统设置 → 抖音 Cookie」后重试；失败的点「重新下载」。平台风控策略会变化，登录态 Cookie 是最稳的方式。

**Q: 快手什么都检测不到？**
快手必须配置登录态 Cookie（游客态被风控），且地址必须含 `/u/` 路径。

**Q: 直播文件名是房间号不是主播名？**
抖音游客态拿不到主播名。填抖音 Cookie，或在编辑主播时手动填「主播名」。

**Q: 容器重启后录制记录显示失败？**
这是启动自愈逻辑：上次异常退出（如容器强杀）遗留的「录制中」记录已无对应进程，被标记为失败；卡在「下载中」的作品也会自动回到下载队列。

**Q: 如何迁移到新机器？**
导出主播配置 + 导出系统设置，新机器导入；`recordings` 数据目录直接拷贝。

---

## 🔒 安全与部署建议

- **务必部署在内网**，或放在已做鉴权的反向代理之后。设置环境变量 `LIVE_RECORDER_API_TOKEN` 可启用 Bearer Token 鉴权（`/api/*` 全部保护，健康检查与静态页豁免）。
- 平台 Cookie 属于敏感凭据，请勿提交到公开仓库（本项目 `.gitignore` 已忽略本地配置）。
- 依赖通过 `requirements.lock` 锁定，CI 构建优先使用以保证复现性。

---

## ⚠️ 免责声明

本项目仅供个人学习与技术研究使用。请遵守各直播平台的服务条款与当地法律法规，录制与下载内容仅限个人留存，不得用于商业用途或侵犯他人权益。

---

## 📄 License

MIT
