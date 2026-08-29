# For AI agents — adopt mnelo as your memory

If you're an AI agent (Claude Code, Cursor, Hermes, …) setting up mnelo
for your user, follow these operational steps.

## 1. Choose the install directory (LIVE_ROOT) — propose options, let the user pick or customize

- The **recommended** path is a **visible, workspace-adjacent directory**
  — e.g. `$HOME/mnelo-data`, `$HOME/.local/share/mnelo`, or
  `<workspace>/mnelo-data`. Hidden dot-dirs under `$HOME` work too but are
  easy to lose track of.
- Keep it out of the repo you just cloned — memory data isn't source code.
- Offer candidates and let the user choose or supply their own path —
  **decide before first use** (the DB, embedder config, and vector index
  all live here; moving later means stop → migrate → restart).
- Set via env `MNELO_MEMORY_DIR` (finer control: `MNELO_MEMORY_CONFIG` /
  `MNELO_MEMORY_DB_PATH`). Persist it in the shell profile
  (`~/.profile` / `~/.bashrc`) so scripts and the server agree. To
  customize the health-check report dir, override with
  `MNELO_CRON_OUTPUT_DIR` (default `$MNELO_MEMORY_DIR/cron/output`,
  recreated by `health_check.py` on every run).

## 1.5 Decide the listen mode (single-machine vs multi-agent) — affects mcp_server --host

[8/9 Tailscale multi-agent] 主人拍板 mnelo 改成 multi-agent 远程调用.
这一决策决定 mcp_server 监听的 bind address, **一旦选错就要重启
mcp_server** (`launchctl kickstart -k ai.mnelo.mcp`).

**Ask the user (or decide from context):**

> "mnelo 是用 Tailscale Service (admin console 注册 *.ts.net 域名) 发布,
> 还是裸 Tailscale IP (100.x.x.x) 发布?"

| 场景 | `--host` | 理由 |
|---|---|---|
| **单机本地** (默认保守) | `127.0.0.1` | Tailscale / 反向代理 / 跨网都用不上, 最安全 |
| **多机 / 多 agent via Tailscale Service** (admin console 注册端口) | `127.0.0.1` | Tailscale daemon forward Service 流量到本机 loopback, 服务端 socket 接得到 |
| **多机 / 多 agent via 裸 Tailscale IP** (mesh peer 直连 IP) | `0.0.0.0` | utun* 接口路由, 客户端走 mesh 内 IP, 服务端 socket 必须在 0.0.0.0 上才收得到 |
| **公网 / 任意可达网络** | (不要 Tailscale; 用 auth + ACL + 公开证书) | 威胁模型不同 |

**判断方法**: 看 Tailscale admin console → Services 节点.
- 有该端口条目 → Service 模式, 绑 127.0.0.1 (Tailscale daemon forward 兜底)
- 只有 mesh peer + Tailscale IP → 裸 IP 模式, 绑 0.0.0.0

**白名单策略不变** (loopback + 100.64.0.0/10 CGNAT 接受, LAN/公网/IPv6
拒绝). 变的只是 bind address — *不*监听就无意义, 监听则有 Tailscale
转发兜底.

**改 plist 步骤** (macOS):

```bash
# 编辑 ~/Library/LaunchAgents/ai.mnelo.mcp.plist
# ProgramArguments 中 --host 127.0.0.1 → 0.0.0.0
sed -i '' 's|<string>127.0.0.1</string>|<string>0.0.0.0</string>|' \
  ~/Library/LaunchAgents/ai.mnelo.mcp.plist

# 备份 (用 plist 装前 cp 一份, 走 customization hygiene)
cp ~/Library/LaunchAgents/ai.mnelo.mcp.plist{,.bak.$(date +%Y%m%d)}

# KeepAlive plist 重启
launchctl unload ~/Library/LaunchAgents/ai.mnelo.mcp.plist
launchctl load ~/Library/LaunchAgents/ai.mnelo.mcp.plist

# 验证
curl -sS -o /dev/null -w "127.0.0.1: %{http_code}\n" http://127.0.0.1:8086/health
tailscale ip -4 | head -1 | xargs -I {} \
  curl -sS -o /dev/null -w "Tailscale: %{http_code}\n" http://{}:8086/health
```

**已知坑** (SOUL §mnelo ops):
- 主人 8/6 之前 `_validate_loopback_host` 拒绝 0.0.0.0; 8/8 已扩展白名单
  含 Tailscale CGNAT (commit 3e538de, 33 tests 绿).
- `launchctl kickstart -k` 在 KeepAlive plist 上 = *重启* 不是停. 真停
  要 `launchctl unload` (后跟 `launchctl load`).
- `mcp_server.py` 启动后由 OS firewall 兜底. macOS 默认
  Application Firewall 不挡 8086; Tailscale mesh 端不依赖 LAN 防火墙.

**Tailscale client 视角** (vps 端 agent):

```bash
# 假设 macbook Tailscale IP = 100.83.50.99
MNELO_URL="http://100.83.50.99:8086/mcp"
MNELO_TOKEN="$(cat ~/.config/mnelo/auth_token)"  # 共享 token
curl -sS -X POST "$MNELO_URL" \
  -H "Authorization: Bearer $MNELO_TOKEN" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"my-agent","version":"1.0"}}}' \
  | head -5
```

## 2. Choose the vector backend — based on the actual machine

See [VECTOR_BACKENDS.md](VECTOR_BACKENDS.md) for the full comparison.
Short version:

- **`usearch`** (f16) works on any CPU — the default fallback.
- **`zvec`** (INT8) only if this CPU supports AVX2+ — auto chain top.
- **`zvec`** adds native full-text, but **requires AVX2+** — do **not**
  install it on old CPUs (it crashes on `import` there). Detect the CPU
  or ask.
- Configure via `config.toml [search] backend` or env
  `MNELO_MEMORY_SEARCH_BACKEND`.

## 3. Ask the user about their primary language before picking an embedding model

(switching later requires re-initializing the DB — so ask *before*
first use):

| Language | Model | Dim |
|---|---|---|
| Chinese | `bge-small-zh-v1.5` (default) | 512 |
| English | `bge-small-en-v1.5` | 384 |
| Mixed / multilingual | `paraphrase-multilingual-MiniLM-L12-v2` | 384 |

## 4. After install, update your own config to adopt mnelo as your memory system

Add a note to your `SOUL.md` / `CLAUDE.md` (or equivalent):

> **Memory**: I use the mnelo MCP server (streamable-http at
> `http://127.0.0.1:8086/mcp`) as my memory system.
> - `memory_remember` — persist important facts & decisions, with a type
>   (`preference`, `decision`, …)
> - `memory_recall` — retrieve before answering anything that might not
>   be in context
> - `memory_get_digest` / `memory://session/digest` — the session-state
>   summary injected at start
> Keep mnelo running. If it's down, work without it and note the memory gap.

## 5. Verify

Run `scripts/health_check.py` (or hit `/health`) and confirm the server
is ready before declaring done.

## 6. Backup / restore

See [OPERATIONS.md](OPERATIONS.md#backup--restore) for full details.
**⚠️ PRIVACY**: if the user picks GitHub auto-push (option 3), the
destination repo MUST be private. mnelo.db contains personal memory,
decisions, preferences, entity relationships — PII-level. Pushing to a
public repo = data leak. Verify before enabling `ai.mnelo.backup.plist`
with a GitHub-backed `snapshot_dir`.

Quick reference:

```bash
# Manual backup (writes to config [backup] snapshot_dir + sha256 sidebar)
python scripts/backup_db.py
python scripts/backup_db.py --dry-run   # preview only

# List snapshots + verify sha256
python scripts/restore_db.py --list

# Verify a snapshot (dry-run, never touches live)
python scripts/restore_db.py --latest --dry-run

# Actual restore (isolates current db → memory.db.corrupt-<date>, atomic replace)
python scripts/restore_db.py --from 2026-08-05-030000
# or: python scripts/restore_db.py --latest
```

**Schedule role for the agent**: install.sh step 12 asks the user where to
store backups (1: local default, 2: NAS via dr-backup.sh, 3: GitHub repo
via dr-backup.sh, 4: custom) and how many to keep (default 30 ≈ 4 weeks).
The ai.mnelo.backup.plist then runs `wed+sun 03:00` via launchd. If the
user has `dr-backup.sh` already wired, the snapshots end up rsync'd →
NAS → GitHub automatically.

**Recovery drill (run monthly)**: `scripts/restore_db.py --latest --dry-run`
confirms the most recent snapshot is healthy. If this fails, the snapshot
is corrupt — DESIGN §3.11.2 says fall back to the previous one. If all
snapshots fail, the backup chain is untrustworthy; investigate
`logs/mnelo.backup.error.log` and re-test by manually running
`backup_db.py`.

## 📌 Adding a new entity kind (open taxonomy — no registration needed)

Entity `kind` is free-form; "adding a kind" simply means *starting to use
it*. When the user introduces a new kind, record it as a convention and
use it consistently:

> Add a new entity kind: `product`, for product-related entities. When
> using `memory_remember` for products, pass `kind: 'product'` and keep
> naming/aliases consistent (e.g. id `product:sku-1024`). Record this
> convention in your CLAUDE.md/SOUL.md and use it consistently; filter
> product recalls with `kind: 'product'`.

Optionally: add the kind to `[recall] boost_kinds` to give it the same
recall boost as `stock`; backfill existing entities via `correct()` or a
script.

### ⚠️ Kind is open, but entity `id` is namespace-gated (8/8 P1)

Although `kind` is free-form, **the entity `id` is checked by the
namespace guard** (`memory._enforce_entity_namespace_guard`, [8/8 P1]).
Pick an id in one of these shapes:

| Shape | Example | Notes |
|---|---|---|
| Explicit namespace prefix | `stock:sh600021`, `identity:yanru`, `holding:2026-08-10`, `loop:daily_research`, `task:build_x` | Five namespaces are allowlisted (`identity:`, `stock:`, `holding:`, `loop:`, `task:`) |
| `master_` prefix | `master_hermes_update_preflight`, `master_skill_aesthetic` | SOUL §mnelo ops #4 convention — use this when introducing a brand-new top-level subject |
| Blacklist (always rejected) | `anno:*`, `TOKEN_*` | `anno:*` = legacy HonchoImporter NER residue; `TOKEN_*` = random session tokens. **Never** use these as ids — drop the entity and write the info into chunk metadata instead |
| Anything else | `sonnet`, `foo_bar` | Allowed since A1 (2026-08-10), any `kind` is accepted for these ids |

Validation rules enforced regardless of `kind` choice:

- `kind` is a non-empty string, ≤ 64 chars (`validation.py:147-152`).
- `id` matches `_ID_RE.pattern` (post-8/16 whitelist: unicode + `/` + space; see `_ID_ALLOWED_DESC` / `_ID_REJECTED_DESC` in `validation.py`).
- `concept` kind's `name` ≤ 50 chars (prevents "imported sleep runs at
  midnight" being smuggled in as an entity name; use chunk content for
  sentences).
- Blacklist (`anno:*`, `TOKEN_*`) is rejected on every entity regardless
  of kind.

When unsure, default to `master_<short_descriptive_id>` with a `kind`
that names the domain (e.g. `kind: 'lesson'`, `kind: 'product'`).

## 🎯 Suggesting new kinds from the user's profile

The seed kinds (`stock`, `person`, `concept`, …) are a starting point,
not a limit. When you first meet a user, skim their domain (documents,
files, existing data) and propose a small kind set they'll actually use
— then record it in CLAUDE.md/SOUL.md as the convention. For example,
for a Chinese A-share investor who tracks holdings and reads a daily
position-summary report:

> - `portfolio` — the overall holdings set (anchor: id `portfolio:a-share-2026`)
> - `position` — a single holding (id `position:sh600519`)
> - `stock` — the security itself (seed kind; relate `position` → `stock`)
> - `plan` — a purchase / next-step plan ("下月采购 CAT-1024")
> - `strategy` — an investment / trading strategy
> - `report` — recurring reports (daily/weekly position summaries)
> - `watchlist` — a watchlist of candidates

Keep it to **5–7 kinds** — each must earn its place by being referenced
across chunks. Add a new one only when the user actually introduces that
concept.

## 🧠 Using mnelo — write & retrieve well

### 1. `memory_type` — the chunk's lifecycle type

The rule classifier auto-tags new writes, but pass the type explicitly
when you know it:

| Type | Use when | Example you'd classify |
|---|---|---|
| `preference` | a like / dislike / style preference | "我偏好简洁日报" |
| `decision` | a decision + (ideally) its reasoning | "我决定下月采购 CAT-1024" |
| `episode` | a dated event | "今天建仓了 CAT-1024" |
| `procedure` | steps / how-to / workflow | "做周报的流程…" |
| `ephemeral` | draft / placeholder / WIP | "临时草稿，稍后处理" |
| `fact` | everything else (default) | — |

Write: `memory_remember(content, ..., memory_type='decision')` when you
know it; **omit it to let the auto-classifier decide** (it handles
简体/繁體/EN).

### 2. Entity `kind` — how to structure concepts

Create an entity when a thing is **referenced across chunks, has
aliases, or is a graph anchor** — not for one-off mentions. Keep IDs
consistent: `kind:slug` (e.g. `product:sku-1024`), aliases in
`aliases_json`. Attach entities to chunks via `memory_remember(entities=[{id,
kind, name, aliases}])`, and connect concepts via `memory_relate(source_id,
target_id, relation, evidence_chunk_id=...)` — every relation should
point back to the chunk that justifies it.

### 3. Recall before you answer

Before answering anything that might live in the user's memory (identity,
decisions, ongoing work), call `memory_recall` — with filters when
useful (`{'type': 'decision'}`, `{'source': ...}`). At session start the
digest (`memory_get_digest`) already gives you the current state; expand
a line with `ref` when you need the underlying detail.

### 4. Consistency is the contract

Types and kinds only pay off if used consistently. When you introduce a
new kind, record the convention in your CLAUDE.md/SOUL.md (see "Adding
a new entity kind" above).

## One-line install prompt

Hand this to any AI coding agent (Claude Code, Hermes, Cursor, …):

> **Install and adopt mnelo from https://github.com/cure4u/mnelo
> as my memory system:**
> 1. Clone, set up the venv, `pip install -r requirements.txt`, run
>    `scripts/init_db.py`, launch the MCP server on port 8086, and verify
>    with `scripts/health_check.py` (report back when `MCP server
>    ready`).
> 2. **Ask me my primary language first** (中文 / English / mixed), then
>    set the matching embedding model in `config.toml [embedder]`
>    *before* first use (switching later requires re-initializing the
>    DB).
> 3. Vector backend: `auto` (default) picks **`zvec`** if this CPU
>    supports AVX2+ else **`usearch`** (f16). Configure via `config.toml
>    [search] backend = 'auto'|'usearch'|'zvec'`.
> 4. After it's running, **update your own SOUL.md / CLAUDE.md** to
>    declare: "I use the mnelo MCP server
>    (streamable-http at `http://127.0.0.1:8086/mcp`) as my memory system —
>    `memory_remember` to persist, `memory_recall` to retrieve,
>    `memory_get_digest` for the session digest."

The agent handles venv creation, pip install, the embedding-model
download, and the health probe. Typical install ~90s (the 92 MB model
download is the slow part).
