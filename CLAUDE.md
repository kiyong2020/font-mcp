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

Six files, strict separation of concerns:

- **[server.py](server.py)** — FastMCP entry point. Registers all MCP tools, owns the `CaseMemory` + `FontBuilder` singletons, and wraps every call in `try/except` that returns `{"error": "..."}`.
- **[font_ops.py](font_ops.py)** — All fontTools logic in `FontOps`. No MCP dependency, so it can be tested or reused independently. Every method returns only JSON-serializable values.
- **[memory.py](memory.py)** — `CaseMemory`: ChromaDB-backed case store using the built-in `all-MiniLM-L6-v2` embeddings (no API key). Persisted under `~/.font-mcp/chroma/`. Public API: `add()`, `update_outcome()`, `search()`, `all()`.
- **[build_spec.py](build_spec.py)** — Parses the 6-sheet XLSX work order (meta / metrics / weights / outputs / names / subset) into a typed `BuildSpec`. Used by `parse_build_sheet` and `build_font_family`.
- **[extract_spec.py](extract_spec.py)** — Reverse direction: takes a list of existing font paths and writes a filled 6-sheet XLSX with the same schema `build_spec.py` reads. Exposed as the `extract_build_sheet` MCP tool.
- **[font_builder.py](font_builder.py)** — `FontBuilder` orchestrator. Takes a `BuildSpec` + base fonts (keyed by `weight_class`) and produces the full family under `output_dir`. Patches head/OS/2/hhea/post/name per weight and emits OTF/TTF/WOFF/WOFF2/subset/VF into per-format subdirs.

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
- **Work order (XLSX → family):** `parse_build_sheet`, `build_font_family`
- **Work order (family → XLSX):** `extract_build_sheet` — reverse-extract name table + metrics from a set of existing fonts into the 6-sheet work order, ready for designer review and re-build
- **Prompts:** `diagnose_then_fix`, `fix_vertical_metrics`, `add_korean_name_records`, `build_from_sheet`

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

## Work-Order (Google Sheets / XLSX) Pipeline

For [제작의뢰서]-driven family builds (Confluence work order → font files):

```bash
# 1a. Generate a starter XLSX (현대캐피탈 산스 sample values pre-filled)
python scripts/make_build_template.py build_template.xlsx

# 1b. OR extract from an existing family (via MCP tool):
#     extract_build_sheet(font_paths=["/abs/Rg.otf", "/abs/Bd.otf", ...],
#                         output_xlsx="/abs/work_order.xlsx")
#     — fills meta/metrics/weights/names from each font's tables.

# 2. Upload to Google Drive → open as Sheets → designer edits values
# 3. File → Download → Microsoft Excel (.xlsx) → save locally
```

The workbook has six sheets (schema mirrors the Confluence [제작의뢰서] page):
- `meta` — project / vendor / copyright / nameID 0,7,8,9,11–14 values
- `metrics` — head·hhea·OS/2·post (UPM, ascender/descender/lineGap, strikeout, fsType, underline)
- `weights` — one row per static weight: `weight_class | style_name | is_bold | is_italic | korean_family | latin_family | psname | fullname_suffix | base_font_key`
- `outputs` — toggle OTF / TTF / WOFF / WOFF2 / WOFF_subset / WOFF2_subset / VF + per-format `psname_suffix`
- `names` — extra nameID records with `platform` (win|mac) and `lang` (en|ko|…)
- `subset` — `mode=preset|text|unicodes`, `preset=common_kr`, `layout_features`

Then call the MCP from the agent:

```text
parse_build_sheet(sheet_path)                   # preview / validate the spec
build_font_family(
    sheet_path=...,
    output_dir=...,
    base_otf={"400": "/abs/...Rg.otf", "700": "/abs/...Bd.otf", ...},
    base_ttf={...},                              # if TTF output enabled
    variable_font="/abs/...VF.ttf",              # if VF output enabled
    validate=True,                                # fontbakery on each OTF/TTF
)
```

Outputs land at `{output_dir}/{OTF,TTF,WOFF2,WOFF2_subset,VF}/{psname}{suffix}.ext`. Each successful artefact is automatically inserted into `CaseMemory` with `validation_after` set, so subsequent `find_similar_cases` queries surface verified family-build patches.

Per-weight patches applied by `build_font_family`:
- `head.fontRevision` from `meta.version`; `head.macStyle` BOLD/ITALIC bits from `weights[i].is_bold/is_italic`
- `OS/2.usWeightClass = weights[i].weight_class`, `fsType`, `sTypo*`, `usWin*`, strikeout, `fsSelection` (BOLD/ITALIC/REGULAR + `USE_TYPO_METRICS` when `metrics.use_typo_metrics`)
- `hhea` ascent/descent/lineGap; `post` underline position/thickness
- `name` records 1/2/4/16/17 in both en-US (0x409) and ko-KR (0x412) using `latin_family`/`korean_family`; auto-generated 3 (uniqueID), 5 (version), 6 (PSname with `outputs.psname_suffix`); all extra rows from the `names` sheet
