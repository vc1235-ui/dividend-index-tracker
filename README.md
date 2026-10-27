# 红利指数个股跟踪

跟踪 **上证红利指数（000015）** 与 **中证红利指数（000922）** 的全部成分股，按股息率、估值历史分位和分红质量筛选值得投资的个股，以网页图表展示；当个股触发**买入**或**卖出**信号时自动推送提醒。

- 网页：部署在 GitHub Pages（<https://vc1235-ui.github.io/dividend-index-tracker/>）或 Cloudflare Pages（<https://dividend-index-tracker.pages.dev/>），见下文“部署”
- 由 GitHub Actions 自动更新，无需服务器：每个交易日北京时间 **16:07** 和次日凌晨 **02:17** 各运行一次（第二次兜底 16:07 时尚未发布的数据），每次都是增量更新，约 1 分钟

> ⚠️ 规则信号仅供学习研究，不构成投资建议。

## 网页内容

| 模块 | 说明 |
| --- | --- |
| 概览 | 跟踪个股数、买入/卖出信号数、通过质量筛选数、股息率中位数 |
| 股息率 × 估值分位散点图 | 横轴股息率，纵轴股息率历史分位，气泡大小为市值，颜色为信号（红=买入，绿=卖出） |
| 成分股列表 | 买入 / 卖出信号后显示已持续的交易日数（开始跟踪前的部分由回测补齐，见下文）；显示所属行业（东方财富行业分类）；可按指数、行业、信号（含买入候选）、质量筛选、关键字过滤，任意列排序 |
| 个股详情 | **网格法买卖价位**（图 + 价位表）、股价 + 年线 + 股息率走势（标注除息日）、股息率与买卖线、PE 历史分位带、年度每股分红与支付率 |
| 信号提醒记录 | 历次信号变化 |

## 指标与规则

- **股息率(TTM)** = 过去 365 天已除息的每股税前现金分红 ÷ 不复权收盘价
- **股息率分位 / PE 分位 / PB 分位**：当前值在近 5 年日度数据中的百分位
- **连续分红年数**：截至最近一个已公布年报分配方案的财年，连续现金分红的年数
- **支付率** = 财年每股分红合计 ÷ 年报每股收益
- **综合评分（0-100）** = 股息率 30 + 分红稳定性 20 + 支付率合理性 15 + 估值分位 20 + 分红增长 15

| 信号 | 条件（默认值） |
| --- | --- |
| 质量筛选 | 连续分红 ≥ 3 年，PE > 0，支付率 ≤ 90%，评分 ≥ 60 |
| 🔴 买入 | 通过质量筛选 且 股息率 ≥ 5% 且 股息率分位 ≥ 70% 且 PE 分位 ≤ 50%；**每天最多 5 只**，按综合评分取前 5 名，其余标为“买入候选”（不提醒） |
| 🟢 卖出（任一） | 股息率 ≤ 3%；股息率分位 ≤ 15%；PE 为负；年度分红较上年削减 > 30%；被调出指数 |

**信号持续天数的回测**：首次运行时，对最近约 1 年（245 个交易日）的每一天，只用当天可得的数据（已除息的分红、此前 5 年的股息率 / PE 分位、已披露的年度分红方案——年报按报告期后约 4 个月、中报 / 季报约 2 个月可得）重算信号，并按天执行每日最多推荐只数的限制，据此补齐当前信号的起始日期；之后每天在此基础上累加。一年内一直未变的显示为“≥245天”。回测未考虑历史上的指数成分调整。

**网格法买卖价位（底仓 + 网格）**：以“股息率回到近 5 年中位数时的价格”为中枢，按 5% 等比划分价格网格，列出现价下方最近 4 档买入价、上方最近 4 档卖出价，并标注每档对应的股息率和所在估值区。跌到买入档分批买入；涨到卖出档时，低估区只卖出网格仓位、保留底仓，高估区可逐步减仓。价位表标注每档的**仓位百分比**（占计划投入总额）：现价买入底仓 40%，其余 60% 按 1:2:3:4 金字塔分给 4 个买入档（6% / 12% / 18% / 24%，买完即满仓）；低估区每个卖出档卖出一份网格仓位（15%）、保留底仓，中枢及高估区每档减仓 25%；卖出信号时暂停买入、在各卖出档平均分批清仓。在价位表上方输入**计划投入总额**，即按 1 手 = 100 股算出每档买卖股数和金额（金额只保存在你自己的浏览器里）。间距、档数和仓位比例在 `config.yaml` 的 `grid` 中设置。

所有阈值都在 [`config.yaml`](config.yaml) 中，修改并推送后即生效。

**只在出现新的买入 / 卖出信号时提醒**（例如由“持有/观望”变为“买入”），信号持续期间不再提醒；同一股票同一信号 20 天内不重复提醒；同一数据日期只在第一次处理时提醒，两次定时运行、非交易日都不会重复。

**成分股自动更新**：每次运行都从中证指数官网重新获取上证红利、中证红利的成分股，与上次名单比较；有调入调出时自动更新跟踪名单、发送提醒并记录在网页“成分股调整记录”中，被两个指数都调出的股票会收到卖出提醒。

## 部署（一次性设置）

网页可以部署到 **GitHub Pages** 或 **Cloudflare Pages**，二选一即可。

**方式 A：GitHub Pages**

仓库 Settings → Pages → Build and deployment → Source 选择 **GitHub Actions**。网址为 `https://<用户名>.github.io/dividend-index-tracker/`。

**方式 B：Cloudflare Pages**

1. 在 Cloudflare 控制台 → 右上角头像 → My Profile → API Tokens → Create Token → Create Custom Token，权限选 **Account → Cloudflare Pages → Edit**，创建后复制令牌。
2. 在 Cloudflare 控制台首页（Workers & Pages 页右侧）复制 **Account ID**。
3. 在本仓库 Settings → Secrets and variables → Actions → **Repository secrets**（不是 Environment secrets）添加 `CLOUDFLARE_API_TOKEN` 和 `CLOUDFLARE_ACCOUNT_ID`。Account ID 不是机密，也可以放在 Variables 里。

下次运行时会自动创建 Pages 项目并部署，网址为 `https://dividend-index-tracker.pages.dev/`。配置 Cloudflare 后默认不再部署 GitHub Pages。可选的仓库变量（Settings → Secrets and variables → Actions → Variables）：

| 变量 | 作用 |
| --- | --- |
| `CLOUDFLARE_PROJECT_NAME` | Pages 项目名（默认 `dividend-index-tracker`；若该名称已被占用，在 Cloudflare 中看到的实际网址可能不同） |
| `SITE_URL` | 提醒消息中“查看图表”的链接，绑定自定义域名后填写 |
| `ALSO_GITHUB_PAGES` | 设为 `true` 时同时部署到 GitHub Pages |

> 国内访问：`*.github.io` 和 `*.pages.dev` 在大陆都可能不稳定。在 Cloudflare Pages 项目的 Custom domains 中绑定自己的域名，通常访问更稳定。

**首次运行**：Actions → “更新数据并部署” → Run workflow。约 10 分钟后网页即可访问。

**配置提醒**（可选，可多选）：Settings → Secrets and variables → Actions → New repository secret。

| 渠道 | Secret |
| --- | --- |
| GitHub 站内通知（默认开启，无需配置） | — 新信号或成分股调整时创建 Issue 并 @仓库所有者，出现在 GitHub 通知（铃铛）中，并按你的 GitHub 设置发邮件 / App 推送 |
| Server酱（微信） | `SERVERCHAN_SENDKEY` |
| PushPlus（微信） | `PUSHPLUS_TOKEN` |
| 企业微信群机器人 | `WECOM_WEBHOOK` |
| 钉钉群机器人 | `DINGTALK_WEBHOOK`，加签时再加 `DINGTALK_SECRET` |
| 飞书群机器人 | `FEISHU_WEBHOOK` |
| Telegram | `TELEGRAM_BOT_TOKEN`、`TELEGRAM_CHAT_ID` |
| Bark（iOS） | `BARK_URL`，如 `https://api.day.app/你的key` |
| 邮件 | `SMTP_HOST`、`SMTP_PORT`（默认 465）、`SMTP_USER`、`SMTP_PASSWORD`、`MAIL_TO`（逗号分隔） |

不想要 Issue 提醒可在 `config.yaml` 中设置 `notify.github_issue: false`。

## 本地运行

```bash
pip install -r requirements.txt
python -m tracker.main --no-notify     # 真实数据（需能访问中证指数、东方财富）
python -m tracker.main --demo          # 离线演示数据
cd web && python -m http.server 8000   # 打开 http://localhost:8000
python -m pytest -q                    # 运行测试
```

## 目录结构

```
config.yaml            规则与阈值
tracker/fetch.py       数据获取（AKShare：中证指数官网成分股、东方财富估值与分红）
tracker/cache.py       估值历史缓存：每次运行批量拉增量，每次轮流全量刷新少量股票（每只约 7 天一次）
tracker/backtest.py    回测历史信号，补齐信号持续天数
tracker/indicators.py  股息率、分位、连续分红、支付率、分红增速
tracker/signals.py     综合评分与买卖规则
tracker/notify.py      各推送渠道
tracker/main.py        每日流程：计算 → 生成网页数据 → 对比状态 → 提醒
web/                   静态网页（ECharts），data/ 由 Actions 生成后直接部署
state/                 信号状态与提醒记录（Actions 自动提交）
```

## 局限

- 数据来自公开网页接口，接口变动可能导致个别股票获取失败（网页会列出失败的股票）；超过 30% 失败时本次不更新、不提醒。
- 股息率按每股分红原值计算，发生送转股的个股在送转前后可能短暂失真。
- 成分股以中证指数官网最新权重文件为准；官网获取失败时沿用上次名单。
