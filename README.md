# AI 论文管家（Paper Librarian）

本地 AI 论文管家：录入 PDF → 双语知识库 → 语义检索/跳转 → 云端同步（ModelScope）→ 每日检索 → 统一馆长 agent（查库/深读/联网搜索/下载/上云/删除）。一个可复现、密钥自带的个人论文管理 agent。内置 **PaperBook 桌面软件**，双击即开、无需终端。

## 桌面版 PaperBook（双击即开）

不用再每次进终端起服务。桌面版把整套 Web 界面装进一个原生窗口，双击 `paperbook.pyw`（或 `PaperBook.bat`）即可打开，**无终端黑窗**；关闭窗口即自动停服。

- 首次使用先装桌面依赖：`python -m pip install pywebview pythonnet`（`requirements.txt` 已包含）。
- Windows 走系统自带 **WebView2**（Win11 预装）渲染，界面与网页版一致；若未装，回退为默认浏览器打开。
- 新增 **「设置」控制台**：在窗口顶部导航切到「设置」，可 UI 调整**全部参数配置与 API Key**（`config.yml` 的模型/检索/每日检索/质量权重/云端仓库，以及 `.env` 里的 DeepSeek/Kimi/ModelScope/Zotero/邮箱密钥），保存后立即生效；密钥只落本地 `.env`，不上云。
- 命令行亦可启动：`python -m service.desktop`。
- 想要**桌面/开始菜单快捷方式**：右键 `install-shortcuts.ps1` → 「使用 PowerShell 运行」，自动创建指向 `pythonw.exe` 的快捷方式（路径无关，项目搬到别处后重跑本脚本即可重新生成）。

## 功能

- **录入**：PDF → 元数据抓取 → **AI 阅读全文生成摘要（DeepSeek）** → 按摘要结果自动分类 / 归档 / 重命名 → Zotero 链接 → 双语 Markdown 卡片；网页上传支持一次多选多个 PDF，逐个排队入库
- **检索**：语义向量 + 关键词 + 标题加权（fastembed + jina-v2-base-zh），精准 / 探索（MMR）两种模式
- **论文库**：大领域 → 方向 → work 三级目录树；跳原文（arXiv / DOI / 云端 / Zotero / 本地）；就地改元数据 / 重抓元数据 / 重分类 / 删除
- **每日检索**：多源抓取 + 质量评分 + LLM 裁判，输出日报，可推库 / 发邮箱
- **馆长 agent**（统一入口，DeepSeek 总控）：查库问答、**深读论文全文**、维护记忆与笔记、带 pid 引用回答；需要联网时自动调 **Kimi web-search**（Kimi 只搜、DeepSeek 拍板）；可**下载论文到本地并自动录入**、**推送到 ModelScope**、**删除/重分类/标记已读/查重去重**等——凡用户在网页能做的它都能做（危险操作先列计划等你确认）
- **上云**：ModelScope 增量同步（PDF 走 git-lfs，KB 走 Markdown）
- **界面**：交互式 CLI + 零依赖 Web 界面 + **PaperBook 桌面版**（原生窗口，含「设置」控制台）；均内嵌统一馆长 agent（Web 右侧常驻聊天栏可拖拽调宽）

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
python -m service          # 交互式 CLI（主菜单：检索 / 论文库 / 每日简报 / 上传 / 云端同步 / 馆长 agent / 查重去重）
python -m service.web      # 网页界面，浏览器打开 http://127.0.0.1:8000
                           #   右侧常驻聊天栏：统一馆长 agent（查库/深读/联网搜索/下载/上云/删除），可拖拽调宽
```

## 私密信息说明（放心公开）

本仓库**不含任何真实密钥或个人数据**，clone 下来即可安全使用：

- 所有密钥只在 `.env` 里填写，而 `.env` 已被 `.gitignore` 排除，**永远不会被提交或推送**；仓库里只有 [.env.example](.env.example) 模板（空值）。
- [config.example.yml](config.example.yml) 里的 `__NAMESPACE__` 是占位符，`setup.py` 会替换成你自填的 ModelScope 用户名。
- 论文库、PDF、检索索引、嵌入模型、日报、馆长记忆与笔记（`knowledge-base/` `cache/` `papers/` `models/` `index/` `reports/` `agent-memory.md` `agent-notes/`）全部 gitignore，由 `setup.py` 在你机器上生成/导入，不入库。
- `service/reconcile_truth.py` 里放的是**空校准表**（一次性修正脚本，按需填你自己的 pid），不随仓库携带任何人数据。

## 密钥获取指引

密钥全部填在 `.env`（已被 `.gitignore` 排除，不会上传）。可 `python setup.py` 交互式生成，或复制 `.env.example` 手动填。

| 密钥 | 变量名 | 必填 | 获取方式 |
|---|---|---|---|
| DeepSeek | `DEEPSEEK_API_KEY` | 是（分类/摘要/每日裁判/馆长 agent） | [platform.deepseek.com](https://platform.deepseek.com) → API Keys |
| ModelScope | `MODELSCOPE_TOKEN` | 自建库模式需要 | [modelscope.cn](https://modelscope.cn) → 个人中心 → 访问令牌 |
| Kimi | `MOONSHOT_API_KEY` | 否（馆长联网搜索） | [platform.moonshot.cn](https://platform.moonshot.cn) → API Key |
| Zotero | `ZOTERO_USER_ID` / `ZOTERO_API_KEY` | 否（Zotero 链接） | [zotero.org/settings/keys](https://www.zotero.org/settings/keys) → 新建 key |
| 邮箱 | `SMTP_HOST/PORT/USER/PASS/TO` | 否（邮件推送） | QQ 邮箱 → 设置 → 账户 → 开启 SMTP → 生成授权码 |

> 可选密钥不填时，对应功能返回友好错误、其余照常。DeepSeek 不填则录入/分类/每日裁判/馆长 agent 不可用，但**检索 / 浏览仍可用**。

## 数据模式

- **示例库（可选，只读）**：若设置了环境变量 `MODELSCOPE_DEMO_NS`（指向某个公开示例论文库的 ModelScope 命名空间），`setup.py` 会 `git clone` 它，克隆即能检索/浏览，零 token。
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
python -m service.desktop                                  # PaperBook 桌面壳（原生窗口）
# 或直接双击 paperbook.pyw / PaperBook.bat，无终端黑窗
```

## 馆长 agent（统一入口）

Web 界面右侧聊天栏背后是一个**会查库、会读全文、会联网搜索、会维护库、有自己记忆**的馆长 agent。它以 **DeepSeek 为主体**掌握全局与决策，只在需要联网时调 **Kimi web-search**（Kimi 只搜、结果回传 DeepSeek 拍板）。

- **身份与规范**：根目录 [AGENT.md](AGENT.md) 是它的操作手册（身份 / 任务 / 工具 / 记忆规范 / 回答规范 / 禁止事项），每次问答前自动加载——想调整它的行为，改这个文件即可。
- **查库 / 深读**：`search_library` / `get_paper` / `deep_read` / `compare_papers` / `library_stats` / `list_by_area`，回答里用反引号给出可点击的 pid 引用；`deep_read` 读 PDF 全文并做结构化分析。
- **联网搜索**：`web_search`（Kimi），查库外最新进展 / 资料，带来源链接。
- **维护库 / 下载 / 上云 / 每日检索**：`download_paper`（下载并自动录入）、`ingest_paper`、`reclassify_paper`、`edit_paper`（改标题 / venue / 年份）、`fix_metadata`（重解析真实标题：arXiv 元数据 / PDF 元数据 / 首页文本，含中文标题翻译）、`fix_all_titles`（批量修正全库 arXiv 编号/占位名标题）、`fix_summary`（重读 PDF、AI 重新生成摘要并就地替换）、`find_duplicates`（按内容查重：arXiv/DOI/PDF 哈希/标题）、`deduplicate_papers`（去重并同步 ModelScope，需确认）、`mark_read`、`pull_library`、`push_to_cloud`、`delete_paper`、`run_daily_retrieval`、`read_daily_report`、`send_daily_email`——凡用户在网页能做的它都能做。
- **危险操作先确认**：`delete_paper` / `push_to_cloud`（推送到公开 ModelScope）只先生成计划，等你在对话里点「确认执行」后才真正执行。
- **自己的记忆**（仅存本地、不上云）：
  - 全局记忆 `agent-memory.md`（你的画像 / 偏好 / 纠错）；
  - 单篇深读笔记 `agent-notes/{pid}.md`（深读分析 + 对话中逐步积累的洞察，可随对话进化）。
- **模型可换**：默认 `deepseek-chat`（改 `config.yml` 的 `model.tasks.librarian`）；联网检索模型改 `chat` 段。

用法：浏览器打开 Web 界面，在右侧聊天栏提问；或在 CLI 主菜单选「6 馆长 agent」直接对话（如「深入读一下 Gavel 的核心调度机制」「网上搜一下最近 GPU 调度的最新进展」）。

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
service/            # 服务代码（Python 模块；含 desktop.py 桌面壳、settings.py 设置读写）
paperbook.pyw       # PaperBook 桌面启动器（双击打开，无终端黑窗）
PaperBook.bat       # PaperBook 桌面启动（bat 版，等价于双击 pyw）
paperbook.ico       # PaperBook 图标（快捷方式用）
install-shortcuts.ps1  # 一键创建桌面/开始菜单快捷方式（Windows，右键运行）
AGENT.md            # 馆长 agent 的操作手册（身份/任务/工具/记忆/规范）
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
agent-memory.md     # 馆长全局记忆（本地，gitignored，不上云）
agent-notes/        # 馆长单篇深读笔记（本地，gitignored，不上云）
```


