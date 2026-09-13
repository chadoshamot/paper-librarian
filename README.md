# AI 论文管家（Paper Librarian）

本地 AI 论文管家：录入 PDF → 双语知识库 → 语义检索/跳转 → 云端同步（ModelScope）→ 每日检索 → 联网对话。一个可复现、密钥自带的个人论文管理 agent。

## 功能

- **录入**：PDF → 元数据抓取 → 自动分类（DeepSeek）→ 重命名 → Zotero 链接 → 双语 Markdown 卡片
- **检索**：语义向量 + 关键词 + 标题加权（fastembed + jina-v2-base-zh），精准 / 探索（MMR）两种模式
- **论文库**：大领域 → 方向 → work 三级目录树；跳原文（arXiv / DOI / 云端 / Zotero / 本地）
- **每日检索**：多源抓取 + 质量评分 + LLM 裁判，输出日报，可推库 / 发邮箱
- **联网对话**：Kimi 官方 web-search，全网检索并带引用回答
- **上云**：ModelScope 增量同步（PDF 走 git-lfs，KB 走 Markdown）
- **界面**：交互式 CLI + 零依赖 Web 界面

## 快速开始

### 1. 环境要求

- Python **3.10+**（推荐 3.11 / 3.12；3.14 部分依赖 wheel 可能滞后）
- Git（含 git-lfs，用于 PDF 云端同步）
- 系统 Python 或 venv 均可

### 2. 克隆并安装

```bash
git clone https://github.com/<你的用户名>/paper-librarian.git
cd paper-librarian
python setup.py
```

`setup.py` 会交互式地：检查 Python → 建目录 → 导入/初始化论文库 → 生成配置与 `.env` → 装依赖 → 冒烟验证。**可选项直接回车跳过**，对应功能会优雅降级。

### 3. 跑起来

```bash
python -m service          # 交互式 CLI（主菜单：检索 / 论文库 / 每日简报 / 上传 / 云端同步 / 联网对话）
python -m service.web      # 网页界面，浏览器打开 http://127.0.0.1:8000
```

## 密钥获取指引

密钥全部填在 `.env`（已被 `.gitignore` 排除，不会上传）。可 `python setup.py` 交互式生成，或复制 `.env.example` 手动填。

| 密钥 | 变量名 | 必填 | 获取方式 |
|---|---|---|---|
| DeepSeek | `DEEPSEEK_API_KEY` | 是（分类/摘要/每日裁判） | [platform.deepseek.com](https://platform.deepseek.com) → API Keys |
| ModelScope | `MODELSCOPE_TOKEN` | 自建库模式需要 | [modelscope.cn](https://modelscope.cn) → 个人中心 → 访问令牌 |
| Kimi | `MOONSHOT_API_KEY` | 否（联网对话） | [platform.moonshot.cn](https://platform.moonshot.cn) → API Key |
| Zotero | `ZOTERO_USER_ID` / `ZOTERO_API_KEY` | 否（Zotero 链接） | [zotero.org/settings/keys](https://www.zotero.org/settings/keys) → 新建 key |
| 邮箱 | `SMTP_HOST/PORT/USER/PASS/TO` | 否（邮件推送） | QQ 邮箱 → 设置 → 账户 → 开启 SMTP → 生成授权码 |

> 可选密钥不填时，对应功能返回友好错误、其余照常。DeepSeek 不填则录入/分类/每日裁判不可用，但**检索 / 浏览仍可用**。

## 数据模式

- **示例库（默认，只读）**：`setup.py` 会 `git clone` 公开的示例论文库（ModelScope `shamot` 命名空间，约 18 篇），克隆即能检索/浏览，零 token。
- **自建库（完整写功能）**：填你自己的 ModelScope `namespace` + token，用 `python -m service.ingest papers/xxx.pdf` 建自己的库，`python -m service.cloud sync` 上云。

## 常用命令

```bash
python -m service                                          # 交互式 CLI 主菜单
python -m service.ingest papers/xxx.pdf                    # 录入一篇 PDF（完整管线）
python -m service.reclassify papers/xxx.pdf --category 方法 --area "机器学习系统::调度" --work slo-aware-scheduling --year 2024
python -m service.search "GPU 集群调度" [--mode search|explore] [--engine auto|keyword|embedding] [--top 10]
python -m service.jump <pid> [--kind local|arxiv|doi|cloud|zotero]
python -m service.cloud sync                               # 增量同步 PDF 与 KB 到 ModelScope
python -m service.daily [--top 10] [--sources openalex,arxiv,semantic_scholar] [--no-llm]
python -m service.web [--host 127.0.0.1 --port 8000]       # Web 界面
```

## 每日检索与定时

`python -m service.daily` 从 arxiv / openalex / semantic_scholar 抓候选，跨源去重、剔除本库已有，按质量评分排序，输出 `reports/daily/YYYY-MM-DD.md`。兴趣点、阈值、权重在 `knowledge-base/config.yml` 的 `daily` / `quality` 段。

**Windows（任务计划程序）**：

```powershell
schtasks /Create /TN "PaperLibrarianDaily" /TR "D:\你的路径\paper-librarian\run_daily.bat" /SC DAILY /ST 08:00 /F
```

**Linux / macOS（cron）**：

```cron
0 8 * * * /path/to/paper-librarian/run_daily.sh
```

## 常见问题

- **首次检索较慢**：会下载嵌入模型（jina-v2-base-zh，约数百 MB，走 `hf-mirror.com` 镜像），之后走缓存。
- **PDF 上云需要 git-lfs**：若 `git clone` 下来的 PDF 是文本指针而非真 PDF，请安装 Git LFS 后重新同步。
- **用 conda 时**：请用目标环境运行 `python setup.py` 与 `python -m service`，避免依赖装错环境。
- **换 LLM**：改 `.env` 的 key 或 `config.yml` 的 `model` 段（provider / base_url / model 三处）；换联网对话模型改 `chat` 段。

## 项目结构

```
service/            # 服务代码（Python 模块）
setup.py            # 一键安装（交互式生成 .env / 配置 / 数据）
config.example.yml  # 配置模板（setup.py 把 __NAMESPACE__ 替换后写入 knowledge-base/）
taxonomy.yml        # 分类学（setup.py 复制到 knowledge-base/）
.env.example        # 密钥模板
requirements.txt    # 依赖清单
papers/             # 新 PDF 收件箱（inbox，gitignored）
cache/              # PDF 热缓存（ModelScope 云仓库本地副本，gitignored）
knowledge-base/     # 双语知识库（gitignored，由 setup.py 生成/导入）
index/              # 检索向量索引（自动生成，gitignored）
models/             # 嵌入模型缓存（自动下载，gitignored）
reports/daily/      # 每日检索日报（gitignored）
```
