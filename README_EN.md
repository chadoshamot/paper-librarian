<p align="center">
  <a href="README.md">中文</a> · <strong>English</strong>
</p>

<p align="center">
  <img src="assets/logo.png" width="180" alt="Paper Librarian: an open book, a bookmark, and a guiding star" />
</p>
<h1 align="center">Paper Librarian · Your AI Research Assistant</h1>
<p align="center">Keep your papers organized and your research connected.</p>
<p align="center">
  <a href="#quick-start">Quick Start</a> · <a href="#librarian-agent">Librarian Agent</a> · <a href="#daily-recommendations-and-scheduling">Daily Recommendations</a>
</p>

A local AI paper librarian: import PDFs → build a bilingual knowledge base → search and open papers → sync with ModelScope → discover new papers daily → manage everything through one librarian agent. Bring your own API keys and keep your personal library on your machine. The built-in **PaperBook desktop app** opens with a double-click, without a terminal.

## PaperBook Desktop App

PaperBook wraps the Web interface in a native window. Double-click `paperbook.pyw` or `PaperBook.bat` to launch it on Windows without a console window. Closing the desktop window stops the local Web server.

- Install desktop dependencies with `python -m pip install pywebview pythonnet`; they are also included in `requirements.txt`.
- Windows uses **WebView2** to render the same interface as the browser version. If pywebview is unavailable, the launcher falls back to your default browser.
- The **Settings** tab (`设置`) lets you configure models, retrieval parameters, quality weights, cloud repositories, and API keys. Changes apply to subsequent requests. Credentials are written to the local `.env` file. Daily recommendation requirements have their own editor in the Daily Briefing tab.
- Launch from the command line with `python -m service.desktop`.
- To create desktop and Start menu shortcuts, right-click `install-shortcuts.ps1` and select **Run with PowerShell**. Run it again after moving the project to regenerate the shortcuts.

The application interface and the agent's default responses currently use Chinese. Switching README languages changes the documentation language.

## Features

- **Import papers:** extract metadata, generate bilingual summaries from available PDF text, classify and rename papers, organize them into folders, optionally link them to Zotero, and create Markdown knowledge cards. The Web interface supports multiple PDF uploads and processes them sequentially.
- **Search:** combine semantic embeddings, keywords, and title weighting using fastembed and jina-v2-base-zh. Choose precise retrieval or MMR-based exploration.
- **Browse and maintain your library:** navigate broad fields → research areas → work topics; open local PDFs, arXiv, DOI, ModelScope, or Zotero links; edit metadata, correct titles, reclassify, and delete papers.
- **Discover new papers daily:** retrieve candidates from multiple sources, rank them using quality scores and an LLM judge, generate a report, import selected papers, or send recommendations by email.
- **Librarian agent:** ask questions about your library, analyze papers, compare methods, maintain notes and preferences, search the Web through Kimi, download and import papers, mark papers as read, find duplicates, and manage your collection. DeepSeek coordinates the tools and produces the final response. Destructive operations require confirmation.
- **Cloud synchronization:** incrementally sync PDFs through Git LFS and Markdown cards through Git to ModelScope.
- **Three interfaces:** an interactive CLI, a Web interface without a frontend build step, and the native PaperBook desktop app. The Web interface includes a persistent, resizable agent sidebar.

## Quick Start

### 1. Requirements

- **Python 3.10+**; Python 3.11 or 3.12 is recommended. Some dependencies may not yet provide wheels for Python 3.14.
- **Git and Git LFS** for PDF cloud synchronization.
- A system Python installation or a virtual environment.

### 2. Clone and Install

```bash
git clone https://github.com/chadoshamot/paper-librarian.git
cd paper-librarian
python setup.py
```

The interactive setup checks Python, creates directories, imports or initializes a library, generates configuration and `.env`, installs dependencies, and runs smoke checks. Press Enter to skip optional integrations.

### 3. Launch

```bash
python -m service          # Interactive CLI
python -m service.web      # Open http://127.0.0.1:8000 in your browser
python -m service.desktop  # Native PaperBook window
```

On Windows, you can also double-click `paperbook.pyw` or `PaperBook.bat`.

## Credentials and Personal Data

The repository provides configuration templates. Credentials and personal library data are excluded from version control through `.gitignore`.

- Store credentials in `.env`. The tracked [.env.example](.env.example) contains empty credential fields and example connection settings.
- [config.example.yml](config.example.yml) uses `__NAMESPACE__` as a placeholder. Setup replaces it with your ModelScope namespace.
- Your library, PDFs, models, index, reports, agent memory, and notes live in ignored paths: `knowledge-base/`, `cache/`, `papers/`, `models/`, `index/`, `reports/`, `agent-memory.md`, and `agent-notes/`.
- `research-profile.md` stores your research requirements locally and is also ignored. It is not included in the public knowledge-base or PDF repositories.
- Chat sessions live in the ignored `chat-logs/` directory and synchronize to a separate **private** ModelScope repository, `paper-chat-logs`.
- `service/reconcile_truth.py` contains an empty calibration map for optional one-time corrections. Populate it with your own paper IDs only when needed.

Research requirements, memory, and paper text may be included in requests to your configured model provider when you use AI features. Local storage does not mean offline model inference.

## API Keys and Integrations

Run `python setup.py` to configure credentials interactively, or copy `.env.example` to `.env` and edit it yourself.

- **DeepSeek — `DEEPSEEK_API_KEY`:** used for classification, summaries, the recommendation judge, and the librarian agent. Create a key at [platform.deepseek.com](https://platform.deepseek.com).
- **ModelScope — `MODELSCOPE_TOKEN`:** needed for your own cloud repositories. Obtain an access token from your account settings at [modelscope.cn](https://modelscope.cn).
- **Kimi — `MOONSHOT_API_KEY`:** optional, for the agent's Web search. Create a key at [platform.moonshot.cn](https://platform.moonshot.cn).
- **Zotero — `ZOTERO_USER_ID` and `ZOTERO_API_KEY`:** optional, for Zotero integration. Create a key with the required library permissions at [Zotero API settings](https://www.zotero.org/settings/keys).
- **Email — `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASS`, and `SMTP_TO`:** optional, for recommendation emails. For QQ Mail, enable SMTP and use its authorization code rather than your account password.

Missing optional credentials disable the corresponding integration. Browsing and search remain available without a model key; metadata-only local maintenance does not require an LLM. AI import, classification, summaries, and agent conversations require a configured model. Daily retrieval can run without the LLM judge using `--no-llm`.

## Library Modes

- **Optional read-only example library:** set `MODELSCOPE_DEMO_NS` to the namespace of a public ModelScope example library. Setup can clone it for browsing and search without a cloud token.
- **Your own library:** configure your ModelScope namespace and token, import papers with `python -m service.ingest papers/example.pdf`, and synchronize with `python -m service.cloud sync`.

## Common Commands

```bash
python -m service
python -m service.ingest papers/example.pdf
python -m service.reclassify papers/example.pdf --category 方法 --area "机器学习系统::调度" --work slo-aware-scheduling --year 2024
python -m service.search "GPU cluster scheduling" --mode search --engine auto --top 10
python -m service.jump <pid> --kind local
python -m service.cloud sync
python -m service.daily --top 10 --sources openalex,arxiv,semantic_scholar --no-llm
python -m service.web --host 127.0.0.1 --port 8000
python -m service.desktop
```

The classification example uses the Chinese category and area names defined by the taxonomy. Replace them with valid entries from your library's taxonomy when reclassifying a paper. Retrieval supports `--mode search|explore` and `--engine auto|keyword|embedding`; paper links support `--kind local|arxiv|doi|cloud|zotero`.

## Librarian Agent

The sidebar agent uses **DeepSeek** for coordination and answers, and **Kimi Web search** when external information is needed. Tool results ground its responses in your actual library and retrieved sources.

- **Instructions:** [AGENT.md](AGENT.md) defines the agent's role, tools, memory rules, response format, and confirmation policy. It is loaded for each request.
- **Search and analysis:** `search_library`, `get_paper`, `deep_read`, `compare_papers`, `library_stats`, and `list_by_area`. Responses cite real paper IDs (`pid`). Deep reading extracts text across PDF pages and records page coverage and truncation. Use `refresh=true` to request a new analysis instead of using a cached note.
- **Organization and reading plans:** `audit_library` checks missing information and duplicates; `list_papers` supports pagination and filtering by area, keywords, or read status.
- **Research requirements:** `get_research_profile` reads the shared Markdown requirements; `update_research_profile` saves changes with revision checks.
- **External search:** `web_search` retrieves information through Kimi and returns sources for the coordinating agent to evaluate.
- **Library maintenance:** `download_paper`, `ingest_paper`, `reclassify_paper`, `edit_paper`, `fix_metadata`, `fix_all_titles`, `fix_summary`, `find_duplicates`, `deduplicate_papers`, and `mark_read` support importing, correcting, classifying, and organizing papers.
- **Sync and daily reports:** `pull_library`, `push_to_cloud`, `run_daily_retrieval`, `read_daily_report`, and `send_daily_email`.
- **Confirmation:** `delete_paper`, `deduplicate_papers`, and `push_to_cloud` create a plan first. Review it and click **Confirm execution** (`确认执行`) before the planned operation runs.
- **Memory:** `agent-memory.md` stores long-term preferences and corrections. `agent-notes/` stores paper analyses and accumulated insights. These files are kept outside cloud synchronization.
- **Models:** change `model.tasks.librarian` in your configuration to select the coordinating model, or edit the `chat` section to change the Web-search model.

Ask questions in the Web sidebar or select the librarian agent from the CLI menu. For example: “Explain Gavel's scheduling mechanism in detail,” “Check my library for duplicates and missing summaries,” or “Find recent work relevant to my research requirements.” The default agent instructions request Chinese responses.

## Chat History

Web conversations are saved as local sessions in `chat-logs/`. The application also attempts to synchronize them to your **private** ModelScope dataset repository, such as `your-namespace/paper-chat-logs`, separately from public PDF and knowledge-base repositories.

- **Automatic saving:** each completed Web turn updates its session file. Without a cloud token, sessions remain local; a later synchronization can upload them.
- **Resume conversations:** open **History** (`历史`) to see titles, update times, and turn counts, then select a session to continue it. **New conversation** (`新对话`) starts a separate session.
- **Delete sessions:** remove individual sessions from the history panel. The application also attempts to synchronize the deletion to the private repository.
- **Repository setup:** the first synchronization initializes the local chat repository and attempts to create a private dataset through the ModelScope SDK. If creation fails, create a **private** dataset named `paper-chat-logs` manually.

## Daily Recommendations and Scheduling

`python -m service.daily` retrieves candidates from arXiv, OpenAlex, and Semantic Scholar, merges source records, excludes papers already in your library, ranks candidates, and writes `reports/daily/YYYY-MM-DD.md` and `.json`. Empty results also update the report so old recommendations do not remain visible as the latest result.

Research goals, query keywords, sources, time ranges, and relevance thresholds share one local file: **`research-profile.md`**. Quality weights remain in the `quality` section of `knowledge-base/config.yml`.

In **Daily Briefing → My paper requirements** (`每日简报 → 我的论文需求`), edit keywords, research goals, recommendation preferences, result count, and date range, then select **Save requirements** (`保存需求`). An expandable editor also lets you modify the Markdown directly. On first use, the file inherits your existing `daily` configuration. Its YAML frontmatter controls retrieval; the Markdown body informs the recommendation judge, paper analysis, and agent conversations. Revision checks prevent an outdated browser or agent edit from overwriting newer changes.

The requirements file is ignored by Git and excluded from public library synchronization. Each recommendation run saves the requirements snapshot it used to `reports/daily/profiles/YYYY-MM-DD.md`. See [research-profile.example.md](research-profile.example.md) for the format. Saving requirements in the UI does not create an operating-system scheduled task.

The library supports title/area and read-status filters. **Ask the librarian to organize** (`让馆长整理`) starts a library audit and organization plan. **Discuss this paper with the librarian** (`与馆长讨论这篇论文`) includes the selected paper's actual ID. Conversations show tool progress and retain execution records in history. Tool errors are returned to the model, and reaching the tool-round limit triggers a final summary request. Scanned PDFs without extractable text still require OCR; the agent reports unavailable or incomplete reading rather than treating an abstract as the full paper.

**Windows Task Scheduler:**

```powershell
schtasks /Create /TN "PaperLibrarianDaily" /TR "D:\path\to\paper-librarian\run_daily.bat" /SC DAILY /ST 08:00 /F
```

**Linux / macOS cron:**

```cron
0 8 * * * /path/to/paper-librarian/run_daily.sh
```

## Troubleshooting

- **Slow first semantic search:** the embedding model is downloaded and cached on first use. The default download configuration uses `hf-mirror.com`.
- **PDFs are Git LFS pointers:** install Git LFS and synchronize again to retrieve the actual PDF files.
- **Conda environment mismatch:** run setup and application commands inside the same intended environment.
- **Change model providers:** update the key in `.env` and the provider, base URL, and model settings in the `model` section. Configure the search model separately in `chat`.

## Project Structure

```text
service/                  # Python services, including desktop and settings modules
service/static/           # index.html, app.css, app.js; no frontend build step
service/jobs.py           # Bounded job queue, serialized writes, isolated progress
service/research_profile.py # Requirements migration, validation, revision checks
service/storage.py        # Atomic file writes
paperbook.pyw             # Windows desktop launcher without a console window
PaperBook.bat             # Windows desktop launch script
paperbook.ico             # Desktop and shortcut icon
assets/logo.png           # Shared project logo for the README and application
install-shortcuts.ps1     # Desktop and Start menu shortcut installer
README.md                 # Chinese documentation
README_EN.md              # English documentation
AGENT.md                  # Librarian agent instructions
setup.py                  # Interactive setup
config.example.yml        # Configuration template
taxonomy.yml              # Taxonomy template
.env.example              # Credential template
requirements.txt          # Python dependencies
papers/                   # Incoming PDFs; ignored
cache/                    # Local PDF cache; ignored
knowledge-base/           # Bilingual knowledge base; ignored
chat-logs/                # Private chat sessions; ignored
index/                    # Retrieval index; ignored
models/                   # Cached embedding models; ignored
reports/daily/            # Daily reports and requirements snapshots; ignored
agent-memory.md           # Local agent memory; ignored
agent-notes/              # Local paper analysis notes; ignored
research-profile.md       # Local research requirements; generated and ignored
tests/                    # Offline workflow and HTTP regression checks
```

The Web and desktop interfaces share the local HTTP service in `service.web`; the frontend sends JSON requests and polls background jobs. The CLI invokes the same domain modules directly. `librarian` coordinates tool calls, `pipeline` imports and maintains papers, `retrieval` reads Markdown cards and caches embeddings, and `daily` uses the shared research requirements to rank new papers.

Markdown cards store paper content. The manifest maps stable paper IDs to PDF paths and Zotero records. Read status is stored separately, while analysis notes and research requirements remain local. Web write jobs run sequentially to avoid conflicting manifest or cloud Git updates within one process. Independent CLI processes do not yet share a cross-process write lock.

Offline checks do not call real models, cloud services, or email servers, and do not modify your personal library:

```bash
python -m unittest discover -s tests -v
node --check service/static/app.js
python tests/ui_smoke.py  # Optional: installed Chrome, temporary library, mocked agent
```

Use Python 3.10+. On Windows, launch scripts prefer the project's `.venv`, then the Python launcher, to avoid an older interpreter earlier on PATH. Verify live model, Web-search, embedding-download, ModelScope, and SMTP connections with your own configuration.

### Privacy and cloud sync

Every chat upload checks the ModelScope repository's visibility. An existing public repository, unknown visibility, network failure, or failed repository creation stops synchronization and keeps the session locally. ModelScope SDK 1.40+ is required; first use creates a private chat repository. PDF and knowledge-base visibility depends on your configured repositories; review their permissions and contents before pushing.

Cloud Git authentication uses a temporary child-process environment. Remote URLs contain no token, and synchronization removes credentials from legacy fetch/push URLs. Error output redacts tokens. Credentials, papers, chats, research requirements, and backups are excluded from this GitHub source repository.

Use your GitHub `USER_ID+USERNAME@users.noreply.github.com` address when committing code. Rewriting history cannot remove other people's clones, forks, or GitHub's cached old commits. Re-clone after a history cleanup to avoid reintroducing the old history.
