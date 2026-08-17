# ETF / A 股分析工作台

一个本地运行的 ETF + A 股技术分析可视化工具,把三套方法论整合到一个页面:

1. **BIAS 三档逃顶**(微信公众号「张翼轸Earl」) — BIAS20>10%/15% 减仓,BIAS60>20% 再减仓
2. **年度最大回撤**(同作者姊妹篇) — 每年初清零,红利 10% / 中性 15% / 创业板 20% 三档参照
3. **Mark Minervini 趋势模板**(etfwin.com) — 4 条均线+价格规则筛选上升趋势标的

## 功能

- 📈 单只标的详情: 蜡烛图 + MA20/50/150/200 + 成交量
- 🚨 BIAS20 / BIAS60 副图,带 10%/15%/20% 三档警戒线
- 📉 年度最大回撤 + 距 52 周 / 今年高、低点的百分比距离
- 🎯 全市场 ETF Mark 模板筛选(均线多头 / MA200 上升 / 远离低点 / 接近新高)
- ⭐ 我的自选(ETF + A 股): 支持分组、逐只警戒订阅、WxPusher 推送
- 🧪 逐只测试推送 + 分组导出/导入
- ⚙️ 所有阈值可配置、可保存、可恢复默认
- 📤 筛选结果导出 CSV / JSON

## 最近更新

**v0.3.13**
- 参数配置新增**「全量拉取时间(交易日)」**设置：可配置交易日全量刷新全市场 ETF + 自选 K 线的时间，默认 `07:30` + `16:15`，与现有离线刷新逻辑一致。保存后后端调度器自动生效，无需重启。
- 参数配置**「保存」按钮**改为：保存成功后自动关闭参数配置抽屉，回到主界面。
- 参数配置抽屉默认**全部折叠显示**（所有分类面板均收起），并把原本的原始 JSON 预览也折叠进「原始配置(JSON)」面板，界面更清爽。

**v0.3.12**
- 自动下载离线数据改为**每天两次**：`07:30`（盘前补拉）+ `16:15`（收盘后主拉取），均覆盖全市场 ETF + 自选。
- 07:30 补拉增加「跳过」判断：若**最新已收盘交易日**的离线数据已被刷新（即前一日 16:15 已成功拉取），则直接跳过、不再重复下载同一份收盘数据，省资源；仅当前一日 16:15 进程未运行/失败导致缺失时才全量补拉。启动补刷逻辑同样采用该「最新已收盘交易日」判断，避免重复。
- 自动推送默认时间由 `10:00 / 13:30 / 16:00` 改为 `10:00 / 13:30 / 17:00`（16:15 离线刷新先于 17:00 推送，保证推送用当日收盘价）。

**v0.3.11**
- 扩展 15:30 收盘后自动下载离线 K 线的范围：从「仅自选」升级为**全市场 ETF + 自选（ETF + 股票）**。自动刷新与手动「刷新所有离线数据」现在统一收集 `data/etf_list.csv` 全部 ETF（约 1576 只）与自选所有分组代码（含 A 股，如 688825），逐一 `force_refresh` 重新下载并覆盖本地缓存。
- 手动「刷新所有离线数据」改为**后台线程执行并立即返回**（全市场约 1500+ 只、耗时 10~20 分钟，不再让请求长时间挂起）；新增 `GET /api/offline-refresh/status` 实时进度接口，前端按钮点击后每 3 秒轮询并显示「刷新中 N% (done/total)」，完成后弹窗汇总成功/失败数与耗时。
- 刷新循环加节流（每只间隔 0.1s）并增加线程安全的「运行中」互斥，避免调度触发与手动点击并发重复刷新；进度写入 `offline_refresh_progress` 内存状态。

**v0.3.10**
- 修复「我的自选」中某组显示数量与实际不符：后端 `/api/watchlist/screen` 现在对**历史 K 线不足 60 日或为空**的标的（如新股/次新股）也会保留显示，指标置空并提示「数据不足」，不再被过滤导致「共 N 只」与「已缓存」不一致。
- 修复 15:30 自动下载与手动「刷新所有离线数据」实际未正确刷新问题：根因是 `wl.list_groups()` 返回的 `codes` 已改为 `{code, name}` 对象，但收集逻辑仍按字符串处理，导致实际代码变成 `{'code': '...'}` 而全部刷新失败。
- 新增启动补刷机制：若当天交易日 15:30 已过且尚未刷新，服务启动后立即执行一次 `_offline_refresh_run()`，避免 15:30 时进程未运行而错过。

**v0.3.9**
- 「当天」视图从 1 分钟 K 线改为更常见的**分时折线图**：主图用蓝色实线绘制**现价走势**，叠加橙色虚线**均价线(VWAP)**，并增加灰色虚线**昨收参考线**；下方保留成交量柱状图，涨跌色与昨收对比，更接近典型行情软件的分时图样式。
- 后端 `/api/chart/intraday/<code>` 新增返回 `close`、`vwap`、`prev_close`、`total_amount`；`core/intraday_price.py` 统一分钟数据字段与数值类型，新增 `fetch_realtime_quotes()` 获取昨收/今开/最新价。
- 顶部标题与 meta 信息同步改为「当天分时图」，并显示昨收与当日涨跌幅。

**v0.3.8**
- 修复「今年 / 52 周 / 全部」详情报错：`/api/chart/<code>` 偶发 500 的根因为**端口 5001 上残留了多个旧的 server 进程**(重复绑定、请求命中旧实例)。已清理为单实例运行;同时在后端给该接口加了 try/except,任何异常都返回 JSON 而非裸 500。
- 修复「当天」视图下「当前信号」栏空白：当天模式新增 `renderLiveSignals()`,用分时**实时最新价**计算实时 BIAS20(实时价 vs 日 K 的 MA20),并保留日级的 BIAS60 与年内回撤;切回日级范围时自动用日 K 摘要重渲染信号(清除实时值)。

**v0.3.7**
- 新增「当天」1 分钟 K 线图：在「今年/52 周/全部」切换按钮旁增加**「当天」**选项，点击后按需实时拉取该代码当天 1 分钟分时 K 线，无需预热缓存。
- 后端 `core/intraday_price.py` 新增 `fetch_today_kline()`：ETF 走 `fund_etf_hist_min_em`、A 股走 `stock_zh_a_hist_min_em`，异常时回退新浪分钟线；前端 `static/js/chart.js` 新增 `drawIntradayChart()` 绘制分时蜡烛图 + 成交量。

**v0.3.6**
- 左侧「自选列表」迷你分组显示优化：每行**左侧显示代码，右侧显示名称**；名称缺失时自动从 ETF 全量列表或已缓存的自选信息中补齐，避免右侧重复显示代码。

**v0.3.5**
- 左侧「ETF/股票列表」面板顺序调整：**自选列表放在前面**，ETF 全量列表放在后面，方便优先查看持仓/关注标的。
- 盘中实时价警戒查询策略优化：由固定 60 秒间隔改为**提前 3 分钟窗口内随机生成最多 3 个查询时刻、随机间隔**，既避免被行情接口识别为规律请求导致限流，又保持「查到即停」的轻量特性。
- 新增 API 层 404/500 错误处理：所有 `/api/*` 异常统一返回 JSON，避免前端因解析 HTML 错误页（`<!doctype ...`）而失败。

**v0.3.4**
- 顶部 tab「ETF 列表」改名为「ETF/股票列表」；左侧列表区拆分为「A. ETF 列表」（全量 ETF）和「B. 自选列表」（按我的自选分组展示，点击代码可快速查看图表）。
- 新增盘中实时价警戒：交易时段内，在配置推送时间点前 3 分钟启动预热，查询新浪实时行情（`hq.sinajs.cn`），获取到有效价格即停止；到达推送时间后用实时价评估警戒。盘后/非交易时段自动回退到日 K 收盘价。
- 前端 `static/js/app.js` 按功能拆分为 `utils.js`（通用工具）、`chart.js`（ECharts 渲染）、`app.js`（业务主控）；后端新增 `core/intraday_price.py` 负责实时行情。

**v0.3.3**
- 新增「交易日 15:30 收盘后自动下载所有离线 K 线」：进程内守护线程调度器，每到 15:30 强制重新下载所有自选（ETF + A 股）的日 K 缓存，保证次日 10:00 / 同日 16:00 的警戒用的是最新收盘价。
- `core/data_source.py` 的 `fetch_kline` / `fetch_stock_kline` 增加 `force_refresh` 参数，绕过 24h 缓存新鲜度判断强制重新下载（下载失败仍回退旧缓存）。
- 新增手动接口 `POST /api/watchlist/refresh-all` 与前端「⬇ 刷新所有离线数据」按钮，可随时手动补齐数据。

**v0.3.2**
- 修复 Docker 中 A 股全市场股票名称列表预热可能卡在 0 的问题：增加 90s/60s 超时、详细日志、阶段状态展示与手动重新下载按钮。

> **A 股股票**: 按代码前缀自动识别(0/2/3/4/6/8/9 开头 = 股票,1/5 开头 = ETF)。
> **只下载「我的自选」里股票的历史数据**,不批量下载全市场;添加到自选的瞬间即同步下载历史 K 线。

## 快速开始

```bash
# 1. 创建隔离 venv 并安装依赖(不污染系统 Python)
cd /path/to/etf-analysis
python -m venv venv
venv/Scripts/python.exe -m pip install --no-cache-dir -r requirements.txt   # Windows
# venv/bin/python -m pip install --no-cache-dir -r requirements.txt         # macOS / Linux

# 2. 启动服务
venv/Scripts/python.exe app.py     # Windows(或直接双击 start.bat)
# venv/bin/python app.py           # macOS / Linux

# 3. 浏览器打开
#    http://127.0.0.1:5001
```

> Windows 用户也可直接双击 `start.bat`,它会自动依次探测 `venv/` → `.venv/` →
> 环境变量 `ETF_PYTHON` → 系统 `python`。

## 全市场筛选(可选预热)

首次打开时,K 线按需拉取。若要全市场秒级筛选,先后台预热缓存:

```bash
python warmup.py               # 全市场(约 1576 只,耗时约 10-20 分钟)
python warmup.py --limit 100   # 只预热前 100 只
python warmup.py --resume      # 续传(跳过已缓存)
```

预热后,`运行筛选` 走本地缓存,秒级返回。

## Docker 部署(群晖/威联通/绿联等 NAS)

```bash
cd F:\workbuddy\ETF
docker compose up -d
# 访问 http://<NAS-IP>:5001
```

- `docker-compose.yml` 默认直接用 GHCR 镜像 `ghcr.io/qwgaan/etf-analysis:latest`,无需本地 build。
- `data/`、`config/`、`outputs/` 通过 volume 持久化,K 线缓存和自定义配置不会因容器重建丢失。
- 容器需能访问外网(拉取 AKShare 数据)。
- 预热全市场:`docker compose exec etf-analysis python warmup.py`
- 更新到最新版:`docker compose pull && docker compose up -d`

## 发布新版本(打语义版本号)

GHCR 上的 `latest` 镜像默认随 `main` 分支每次 push 更新,但没有 1.0.0 / 1.0.1 这种语义版本号。要发布带版本号的镜像:

```bash
# 1. 在 main 分支最新 commit 上打一个 semver tag
git tag v1.0.0
git push origin v1.0.0

# 或在 GitHub 网页 → Code → Releases → "Create a new release"
#   · Tag: v1.0.0(选择 existing 或新建)
#   · Target: main 分支最新 commit
#   · 写 release notes
#   · Publish
```

Actions 触发后,镜像同时打 `1.0.0`、`1.0`、`latest` 三个标签。用户侧:
```bash
docker pull ghcr.io/qwgaan/etf-analysis:1.0.0    # 固定版本
docker pull ghcr.io/qwgaan/etf-analysis:1.0      # 主版本
docker pull ghcr.io/qwgaan/etf-analysis:latest   # 跟随 main 最新
```

**升级小版本时不要跳过 `v0.x` → `v1.x`**(semver 约定)。补丁流程 `v1.0.0` → `v1.0.1` → `v1.1.0` → `v2.0.0` 即可,workflow 的 `type=semver` 会自动识别。

## GitHub + GHCR 自动化部署(推荐长期方案)

### 一次性配置
1. 在 GitHub 新建空仓库 `etf-analysis`(设为 Public,不要勾选 README)
2. 把本项目推上去(沙箱 git push 会被拦截,需用 Contents API,token 走环境变量):
   ```bash
   # Windows PowerShell(token 只放环境变量,不要写进任何文件):
   $env:GH_TOKEN="<你的 GitHub PAT>"
   python scripts/push_repo.py 你的用户名 etf-analysis .
   ```
3. 推完后,在 GitHub 仓库 **Settings → Packages**,把生成的 GHCR 镜像(默认 Private)改成 Public,其他人才能拉。

### 自动构建镜像
- 项目里已有 `.github/workflows/docker-image.yml`,token = `GITHUB_TOKEN`(自动注入,无需手动配置)
- 每次 push 到 main 分支都会自动触发,推 `ghcr.io/你的用户名/etf-analysis:latest`
- 手动触发:`Actions → build-and-push-ghcr → Run workflow`

### NAS 上拉镜像
```bash
docker pull ghcr.io/你的用户名/etf-analysis:latest
docker run -d \
  -p 5001:5001 -p 5001:5001 \
  -v /你的本地路径/data:/app/data \
  -v /你的本地路径/config:/app/config \
  -v /你的本地路径/outputs:/app/outputs \
  --name etf-analysis \
  ghcr.io/你的用户名/etf-analysis:latest
```

访问 `http://<NAS-IP>:5001`。

> **为什么用 GHCR 而不是 Docker Hub?** GHCR 是 GitHub 内置的镜像仓库,只需要 GitHub 账号,不需要单独注册 Docker Hub,Webhook/PAT 授权也最简单。

## 目录结构

```
ETF/
├── app.py                  # Flask 后端(路由 + 指标聚合)
├── warmup.py               # 全市场 K 线预热脚本
├── core/
│   ├── data_source.py      # 数据源:AKShare 列表 + 历史K线(ETF/股票,带磁盘缓存)
│   ├── indicators.py       # 技术指标:MA / BIAS / 年度回撤 / 区间高低点
│   ├── filters.py          # Mark 模板 4 条规则判定
│   ├── watchlist.py        # 我的自选(多组,ETF + 股票,警戒订阅/阈值)
│   ├── alert.py            # WxPusher 警戒推送(BIAS 三档 + 年度回撤)
│   └── config.py           # 配置管理(defaults.json + user.json)
├── templates/index.html    # 单页前端
├── static/                 # CSS + JS(ECharts)
├── config/defaults.json    # 默认配置(可被 user.json 覆盖)
├── data/klines/            # K 线缓存(按 code 分文件)
└── outputs/                # 预留:报告输出目录
```

## 数据源说明

- **历史 K 线**: [AKShare](https://akshare.akfamily.xyz/)(开源免费)。BIAS、回撤、均线、52 周高/低全部依赖历史 K 线。
  - ETF: 优先**新浪 `fund_etf_hist_sina`**(稳定、数据更久),东财 `fund_etf_hist_em` 兜底。
  - A 股: 优先**新浪 `stock_zh_a_daily`**(前复权),东财 `stock_zh_a_hist` 兜底(含北交所)。
- **股票名称/搜索**: 新浪 `stock_zh_a_spot`(全市场 A 股快照,仅名称/代码截面,本地缓存到 `data/stock_list.csv`,不下载历史)。
- **问财技能**(同花顺问财 ETF 选股): 可额外接入做 ETF 快照/规模/风格筛选,但问财只返回截面数据、无历史序列,故本工具以 AKShare 为主。

> 东财源在部分网络/代理环境下会被限流(ConnectionError),故历史 K 线与股票名称均以新浪源为主。

## 指标口径

| 指标 | 公式 | 用途 |
|------|------|------|
| BIAS(N) | (close − MA(N)) / MA(N) × 100% | 乖离率,N=20/60 |
| 年度最大回撤 | 当年内 (close − 年初以来最高) / 年初以来最高 × 100% | 每年清零,避免历史极值干扰 |
| 距 52 周高/低 | (close − 52周高/低) / 52周高/低 × 100% | 趋势模板规则 3/4 |
| MA200 斜率 | MA200 最近 N 日最小二乘斜率 | 规则 2「持续上升」 |

## 免责声明

本工具仅用于策略研究与技术分析学习,不构成任何投资建议。市场有风险,交易需谨慎,据此操作风险自负。
