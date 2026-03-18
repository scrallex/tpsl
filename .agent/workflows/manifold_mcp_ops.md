---
description: How to Properly Utilize the Manifold Engine MCP Tools
---
# Manifold Engine MCP Server Directives

This workflow defines the EXPLICIT operating constraints for utilizing the Manifold Engine tools on this repository. When you are tasked with searching the codebase, analyzing structure, or exploring code logic, prioritize these steps over native bash tools (`grep`/`find`).

## 1. Context and Absolute Paths
The Manifold Server indexes paths absolutely from its ingest root. 
- You MUST provide exact absolute paths (e.g. `/path/to/my/file.py`) to tools like `mcp_manifold-engine_get_file`, `analyze_code_chaos`, `predict_structural_ejection`, and `visualize_manifold_trajectory`.
- Relative strings or basenames will instantly fail.
- In this workspace, the correct ingest root is `/sep/trader`.
- If the MCP server’s working directory is different (e.g., `/sep/structural-manifold-compression/SEP-mcp`), ingest with `root_dir: "../../trader"` so the index points at `/sep/trader`.

## 2. Ingestion Rules
- Do NOT perform search queries until you verify the index stats (`get_index_stats`).
- If the index is empty or extremely stale, run `ingest_repo` with `clear_first=true` on the absolute root path you want to evaluate. 
- Confirm `Last ingest root` equals `/sep/trader` before running dependency tools.

## 3. The 512-Byte Structural Minimum
- `compute_signature` and `verify_snippet` mathematically parse sliding 512-byte block structures.
- You MUST NOT pass small strings, single lines of code, or fragments under 512 bytes to these functions. They will return a `0-byte` tensor and fail.

## 4. Pruning and Code Clean Up
- Use `batch_chaos_scan` instantly upon entering to find technical debt.
- To find copies of a script, run `get_file_signature` on the absolute path of the target, and pass the resulting string (e.g., `c0.269_s0.000_e0.925`) to `search_by_structure`. This will mathematically locate all file clones and structural matches in the entire repo.
- For `analyze_blast_radius`, use the indexed repo-relative path (e.g., `scripts/trading/portfolio_manager.py`) after confirming the ingest root is `/sep/trader`.
- If `analyze_blast_radius` reports “file not found,” call `list_indexed_files` with a glob like `*portfolio_manager.py` to discover the exact indexed path, then retry with that value.
- Use `search_code` to confirm the module exists before blast analysis if the file name is uncertain.

## 5. Regex Queries
- `search_code` supports full Python Regular Expressions natively in RAM. Use it over Bash `grep`.

## 6. The Context Codebook
- Use `inject_fact` to persist architectural rules without wasting token windows. You can query these facts later seamlessly just like physical code files using `search_code`.

## 7. Advanced Refactoring Workflow
When asked to modify or improve code, you MUST follow this sequence using the Manifold tools:
1. **Discover:** Use `search_code` to find how similar features are implemented elsewhere in the repo before writing new logic.
2. **Assess:** Run `analyze_blast_radius` on the target file to understand dependencies before modifying it.
3. **Validate:** After writing new logic, run `verify_snippet` to ensure it structurally aligns with the codebase.
4. **Remember:** If the user provides a core architectural rule or trading thesis, use `inject_fact` to persist it in the Dynamic Semantic Codebook.
