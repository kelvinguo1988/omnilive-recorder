# OmniLive Recorder 代码优化清单

> 扫描时间：2026-08-23 07:20
> 扫描范围：`app/` 全量 + Docker/compose/CI + 前端静态资源
> 评估维度：正确性 / 稳定性 / 资源泄漏 / 性能 / 安全 / 可维护性

---

## 一、优先级矩阵（总览）

| 级别 | 数量 | 含义 |
|---|---|---|
| 🔴 P0 | 3 | 直接导致功能失效或数据错乱，建议立即修复 |
| 🟠 P1 | 5 | 影响稳定性/性能/安全，建议尽快修复 |
| 🟡 P2 | 7 | 代码异味/弃用 API/未使用配置，可择机清理 |

---

## 二、🔴 P0 严重问题

### P0-1 · 多 part 合并后文件落到错误目录，破坏「平台/主播/日期」结构

- **位置**：`app/services/monitor.py:414-425` + `app/services/file_manager.py:168-233`
- **现象**：当一场录制发生断流重连产生多个 part 后，下播时 `_finalize_session` 调用 `file_manager.merge_recordings()`，而该函数把合并结果**强制写到 `{output_dir}/merged/merged_YYYYMMDD_HHMMSS.{fmt}`**，然后 monitor 用 `final_path = merged["output_path"]` 覆盖了原计划的 `平台/主播/日期/base.扩展名` 路径。
- **影响**：
  1. 合并文件脱离「平台/主播/日期」目录结构，文件管理页按平台/主播筛选找不到这些文件（被识别为 `platform="merged"`）。
  2. DB 中 `file_name` 变成 `merged_20260823_072010.ts`，丢失主播名/时间信息，无法追溯。
  3. 原 part 所在的「平台/主播/日期」目录删除 part 后变空壳，`_clean_empty_dirs` 只清理 `merged/` 的父目录，留下空目录垃圾。
- **修复建议**：给 `merge_recordings()` 增加 `output_path` 参数（或 `output_dir + output_name`），由 `_finalize_session` 显式传入 `recording.file_path`（原 final_path）作为合并目标，保持目录结构与命名一致。伪代码：
  ```python
  # file_manager.merge_recordings 签名扩展
  def merge_recordings(self, file_paths, output_format="mp4", output_path=None):
      ...
      out_path = output_path or os.path.join(merged_dir, f"merged_{ts}.{fmt}")
      ...
  ```
  ```python
  # monitor._finalize_session 调用处
  merged = file_manager.merge_recordings(
      [os.path.relpath(f, settings.output_dir) for f in files],
      output_format=recording.format,
      output_path=final_path,  # 用原计划路径，保持结构
  )
  ```

---

### P0-2 · 平台适配器实例重建时旧 `httpx.AsyncClient` 未关闭，连接泄漏

- **位置**：`app/services/monitor.py:42-57`、`app/routers/system.py:237-238`、`app/routers/rooms.py:228-229`
- **现象**：当用户在系统设置页修改 Cookie 或代理后，`_get_platform()` 会新建一个平台实例并覆盖缓存；`system._apply_settings` 和 `rooms.update_room` 还会直接 `monitor._platform_instances.clear()`。**两种路径都没有调用旧实例的 `close()`**，每个旧实例都持有一个 `httpx.AsyncClient`（含连接池），被直接丢弃。
- **影响**：每次保存设置都泄漏一个 client（≥3 个 TCP 连接 + 协程）。长期运行下连接数累积，最终可能触发 fd 上限或对平台接口产生异常请求模式。
- **修复建议**：把 `_get_platform` 中替换实例的逻辑改为先 close 旧的；`_platform_instances.clear()` 改为遍历 close 后再 clear。建议在 monitor 上加一个 `_reset_platform_cache()` 方法统一处理：
  ```python
  async def _reset_platform_cache(self):
      for inst in self._platform_instances.values():
          try: await inst.close()
          except Exception: pass
      self._platform_instances.clear()
  ```
  三个调用点统一改成 `await monitor._reset_platform_cache()`。

---

### P0-3 · `monitor.stop()` 不停止 ffmpeg，容器重启时录制文件损坏

- **位置**：`app/services/monitor.py:69-91`
- **现象**：`stop()` 只 cancel 调度任务 + close 平台 client，**完全没调用 `recorder.stop_recording()`**。容器收到 SIGTERM 后 uvicorn 退出，ffmpeg 子进程被内核 SIGKILL（因为 `preexec_fn=os.setsid` 创建了进程组，但父进程退出后该进程组不会自动终止，而是被 init 收养；实际 Docker 停容器时 cgroup 销毁会强杀所有进程）。
- **影响**：
  - TS 容错好，影响小；
  - **MP4 录制文件因 `moov` atom 末尾未刷写而完全不可播放**（mp4 必须正常关闭才写 moov）；
  - 录制中的 DB 记录停留在 `status=recording`，重启后不会自动 finalize，需手动干预。
- **修复建议**：`stop()` 末尾加：
  ```python
  # 优雅停止所有 ffmpeg，让 mp4 正常写 moov
  for room_id in list(recorder.active_processes.keys()):
      await recorder.stop_recording(room_id)
  ```
  并考虑在 lifespan 启动时扫描 DB 中 `status=recording` 但实际无进程的记录，标记为 `failed` 或自动 finalize。

---

## 三、🟠 P1 重要问题

### P1-1 · 主动刷新循环与断流重连双路竞态，可能造成重复重启 ffmpeg

- **位置**：`app/services/monitor.py:126-166` + `237-248`
- **现象**：`_refresh_stream_urls`（90s 周期）和 `_check_room`（120s 周期）都能对同一房间触发 `_reconnect_session`。虽然 `_reconnect_lock` 串行化了两路，但**串行执行的第二次不会重新判断是否已处理**，导致两次 stop+start ffmpeg，中间空一段视频。
- **修复建议**：`_reconnect_session_inner` 开头加幂等检查：
  ```python
  # 若流地址已与最新跟踪一致，说明刚被另一路处理过，跳过
  prev = self._room_states.get(room.id, {}).get("stream_url", "")
  if info.stream_url == prev and await recorder.is_recording(room.id):
      logger.debug(f"房间 {room.id} 已被其他路径重连，跳过")
      return
  ```

---

### P1-2 · 文件播放接口"伪 Range 支持"，视频拖动失败

- **位置**：`app/routers/files.py:45-74`
- **现象**：响应头声明 `Accept-Ranges: bytes` + `Content-Length`，但 `iter_file` 是顺序读取，不处理 `Range` 请求。浏览器 `<video>` 拖动进度条会发 Range 请求，服务器仍返回 200 全量，导致拖动无效（从头播）或卡顿。
- **修复建议**：直接换用 `FileResponse`，Starlette 自动支持 Range：
  ```python
  @router.get("/play/{file_path:path}")
  async def play_file(file_path: str):
      full_path = file_manager.get_file_path(file_path)
      ext = os.path.splitext(full_path)[1].lower()
      media_types = {".ts": "video/mp2t", ".flv": "video/x-flv",
                     ".mp4": "video/mp4", ".mkv": "video/x-matroska"}
      return FileResponse(full_path, media_type=media_types.get(ext, "application/octet-stream"),
                          filename=os.path.basename(full_path))
  ```

---

### P1-3 · CORS 配置矛盾 + 无任何 API 鉴权

- **位置**：`app/main.py:57-64`
- **现象**：`allow_origins=["*"]` + `allow_credentials=True` 违反浏览器规范（带 credentials 时不能用 *）。虽然 FastAPI 中间件会回显 Origin 让它实际能 work，但这是反模式。同时所有 API 公开无鉴权——房间增删、Cookie 修改、文件删除等敏感操作任何人都能调。
- **修复建议**：
  - 关闭 `allow_credentials` 或改用白名单 origin；
  - 加一个基于环境变量的简单 token 鉴权（`LIVE_RECORDER_API_TOKEN`），中间件校验 `Authorization: Bearer <token>`，静态首页和 `/api/health` 放行；
  - README 明确警告「务必部署在内网或加反向代理鉴权」。

---

### P1-4 · `SystemLog` 表无清理机制，长期运行无限增长

- **位置**：`app/models.py:54-62` + 全局调用点
- **现象**：每次开播/下播/录制开始/结束都写日志。长期运行（NAS 7×24）后 `system_logs` 表行数无上限。
- **修复建议**：在 `monitor._monitor_loop` 每轮末尾或单独定时任务中清理 30 天前的日志：
  ```python
  async with async_session() as s:
      await s.execute(delete(SystemLog).where(SystemLog.created_at < datetime.utcnow() - timedelta(days=30)))
      await s.commit()
  ```
  或加 `max_log_rows` 配置项，按行数截断。

---

### P1-5 · 文件列表/磁盘统计全量 `os.walk`，大目录性能差

- **位置**：`app/services/file_manager.py:27-66`、`105-129`、`131-165`
- **现象**：`get_file_list`、`get_disk_usage`、`get_streamers` 每次都全量遍历 `output_dir`。NAS 上录制文件动辄几 TB、上万个文件，前端切到「文件管理」页或仪表盘刷新（每 15s）都会触发一次全量 walk，I/O 抖动明显。
- **修复建议**：
  - 加一个 5–10s TTL 的内存缓存（key 为目录 mtime）；
  - 或限制 `get_file_list` 默认只列最近 N 条（按 mtime 倒序），前端按需分页；
  - `get_disk_usage` 的 `recording_size` 计算可以单独缓存，因为它每次都全量 stat。

---

## 四、🟡 P2 代码异味 / 弃用 API / 死配置

### P2-1 · 配置项 `max_retries` / `retry_delay` / `video_quality` 完全未使用

- **位置**：`app/config.py:13-16`、`app/services/platform/bilibili.py:18-25`
- **现象**：
  - `max_retries`/`retry_delay`：grep 全项目，仅在 config.py 定义和持久化，**业务代码零调用**。用户在 Web 设置页改了无效果。
  - `video_quality`：bilibili 适配器定义了 `QUALITY_MAP` 但 `_get_stream_url` 中 `quality = 10000` 写死，QUALITY_MAP 是死代码；抖音/快手根本没用 quality。
- **修复建议**：要么删除这三个配置项 + Web 设置页对应字段（保持「配置即生效」契约），要么在适配器中实际接入（B 站用 QUALITY_MAP，抖音/快手按平台 quality 字段映射）。

---

### P2-2 · `.gitignore` 错把 `.dockerignore` 列入忽略

- **位置**：`.gitignore:29`
- **现象**：
  ```
  # Docker
  .dockerignore
  ```
  `.dockerignore` 是构建配置，**应该入库**。当前因文件已被跟踪不影响，但意图错误，且新 maintainer 会困惑。
- **修复建议**：删除 `.gitignore` 第 29 行的 `.dockerignore` 条目。

---

### P2-3 · `datetime.utcnow()` 已被 Python 3.12+ 弃用

- **位置**：`monitor.py:201,288,430`、`rooms.py:122`、`system.py:122,278`、`models.py:28,46,62`
- **现象**：Python 3.12 起 `datetime.utcnow()` 发出 `DeprecationWarning`，未来版本会移除。Dockerfile 用 `python:3.11-slim` 暂无警告，但升级即爆。
- **修复建议**：统一改为 `datetime.now(timezone.utc).replace(tzinfo=None)`（保持 DB 列无时区），或全部改用 `datetime.now(UTC)` 并把 DB 列改为 `DateTime(timezone=True)`。

---

### P2-4 · pydantic v1 风格的 `class Config` 在 v2 中已弃用

- **位置**：`app/config.py:57-58`
- **现象**：
  ```python
  class Config:
      env_prefix = "LIVE_RECORDER_"
  ```
  pydantic-settings v2 推荐 `model_config = SettingsConfigDict(env_prefix="LIVE_RECORDER_")`。当前能 work 但会有 deprecation 警告。
- **修复建议**：改用 `SettingsConfigDict`。

---

### P2-5 · 抖音适配器 `RENDER_DATA` 解析路径为死代码

- **位置**：`app/services/platform/douyin.py:60-105`
- **现象**：根据 8/16 修复日志，抖音已弃用 `RENDER_DATA` 注水，现走 `LIVE_SSR_DATA_ID`。但代码里仍在 `re.search(r'<script id="RENDER_DATA"...')`，命中后走一套复杂的解析逻辑——这条路径实际上永远走不到（或走到也是空数据）。
- **修复建议**：删除 RENDER_DATA 分支，统一走 `_get_stream_from_api`，简化代码 ~40 行。保留的话也行（作为兜底），但应加注释说明「抖音已弃用，保留作为历史版本兜底」。

---

### P2-6 · `_check_room` 中 `room` ORM 对象的字段不会随 update 语句回写

- **位置**：`app/services/monitor.py:190-248`
- **现象**：
  ```python
  await session.execute(update(Room).where(Room.id == room.id).values(**update_data))
  await session.commit()
  # 后面仍读 room.is_recording（取的是循环开始时的快照值）
  if info.is_live and not room.is_recording: ...
  ```
  SQL 层 update 不会同步到 `room` 对象。正常情况下间隔短无影响，但并发刷新循环改 `_room_states` 时可能读到陈旧的 `room.is_recording`。
- **修复建议**：update 后 `await session.refresh(room)`，或直接用 `_room_states` 判断录制态。

---

### P2-7 · `requirements.txt` 未锁 transitive deps，重建镜像版本浮动

- **位置**：`requirements.txt`
- **现象**：只锁了 11 个直接依赖的精确版本，但 `httpx`、`sqlalchemy` 等带的子依赖（`httpcore`、`h11`、`anyio` 等）未锁。不同时间构建镜像可能拉到不同子依赖版本，复现性差。
- **修复建议**：用 `pip-compile` 生成 `requirements.lock`，CI 构建用 lock 文件。

---

## 五、其他观察（不计入优先级，仅供参考）

1. **前端 innerHTML 大量字符串拼接**（`app.js` 共 14 处）：所有数据来自后端且后端 sanitize 过文件名，XSS 风险低，但建议改用 `textContent` 或简单 escape 函数，防御纵深。
2. **`recorder.cleanup_finished` 用 `process.stderr.read()`**（recorder.py:328）：长录制 stderr 量大时占内存。已用 2s 超时 + `[-500:]` 截取，影响可控。
3. **Dockerfile 用 `python:3.11-slim`**（小版本浮动）：建议固定到 `python:3.11.9-slim-bookworm` 提高复现性。
4. **`docker-compose.yml` 资源限制 `cpus: 2.0, memory: 1G`**：默认 `-c copy` 不转码，够用；若未来加转码需调大。
5. **`httpx.AsyncClient` 全局复用**：`BasePlatform.__init__` 创建后常驻，未设连接池上限。建议 `httpx.Limits(max_connections=20, max_keepalive_connections=10)`。
6. **抖音 `_get_x_bogus` 是简化版签名**：注释明确写「简化版」，长期可能被风控。如失效需接入完整 JS 算法或调用第三方签名服务。
7. **`bilibili._get_stream_url` 优先返回 FLV**：HLS 兼容性更好（拖动、CDN 缓存），可考虑优先返回 HLS。当前优先 FLV 是为了避免 m3u8 分片加载延迟，合理。

---

## 六、建议修复顺序

| 阶段 | 修复项 | 预估工作量 |
|---|---|---|
| **第一波（必做）** | P0-1 合并路径、P0-2 client 泄漏、P0-3 stop 不停 ffmpeg | 1–2 小时 |
| **第二波（推荐）** | P1-1 竞态、P1-2 Range、P1-4 日志清理 | 1 小时 |
| **第三波（择机）** | P1-3 鉴权、P1-5 文件列表缓存 | 2–3 小时 |
| **第四波（清理）** | P2 全部 | 1 小时 |

---

## 七、修复状态（2026-08-23 已全量修复）

> 全部 15 项 + 2 项「其他观察」已在本轮提交中修复，并通过 `py_compile` 与 `app.main` 导入验证。

| 编号 | 修复内容 | 关键改动 |
|---|---|---|
| P0-1 | 合并落到错误目录 | `file_manager.merge_recordings` 新增 `output_path` 参数；`_finalize_session` 传入 `final_path` 保持「平台/主播/日期」结构 |
| P0-2 | httpx client 泄漏 | 新增 `_reset_platform_cache()`（先 close 再 clear）；`_get_platform` 重建前 close 旧实例；system/rooms 两处调用点改为 `await monitor._reset_platform_cache()`（同时把 `_get_platform` 改为 async） |
| P0-3 | stop 不停 ffmpeg | `stop()` 优雅停止所有 ffmpeg（mp4 正常写 moov）；启动时 `_recover_stale_recordings()` 把遗留 `recording` 标记为 `failed` |
| P1-1 | 重连竞态 | `_reconnect_session_inner` 入口加幂等检查（流地址未变且进程存活则跳过） |
| P1-2 | 播放伪 Range | `files.py` 改用 `FileResponse`，由 Starlette 自动支持 Range |
| P1-3 | CORS + 无鉴权 | `allow_credentials=False`；新增 `AuthMiddleware`，设置 `LIVE_RECORDER_API_TOKEN` 后对所有 `/api/*` 强制 Bearer 鉴权；README 补充安全部署说明 |
| P1-4 | 日志无限增长 | `_monitor_loop` 每天清理 30 天前 `system_logs`（新增 `_cleanup_old_logs`） |
| P1-5 | 文件列表全量 walk | `FileManager` 加 10s TTL 内存缓存（`get_file_list`/`get_disk_usage`），删除时失效缓存 |
| P2-1 | 死配置 | 删除 `video_quality`/`max_retries`/`retry_delay`（config/API/前端同步移除） |
| P2-2 | gitignore | 移除 `.dockerignore` 忽略条目（现纳入版本管理） |
| P2-3 | utcnow 弃用 | 全量替换为 `datetime.now()` / `datetime.now(timezone.utc).replace(tzinfo=None)` |
| P2-4 | pydantic v1 Config | 改为 `model_config = SettingsConfigDict(env_prefix=...)` |
| P2-5 | 抖音 RENDER_DATA 死代码 | 删除已失效的 `RENDER_DATA` 解析分支，统一走 webcast API |
| P2-6 | is_recording 陈旧 | `_check_room` 写库后重新读取 `is_recording` 再判断录制态 |
| P2-7 | 依赖未锁定 | `pip-compile` 生成 `requirements.lock`；Dockerfile 优先用 lock 安装 |
| 观察 #3 | Dockerfile 浮动 | 固定 `python:3.11.9-slim-bookworm` |
| 观察 #5 | httpx 连接池无上限 | `BasePlatform` 的 `AsyncClient` 加 `Limits(max_connections=20, max_keepalive_connections=10)` |

---

*报告生成于 2026-08-23 07:20 · 全量修复于 2026-08-23 11:xx · 基于 commit `de0d798`/`5e4278d` 之后的当前工作区状态*
