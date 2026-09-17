"""Stable public MCP catalog contract shared by runtime and diagnostics."""

from __future__ import annotations


PUBLIC_TOOL_ORDER: tuple[str, ...] = (
    "list_dir",
    "read_file",
    "grep_code",
    "code_rag_search",
    "file_info",
    "query_knowledge",
    "query_knowledge_strict",
    "query_table",
    "git_status",
    "git_diff",
    "apply_patch",
    "run_lint",
    "run_command",
    "analyze_file",
    "ingest_document",
    "remove_document",
    "reload_knowledge_base",
    "review_figures",
    "review_text",
    "import_external_file",
    "record_lesson",
)

PUBLIC_TOOL_NAMES: frozenset[str] = frozenset(PUBLIC_TOOL_ORDER)

if len(PUBLIC_TOOL_NAMES) != len(PUBLIC_TOOL_ORDER):  # pragma: no cover - import guard
    raise RuntimeError("PUBLIC_TOOL_ORDER contains duplicate names")


# The client injects this text into the model-visible system prompt. Keep it a
# routing map, not a second copy of every tool description.
MCP_INSTRUCTIONS = """Use CodeTrail for project facts. Locate code with code_rag_search (build_target scopes compilation), text with grep_code, files with read_file, directories with list_dir. Query specs with query_knowledge, high-risk constraints with query_knowledge_strict, exact table cells with query_table. In git repos inspect git_status/git_diff before apply_patch; non-git skip notices need no retry. Use analyze_file for images, PDF, ELF, or memory consistency. Query independent evidence in parallel and cite source/file:line. Missing or unverified evidence remains unknown. Plain text, XML, or promises are not tool calls; rely on completed structured results."""

if len(MCP_INSTRUCTIONS) > 700:  # pragma: no cover - import guard
    raise RuntimeError("MCP_INSTRUCTIONS exceeds the 700-character contract")


EVIDENCE_TOOL_NAMES: frozenset[str] = frozenset(
    {"code_rag_search", "query_knowledge", "query_knowledge_strict", "query_table"}
)


MODEL_TOOL_DESCRIPTIONS: dict[str, str] = {
    "list_dir": (
        "List a bounded directory tree inside AICODE_ROOT. Use for project structure, not file contents; "
        "then use read_file or code_rag_search. Returns tree lines and marks truncation."
    ),
    "read_file": (
        "Read a known text file inside AICODE_ROOT with numbered lines. Use code_rag_search first when the "
        "location is unknown; use analyze_file for PDF, images, ELF, or firmware. Continue from the reported line when partial."
    ),
    "grep_code": (
        "Find an exact identifier, literal, or safe regex inside AICODE_ROOT. Prefer code_rag_search for intent/semantic lookup. "
        "Returns file:line evidence; narrow path/include/pattern when partial."
    ),
    "code_rag_search": (
        "Locate code by intent or traverse confirmed graph evidence. semantic finds symbols; neighbors takes a symbol or relative "
        "file; path takes 'SRC -> DST'; context builds bounded evidence. For Chinese questions, keep the query mostly ASCII English "
        "and include distinctive identifiers. Select imported build_target for compilation scope; otherwise target is unknown. "
        "Returns path:line evidence plus uncertainties/truncation."
    ),
    "file_info": "Inspect size/type metadata for one sandboxed path before deciding how to read or analyze it.",
    "query_knowledge": (
        "Retrieve indexed PDF/spec/manual evidence. Use for document facts, not repository code; optionally restrict source to a "
        "basename. Cite returned sources and say evidence is unavailable when has_ref is false."
    ),
    "query_knowledge_strict": (
        "Answer high-risk numeric/spec constraints through the server-side grounding and refusal gate. Use query_knowledge for normal "
        "document lookup. Respect refused=true and review excluded_figures/excluded_text before asserting a value."
    ),
    "query_table": (
        "Read exact verified canonical table cells by register, address, or one-based row/column. "
        "Returns literal values with sources. Ambiguous, missing, damaged or unverified cells remain unknown; no model calls."
    ),
    "git_status": "Return the repository worktree status. Call before edits in a git project so user changes are preserved; a non-git root returns a skip notice, not an error.",
    "git_diff": "Return current repository diffs, optionally for one path or staged changes. Use before and after apply_patch in a git project; a non-git root returns a skip notice.",
    "apply_patch": (
        "Write files inside AICODE_ROOT using exactly one format; the diff is already a string, so do not wrap it in Markdown fences. "
        "A SEARCH/REPLACE minimum: `x.py\\n<<<<<<< SEARCH\\nold\\n=======\\nnew\\n>>>>>>> REPLACE`; SEARCH/context must match "
        "exactly and is never applied by similarity. B unified minimum: `--- a/x.py\\n+++ b/x.py\\n@@\\n-old\\n+new`. Maximum 5 files "
        "and 200 lines per file: unified counts added+removed; SEARCH/REPLACE counts all SEARCH+REPLACE payload lines. dry_run=true "
        "writes nothing and reports format, files, blocks, budget, locations, new_file, and only then `would apply`. A real apply runs "
        "only advisory in-process syntax checks; lint/tests do not run automatically. A failed advisory does not roll back and says the "
        "patch was applied. Run run_lint(fix=false) and run_command separately, each behind its own permission."
    ),
    "run_lint": "Run the configured formatter/linter on one sandboxed path. Use fix=false for check-only; this is a separate approval from apply_patch.",
    "run_command": (
        "Run one bare command from the server whitelist inside AICODE_ROOT (tests/static checks; build needs build_commands "
        "in client.json; git uses git_status/git_diff). Each call reloads extra_allowed_commands (PATH names) and revalidates "
        "extra_allowed_command_dirs (user-trusted host tool dirs; invalid grants deny; not in containers). Bare names only; "
        "approval and argument checks apply. timeout: integer 1..600 s; client may stop earlier. Output bounded, failures highlighted."
    ),
    "analyze_file": (
        "Inspect a sandboxed image, PDF spot-check, ELF, or firmware/binary without ingesting it. For ELF choose a closed-set view and "
        "narrow target/limit when partial. consistency compares ELF with linker_map/linker_script/preload_log/dram_config; "
        "missing evidence remains unknown. Use read_file for plain text."
    ),
    "ingest_document": (
        "Ingest a sandboxed file into knowledge.json. PDF preflight_only estimates cost without writes; fresh rebuilds the KB. "
        "MinerU needs mineru_content_list plus its generation-time PDF SHA-256; strict excludes unverified OCR. "
        "Queries auto-reload after success. Ingest can take minutes: KB tools report busy until completion; wait, do not retry. "
        "Report [CODETRAIL_ACTION_REQUIRED] items and next steps. resume reuses validated checkpoints. "
        "redo_pages/redo_figures/retry_failed are mutually exclusive PDF selectors, require resume, and cannot combine with "
        "fresh/preflight. Unselected valid content is preserved; fresh and preflight cannot combine."
    ),
    "remove_document": "Remove every knowledge-base chunk for one source basename; query tools auto-reload afterward.",
    "reload_knowledge_base": "Force immediate fail-loud reload of knowledge.json and report status; normal queries already auto-reload.",
    "review_figures": (
        "List or fix structured PDF figures. action=list is read-only; action=fix requires figure_id, current expected_revision, canonical "
        "payload JSON, and confirm_against_image=true after human inspection. Conflicts and invalid payloads write nothing."
    ),
    "review_text": (
        "List/show OCR or correct/confirm/revoke a revision. Changes require expected_revision and expected_sha256. "
        "Correction does not confirm; set confirm_against_source only after source inspection. Strict accepts current verified, undamaged text."
    ),
    "import_external_file": "Copy an explicitly allowed external file into the sandbox, then use the returned relative path with analyze_file or ingest_document.",
    "record_lesson": (
        "Propose a durable behavior rule only after the user corrected how the agent works. This ask-permission tool is not for factual "
        "corrections, tool errors, or self-invented preferences."
    ),
}

if set(MODEL_TOOL_DESCRIPTIONS) != PUBLIC_TOOL_NAMES:  # pragma: no cover - import guard
    raise RuntimeError("MODEL_TOOL_DESCRIPTIONS must cover the public catalog exactly")
