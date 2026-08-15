# ETF 分析工作台

一个本地运行的 ETF 技术分析可视化工具,把三套方法论整合到一个页面:

1. **BIAS 三档逃顶**(微信公众号「张翼轸Earl」) — BIAS20>10%/15% 减仓,BIAS60>20% 再减仓
2. **年度最大回撤**(同作者姊妹篇) — 每年初清零,红利 10% / 中性 15% / 创业板 20% 三档参照
3. **Mark Minervini 趋势模板**(etfwin.com) — 4 条均线+价格规则筛选上升趋势标的

## 功能

- 📈 单只 ETF 详情: 蜡烛图 + MA20/50/150/200 + 成交量
- 🚨 BIAS20 / BIAS60 副图,带 10%/15%/20% 三档警戒线
- 📉 年度最大回撤 + 距 52 周 / 今年高、低点的百分比距离
- 🎯 全市场 Mark 模板筛选(均线多头 / MA200 上升 / 远离低点 / 接近新高)
- ⚙️ 所有阈值可配置、可保存、可恢复默认
- 📤 筛选结果导出 CSV / JSON

## 快速开始

```bash
# 1. 安装依赖(用隔离 venv,不污染系统)
C:\Users\admin\.workbuddy\binaries\python\envs\default\Scripts\python.exe -m pip install --no-cache-dir akshare flask pandas numpy

# 2. 启动服务
cd F:\workbuddy\ETF
C:\Users\admin\.workbuddy\binaries\python\envs\default\Scripts\python.exe app.py

# 3. 浏览器打开
#    http://127.0.0.1:5001
```

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
   # Windows PowerShell:
   $env:GH_TOKEN="ghp_xxx..."
   python scripts/push_repo.py 你的用户名 etf-analysis F:\workbuddy\ETF
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
│   ├── data_source.py      # 数据源:AKShare 列表 + 历史K线(带磁盘缓存)
│   ├── indicators.py       # 技术指标:MA / BIAS / 年度回撤 / 区间高低点
│   ├── filters.py          # Mark 模板 4 条规则判定
│   └── config.py           # 配置管理(defaults.json + user.json)
├── templates/index.html    # 单页前端
├── static/                 # CSS + JS(ECharts)
├── config/defaults.json    # 默认配置(可被 user.json 覆盖)
├── data/klines/            # K 线缓存(按 code 分文件)
└── outputs/                # 预留:报告输出目录
```

## 数据源说明

- **列表 + 历史K线**: [AKShare](https://akshare.akfamily.xyz/)(开源免费)。BIAS、回撤、均线、52 周高/低全部依赖历史 K 线。历史 K 线优先用**新浪源 `fund_etf_hist_sina`**(稳定、数据更久),东财源兜底(带复权但易被限流)。
- **问财技能**(同花顺问财 ETF 选股): 可额外接入做 ETF 快照/规模/风格筛选,但问财只返回截面数据、无历史序列,故本工具以 AKShare 为主。

## 指标口径

| 指标 | 公式 | 用途 |
|------|------|------|
| BIAS(N) | (close − MA(N)) / MA(N) × 100% | 乖离率,N=20/60 |
| 年度最大回撤 | 当年内 (close − 年初以来最高) / 年初以来最高 × 100% | 每年清零,避免历史极值干扰 |
| 距 52 周高/低 | (close − 52周高/低) / 52周高/低 × 100% | 趋势模板规则 3/4 |
| MA200 斜率 | MA200 最近 N 日最小二乘斜率 | 规则 2「持续上升」 |

## 免责声明

本工具仅用于策略研究与技术分析学习,不构成任何投资建议。市场有风险,交易需谨慎,据此操作风险自负。
