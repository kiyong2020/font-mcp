# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Setup & Running

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Start the MCP server (stdio transport)
python server.py
```

To register with Claude Desktop, copy `claude_desktop_config.example.json` into `~/Library/Application Support/Claude/claude_desktop_config.json` and replace the placeholder paths with absolute paths to `.venv/bin/python` and `server.py`.

Case memory is persisted to `~/.font-mcp/cases.json` at runtime.

## Architecture

Three files, strict separation of concerns:

- **[server.py](server.py)** — FastMCP entry point. Registers all MCP tools, owns the `CaseMemory` singleton, and wraps every `FontOps` / `CaseMemory` call in a `try/except` that returns `{"error": "..."}` instead of raising.
- **[font_ops.py](font_ops.py)** — All fontTools logic in `FontOps`. No MCP dependency, so it can be tested or reused independently. Every method returns only JSON-serializable values.
- **[memory.py](memory.py)** — `CaseMemory`: ChromaDB-backed case store using the built-in `all-MiniLM-L6-v2` embeddings (no API key). Persisted under `~/.font-mcp/chroma/`. Public API: `add()`, `update_outcome()`, `search()`, `all()`.

## Design Constraints

- **Never overwrite the source font.** All write tools (`apply_ttx_patch`, `set_name_record`, `set_vertical_metrics`, `subset_font`, `merge_fonts`, `convert_format`, `instance_variable`) require an `output_path` and save to a new file.
- **All tools return structured dicts/lists**, never free text. Errors surface as `{"error": "..."}` so the agent can decide the next action.
- **`validate_and_record` is preferred over raw `record_case`** at the end of a fix cycle — it auto-runs `validate_font`, attaches the verdict (PASS/FAIL/WARN) to the case, and stores the patch body, so the learning loop never breaks if the agent forgets.
- **`sync_all=True` (default) in `set_vertical_metrics`** synchronises OS/2 sTypo\*, hhea, and OS/2 usWin\* and sets the `USE_TYPO_METRICS` bit — the standard fix for cross-app line-spacing inconsistencies.

## Tool Inventory

- **Diagnosis (read-only):** `font_info`, `list_tables`, `dump_table_ttx`, `get_name_records`, `get_vertical_metrics`, `list_features`, `diagnose` (aggregated checks across vertical metrics, name table, fsSelection/macStyle bits, cmap)
- **Edit (writes new file):** `apply_ttx_patch`, `set_name_record`, `set_vertical_metrics`
- **Convert (writes new file):** `subset_font`, `merge_fonts`, `convert_format` (WOFF/WOFF2 wrap), `instance_variable` (variable → static instance)
- **Visual / regression:** `render_sample` (PNG + cmap miss ratio), `diff_fonts` (field + name diffs)
- **Validation:** `validate_font` (fontbakery)
- **Memory (RAG):** `find_similar_cases`, `record_case`, `validate_and_record`, `update_case_outcome`
- **Prompts:** `diagnose_then_fix`, `fix_vertical_metrics`, `add_korean_name_records`

## Typical Agent Workflow

```text
diagnose(font)                              # one-shot issue list
  → for each issue:
    find_similar_cases(issue.message)       # ChromaDB semantic match
      → if match.score > 0 and match.patch_ttx:
        apply_ttx_patch(...)                # reuse verified patch
      else:
        set_vertical_metrics / set_name_record / apply_ttx_patch (per issue.hint)
validate_and_record(output_path, ...)       # validate + store with verdict
update_case_outcome(case_id, success)       # later, when reapplying
```

The case score (`success_count - fail_count`) lets verified patches surface first in future searches.
