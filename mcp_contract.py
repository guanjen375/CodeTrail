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
    "import_external_file",
    "record_lesson",
)

PUBLIC_TOOL_NAMES: frozenset[str] = frozenset(PUBLIC_TOOL_ORDER)

if len(PUBLIC_TOOL_NAMES) != len(PUBLIC_TOOL_ORDER):  # pragma: no cover - import guard
    raise RuntimeError("PUBLIC_TOOL_ORDER contains duplicate names")


# The client injects this text into the model-visible system prompt. Keep it a
# routing map, not a second copy of every tool description.
MCP_INSTRUCTIONS = """Use CodeTrail for facts about the current project or indexed documents. Locate unknown code with code_rag_search, exact text with grep_code, known files with read_file, directories with list_dir, and indexed specs with query_knowledge; use query_knowledge_strict for high-risk numeric constraints. In git repos, inspect git_status/git_diff before apply_patch; a non-git root gets a skip notice. Use analyze_file for images, PDF spot checks, ELF, or firmware. Query independent evidence in parallel, then answer from returned source/file:line evidence. If evidence is absent, say so and do not guess. Plain text, XML, or promises are not tool calls; rely only on completed structured tool results."""

if len(MCP_INSTRUCTIONS) > 700:  # pragma: no cover - import guard
    raise RuntimeError("MCP_INSTRUCTIONS exceeds the 700-character contract")


EVIDENCE_TOOL_NAMES: frozenset[str] = frozenset(
    {"code_rag_search", "query_knowledge", "query_knowledge_strict"}
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
        "and include distinctive identifiers. Returns path:line evidence plus explicit uncertainties/truncation."
    ),
    "file_info": "Inspect size/type metadata for one sandboxed path before deciding how to read or analyze it.",
    "query_knowledge": (
        "Retrieve indexed PDF/spec/manual evidence. Use for document facts, not repository code; optionally restrict source to a "
        "basename. Cite returned sources and say evidence is unavailable when has_ref is false."
    ),
    "query_knowledge_strict": (
        "Answer high-risk numeric/spec constraints through the server-side grounding and refusal gate. Use query_knowledge for normal "
        "document lookup. Respect refused=true and review excluded_figures before asserting a value."
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
        "Run one command from the server whitelist inside AICODE_ROOT. Allowed defaults cover tests and static checks; build commands "
        "require build_commands in client.json, and git is not allowed (use git_status/git_diff). timeout is an integer 1..600 seconds; "
        "the MCP client may stop waiting earlier. Output is bounded and highlights failures."
    ),
    "analyze_file": (
        "Inspect a sandboxed image, PDF spot-check, ELF, or firmware/binary without ingesting it. For ELF choose a closed-set view and "
        "narrow target/limit when partial; use read_file for plain text."
    ),
    "ingest_document": (
        "Add a sandboxed document/image/binary to knowledge.json. PDF preflight_only=true estimates cost with zero KB writes; fresh=true "
        "rebuilds the KB and cannot be combined with preflight. For local MinerU PDF text, provide both mineru_content_list and the "
        "PDF digest recorded at generation in mineru_pdf_sha256; strict excludes unverified OCR text. Query tools auto-reload after success. This call can run for minutes; "
        "knowledge-base tools and a second ingest report busy until it finishes, so wait for this result instead of retrying. When the "
        "result contains [CODETRAIL_ACTION_REQUIRED], report the listed figures and their next step instead of calling it done."
    ),
    "remove_document": "Remove every knowledge-base chunk for one source basename; query tools auto-reload afterward.",
    "reload_knowledge_base": "Force immediate fail-loud reload of knowledge.json and report status; normal queries already auto-reload.",
    "review_figures": (
        "List or fix structured PDF figures. action=list is read-only; action=fix requires figure_id, current expected_revision, canonical "
        "payload JSON, and confirm_against_image=true after human inspection. Conflicts and invalid payloads write nothing."
    ),
    "import_external_file": "Copy an explicitly allowed external file into the sandbox, then use the returned relative path with analyze_file or ingest_document.",
    "record_lesson": (
        "Propose a durable behavior rule only after the user corrected how the agent works. This ask-permission tool is not for factual "
        "corrections, tool errors, or self-invented preferences."
    ),
}

if set(MODEL_TOOL_DESCRIPTIONS) != PUBLIC_TOOL_NAMES:  # pragma: no cover - import guard
    raise RuntimeError("MODEL_TOOL_DESCRIPTIONS must cover the public catalog exactly")
