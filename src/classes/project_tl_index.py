"""Name of the project's shared media index."""

from __future__ import annotations


def build_project_index_name(project_id: str) -> str:
    """Must match desktop indexing in files_model / reindex handlers."""
    pid = str(project_id or "").strip()
    if not pid:
        return "zenvi-videos"
    return f"zenvi-{pid}"

