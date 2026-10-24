# SubFlow —— PT 视频字幕下载 Web UI

把原先散落在脚本里的「qBittorrent 种子 → 站点详情页 → 字幕包下载 → 落盘到视频目录」
这条链路，封装成一个可长期运行、可观测、可多站点扩展的 Web 应用。

- **一条链路**：扫描下载器种子库 → 反查站点详情页 → 下载字幕 → 解压到视频同目录
- **三种入口**：全库扫描（种子级）+ 站点字幕区（站点级）+ **按分类批量下载**
- **七个视图**：任务台 · 字幕区 · 缺字幕 · 统计 · 历史记录 · 日志 · 设置
- **自动化**：可选的 HTTP Basic 访问控制 + 进程内定时扫描
- **可观测**：缺字幕清单（以本地文件为准）+ 统计面板（每日量 / 站点 / 分类 / 失败原因）
- **多站点**：适配器模式，新增站点只写一个类并注册
- **多下载器**：可同时接多个 qB / Transmission 实例，各自独立路径映射与分类视图

---

## 1. 快速开始（Docker Compose）

```bash
# 1) 放置源码到内网主机的部署目录
mkdir -p /opt/subflow
tar -xzf subflow-src.tar.gz -C /opt/subflow
mkdir -p /opt/subflow/data

# 2) 生成配置（qB / CookieCloud 填 127.0.0.1 或内网 IP）
cp /opt/subflow/config.json.example \
   /opt/subflow/data/config.json
vi /opt/subflow/data/config.json

# 2.1) 指定下载根目录：宿主机与容器内必须同路径
cd /opt/subflow
echo "DOWNLOAD_DIR=/path/to/downloads" > .env

# 3) 构建并启动
docker compose up -d --build

# 4) 验证
curl -s http://127.0.0.1:8099/api/health | python3 -m json.tool
```

浏览器打开 `http://<内网IP>:8099`。

### 直接使用预构建镜像

```bash
docker pull ghcr.io/gtx2090ti/subflow:latest
cd /opt/subflow
echo "SUBFLOW_IMAGE=ghcr.io/gtx2090ti/subflow:latest" >> .env
docker compose up -d --no-build
```

镜像由 GitHub Actions 在每次推送到 `main` 时构建并推送到 GHCR（见 `.github/workflows/docker.yml`），
公开可匿名拉取。除 `latest` 外，打 `v*` 标签会生成一组版本标签（`v1.1.0` → `1.1.0` / `1.1` / `v1.1.0`），
标签版本必须与 `app/config.py` 的 `APP_VERSION` 一致，否则 CI 会直接失败。国内直连 GHCR 较慢时，
可改用镜像加速站的前缀形式拉取。

---

## 2. 目录与持久化

```
/opt/subflow/
├── app/                    # 应用代码（构建上下文）
├── data/                   # ← 唯一需要备份的目录
│   ├── config.json         #   配置（含凭据，权限建议 600）
│   ├── subflow.db          #   SQLite：jobs 任务/历史、logs 日志、settings
│   ├── subflow.db-wal      #   WAL 模式伴随文件
│   └── subflow.db-shm
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```

**数据持久化方案：SQLite 单文件（WAL 模式）**，不做外部数据库依赖。

| 表 | 用途 | 关键字段 |
|---|---|---|
| `jobs` | 任务与历史共用一张状态机表 | site_id / torrent_id / subid / status / save_path / file_name / created_at / finished_at |
| `logs` | 运行日志，按来源分类 | level / source / job_id / ts |
| `settings` | 键值杂项（扫描窗口、上次同步时间等） | key / value |

- 状态机：`pending → running → success / failed / exists / skipped`
- 进程重启时自动把卡在 `running` 的任务复位为 `pending`，避免永久假运行
- 历史与任务同源：状态为终态的记录即历史，支持分页、筛选、CSV 导出

---

## 3. 配置说明（`data/config.json`）

优先级：**内置默认值 < config.json < 环境变量 `SUBFLOW_<SECTION>_<KEY>`**

| 段 | 键 | 说明 |
|---|---|---|
| `server` | `host` / `port` | 监听地址，容器内固定 `0.0.0.0:8099` |
| `qbittorrent` | `url` | qB WebUI 地址，同机填 `http://127.0.0.1:8080`，跨机填内网 IP |
| | `api_key` | qB ≥ 5.2 的 WebUI API Key；留空则从 `conf_path` 读取 |
| | `conf_path` | 挂载进来的 `qBittorrent.conf` 路径，用来自动取 API Key |
| | `save_map` | **qB 容器内路径 → 宿主机真实路径**，逗号分隔多组。映射错会写不进视频目录 |
| | `max_torrents` | 单次拉取种子数上限（按添加时间倒序）。**不要调成 10 万级**，见排障 |
| `cookiecloud` | `url` / `uuid` / `password` | CookieCloud 实例与凭据；解密结果带 `cache_ttl` 缓存 |
| `download` | `workers` | 并发下载线程数 |
| | `req_interval` | 站点请求间隔（秒），对站点友好 |
| | `scan_days` | 只处理最近 N 天添加的种子 |
| | `extract_zip` / `extract_archive` | 是否自动解压 zip / rar·7z 字幕包 |
| | `keep_archive` | 解压后是否保留原压缩包 |
| | `manual_save_dir` | 手动提交任务（无本地种子可对应）时的兜底目录 |
| | `legacy_state_file` | 旧版 `qbit_sub_sync_state.json`，首次启动导入历史 |
| `sites` | `enabled` / `overrides` | 站点总开关；`overrides` 可按 site_id 覆盖 enabled / base_url / label |
| `log` | `level` / `retain_days` | 日志级别与保留天数 |
| `auth` | `enabled` / `username` / `password` | HTTP Basic 访问控制。**密码为空时认证不生效**；默认关闭 |
| | `realm` / `allow_localhost` | 浏览器提示域名；本机访问是否免认证（便于容器健康检查），默认 `true` |
| `schedule` | `enabled` / `interval_hours` | 定时扫描开关与间隔（小时，最小 0.25）。首次启用只记基准，不会立刻扫 |
| | `days` / `limit` / `refresh_cookie` | 定时扫描的时间窗口、单次最多检查数（0=不限）、是否强制刷新 Cookie |
| `missing` | `video_exts` | 视为视频的扩展名清单 |
| | `max_depth` / `max_files` | 缺字幕检查的目录深度与单目录视频数上限，避免超大合集拖慢整轮 |

> 敏感字段（`qbittorrent.api_key`、`cookiecloud.password`、`auth.password`）在接口返回时统一脱敏，
> 设置页只回显掩码，留空表示「不修改」。

---

## 4. 界面说明

| 视图 | 交互方式 | 展示形式 |
|---|---|---|
| **任务台** | 选站点 / 选下载器分类 → 一键扫描、按分类批量下载 / 手动提交（粘贴种子 ID、详情页或字幕页 URL）→ 查看进度；任务表格可按站点 / 分类 / 状态 / 关键词筛选 | 概览卡片 + 分页任务表格（上下双分页：首/末页、页码窗口、每页 20/50/100/200），状态徽章着色，失败可单条重试或批量重试 |
| **字幕区** | 选站点 → 拉取该站字幕区列表 → 勾选条目 → 指定保存目录 → 提交下载 | 分页表格：种子ID / 字幕ID / 语言 / 大小 / 发布时间 / 本地是否命中；支持关键词过滤 |
| **缺字幕** | 选时间窗口/下载器分类 → 检查本地视频目录 → 勾选可补条目 → 一键补字幕 | 列出视频旁没有字幕的种子：缺失数、缺失文件名、能否反查站点；无法反查的只能手工提交 |
| **统计** | 选 7 / 30 / 90 / 365 天窗口 | 成功/失败/总量/平均耗时卡片、每日堆叠柱状图、按站点/分类/来源排行、失败原因 Top 8 |
| **历史记录** | 按站点、状态、关键词、时间范围筛选 | 分页表格 + CSV 导出；含来源站点、视频名称、来源分类、字幕标题、时间、状态、落盘文件 |
| **日志** | 自动轮询增量拉取（游标式），可按级别/来源过滤 | 终端风格列表，支持一键下载全量日志 |
| **设置** | 表单编辑，分组折叠 | 回显当前配置（密钥掩码），保存后即时生效，无需重启；含访问控制与定时扫描 |

前端为单页应用，深色科技蓝主题，图标全部内联 SVG，无外部 CDN 依赖。

---

## 5. 接口清单

```
GET    /api/overview            概览统计（容器健康检查也用它）
GET    /api/health              qB / CookieCloud 连通性
GET    /api/sites               站点清单、Cookie 命中状态、能力位
GET    /api/categories          下载器分类清单（含每个分类的种子数量，成功后落快照）
GET    /api/categories/cached   上次分类快照（不访问下载器，页面启动回显）
POST   /api/scan                触发扫描（可带 categories 筛选；days<=0 表示不限时间窗口）
GET    /api/scan/state          扫描进度
GET    /api/schedule            定时扫描状态（上次/下次运行、上次结果）
GET    /api/stats               统计聚合（?days=7|30|90|365）
GET    /api/missing             缺字幕清单（?days=&downloader=&category=&keyword=&only_site_matched=）
POST   /api/missing/repair      按「站点+种子」批量补字幕（复用提交链路）
POST   /api/submit              手动提交任务（种子 ID / URL）
POST   /api/zone/list           拉取站点字幕区列表
POST   /api/zone/submit         提交字幕区条目下载
GET    /api/jobs                任务列表（站点/状态/关键词/分类/下载器过滤；limit/offset 分页，返回 total）
GET    /api/jobs/categories     任务里出现过的分类（含未分类与条数），供筛选下拉
GET    /api/jobs/{id}           任务详情
POST   /api/jobs/{id}/retry     重试单条
POST   /api/jobs/retry-failed   批量重试失败
POST   /api/jobs/run-pending    执行待处理队列
DELETE /api/jobs/{id}           删除记录
GET    /api/history             历史分页（站点/状态/关键词/时间范围）
GET    /api/history/export.csv  历史导出 CSV
GET    /api/logs                日志增量拉取（after_id 游标）
GET    /api/logs/download       全量日志下载
DELETE /api/logs                清理日志（可保留最近 N 条）
GET    /api/config              读取配置（密钥掩码）
POST   /api/config              更新配置
POST   /api/cookiecloud/refresh 强制刷新 CookieCloud 缓存
```

---

## 6. 新增站点

在 `app/sites/` 下继承 `SiteAdapter`（NexusPHP 系直接继承 `NexusPHPAdapter`），
然后在 `app/sites/__init__.py` 注册即可。适配器全部是**纯函数**，不发网络请求：

```python
class MySiteAdapter(NexusPHPAdapter):
    id = "mysite"
    label = "MySite"
    base_url = "https://mysite.example"
    domain = "mysite.example"        # CookieCloud 中的域名，引擎据此取登录态
    site_aliases = ("mysite.cc",)    # 别名域，comment 反查时一并认领
    supports_scan = True             # 是否参与全库扫描
    supports_subtitle_zone = True    # 是否支持站点级字幕区
```

需要覆写的方法（都有默认实现，通常只需改正则）：

| 方法 | 作用 |
|---|---|
| `parse_torrent_id(comment)` | 从 qB 种子的 comment 反查本站种子 ID |
| `detail_url(tid)` / `subtitle_url(tid, subid)` | 详情页 / 字幕包地址 |
| `parse_subtitles(html, tid)` | 详情页里有哪些字幕包 |
| `parse_subtitle_zone(html)` | 站点字幕区的条目列表 |
| `parse_name(html)` | 资源标题（手动提交时补全名称） |
| `looks_logged_out(html)` | 登录态失效判定 |

---

## 7. 按下载器分类批量下载字幕

种子越积越多以后，「最近 30 天」这个窗口就不够用了：想给整个「电影」目录补字幕，
却得按时间一批批扫。分类功能就是解决这个的——**分类是扫描的筛选条件，不是第二条链路**，
所以它复用的是同一套「种子 → 详情页 → 字幕包 → 落盘」流程。

### 怎么用

1. 任务台「扫描种子库」卡片里点 **读取分类**；
2. 分类 chips 上会显示名称与**该分类下的种子数量**（数量多的排前面）；
3. 勾选要处理的分类（可多选）；
4. 点 **按分类批量下载** —— 对所选分类做**全量**处理，**忽略时间窗口**。

每次读取成功都会把结果落成快照（SQLite `kv`），页面启动时自动回显上次结果，
所以下载器暂时离线也还能按旧分类筛选；离线节点会标注「显示上次读取结果」，
点 **读取分类** 即可刷新并覆盖快照。

「开始扫描」也会带上已勾选的分类，但**仍受时间窗口限制**，两者区别只有这一点：

| 按钮 | 分类筛选 | 时间窗口 |
|---|---|---|
| 开始扫描 | 生效 | 生效（用输入框里的天数） |
| 按分类批量下载 | 生效 | **忽略**（后端传 days=0） |

拉取详情页是按 `download.req_interval` 串行的，分类里种子多时整轮会明显变慢
（进度条与日志都有实时反馈，先用「演练模式」跑一遍可以只看命中量、不落盘）。

### 分类的抽象：qB 分类 vs TR 标签

| | qBittorrent | Transmission |
|---|---|---|
| 概念 | 分类（category），**单值** | 标签（label），**多值** |
| 定义来源 | `torrents/categories`（含保存路径） | 无此接口 |
| 归属判定 | `torrents/info` 的 `category` | `torrent-get` 的 `labels` |

引擎把两者统一成「一个种子归属若干分类」：`list_categories()` 取定义，
`torrent_categories(torrent)` 取归属。TR 走第二条路——拿不到分类定义时，
引擎自动退化为「从种子列表统计实际出现过的分类」，界面照常可用，只是没有保存路径。

> 注意：TR 一个种子可以有多个标签，因此各分类数量之和可能大于种子总数；
> qB 是单值分类，不存在这个问题。

### 接口层

```
GET  /api/categories                      读取分类 + 各分类种子数量（成功后写快照）
GET  /api/categories/cached               上次快照，不访问下载器（页面启动回显）
POST /api/scan  { "categories": ["dl1:电影", "剧集", ""], "days": 0 }
```

分类项的格式是 `<下载器ID>:<分类名>`；不带前缀则对所有下载器生效。
分类名传 `""` / `__none__` 表示「未分类」这一桶。

---

## 8. 排障（踩过的坑）

| 现象 | 根因 | 处理 |
|---|---|---|
| 扫描直接超时 | `torrents/info?limit=100000` 让 qB 服务端明显变慢直至读超时 | 改为按 `added_on` 倒序 + `max_torrents` 限量拉取 |
| 字幕文件名叫 `xxx.rar.rar` | 站点文件名后缀是语言代码（`.chs`），判定逻辑简单追加后缀 | 归一化：后缀与内容一致→保留；已知类型但不符→以内容为准；不认识→追加 |
| 裸字幕存成 `.bin` | 只按魔数判类型，未识别文本字幕 | 增加文本嗅探：BOM + `[Script Info]`→`.ass`；时间轴→`.srt` |
| ASS 字幕被当二进制 | 同上 | 同上 |
| `.7z` 包识别不出来 | 魔术字节是 6 字节，代码写成 `data[:7]` 恒不匹配 | 改为 6 字节比较 |
| RAR 包解不开 | 运行环境无解压后端 | 镜像装 `libarchive-tools`(bsdtar，RAR5 支持最好) + `p7zip-full`；Windows 本地用自带 `tar.exe`(bsdtar) |
| 字幕写不进视频目录 | `save_map` 与实际挂载不一致 | 必须把 qB 下载根**以同路径**挂进容器，`save_map` 写 `qB内路径=宿主机路径` |
| qB 返回 401 | qB ≥ 5.2 用 `Authorization: Bearer`，老版本用 `X-Api-Key` | 统一用 Bearer |
| 站点页面 200 但解析不到字幕 | 登录态失效（页面变成登录页） | 适配器 `looks_logged_out()` 判定 + CookieCloud 刷新 |
| 升级后启动即报 `no such column: downloader_id` / `category` | 老库缺列需要先 `ALTER TABLE`，而索引建在表结构脚本里，建索引时列还不存在 | 把依赖新列的索引从 `SCHEMA` 挪到 `_ensure_columns()`，在 `ALTER` 之后建 |

---

## 9. 自检与验证

```bash
# 离线自检（纯解析函数回归，无需联网）
python3 selftest.py

# 容器内自检
docker exec subflow python3 /app/selftest.py

# 接口连通性
curl -s http://127.0.0.1:8099/api/health | python3 -m json.tool
curl -s http://127.0.0.1:8099/api/sites  | python3 -m json.tool | head -40

# 启用了访问控制时，curl 需要带 Basic 凭据（本机默认免认证，可按需关闭）
curl -s -u admin:你的密码 http://127.0.0.1:8099/api/overview
```

---

## 10. 运行环境

- Python 3.10+（镜像基于 `python:3.11-slim`）
- 依赖：fastapi / uvicorn / requests / pycryptodome
- 解压后端：bsdtar（主力，RAR/RAR5）、7z（兜底）、unrar（备选）
- 时区 `Asia/Shanghai`
- 健康检查：`GET /api/overview`，周期 60s
