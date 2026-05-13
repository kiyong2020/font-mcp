"""사례(케이스) 저장소 — ChromaDB + built-in embeddings.

ChromaDB 기본 임베딩 함수(all-MiniLM-L6-v2)를 사용하므로
외부 API 키가 필요 없다. 첫 실행 시 모델을 자동 다운로드한다.

데이터는 db_path 의 부모 디렉터리 아래 chroma/ 폴더에 영속 저장된다.
    예) ~/.font-mcp/cases.json 을 넘기면 → ~/.font-mcp/chroma/

public API (server.py 와의 계약):
    add(symptom, diagnosis, patch_summary, table_tag, font_path,
        validation_after=None, patch_ttx=None) -> dict
    update_outcome(case_id, success) -> dict
    search(query, k) -> list[dict]
    all() -> list[dict]

케이스 메타데이터 필드:
    validation_after  PASS | FAIL | WARN | ""   — 적용 후 fontbakery 결과
    patch_ttx         원본 TTX 패치 (재적용용, 20KB 캡)
    success_count     같은 케이스를 재시도해 성공한 횟수
    fail_count        실패한 횟수
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Optional

import chromadb


# 메타데이터에 들어가는 TTX 본문은 너무 커지면 검색 성능에 악영향 — 소프트 캡.
_PATCH_TTX_CAP = 20_000


class CaseMemory:
    def __init__(self, db_path: Path):
        chroma_dir = Path(db_path).parent / "chroma"
        chroma_dir.mkdir(parents=True, exist_ok=True)
        self._client = chromadb.PersistentClient(path=str(chroma_dir))
        # Default embedding function: all-MiniLM-L6-v2 (local, no API key)
        self._col = self._client.get_or_create_collection("cases")

    # ── 내부 ─────────────────────────────────────────────────────

    def _next_id(self) -> str:
        return str(self._col.count() + 1)

    @staticmethod
    def _to_case(doc: str, meta: dict) -> dict:
        success = int(meta.get("success_count", 0))
        fail = int(meta.get("fail_count", 0))
        return {
            "id": meta["id"],
            "ts": meta["ts"],
            "symptom": doc,
            "diagnosis": meta["diagnosis"],
            "patch_summary": meta["patch_summary"],
            "table_tag": meta["table_tag"],
            "font_path": meta["font_path"] or None,
            "validation_after": meta.get("validation_after") or None,
            "patch_ttx": meta.get("patch_ttx") or None,
            "success_count": success,
            "fail_count": fail,
            "score": success - fail,
        }

    # ── 공개 API ─────────────────────────────────────────────────

    def add(
        self,
        symptom: str,
        diagnosis: str,
        patch_summary: str,
        table_tag: str,
        font_path: Optional[str] = None,
        validation_after: Optional[str] = None,
        patch_ttx: Optional[str] = None,
    ) -> dict:
        case_id = self._next_id()
        ts = datetime.now().isoformat(timespec="seconds")

        if patch_ttx and len(patch_ttx) > _PATCH_TTX_CAP:
            patch_ttx = (
                patch_ttx[:_PATCH_TTX_CAP]
                + f"\n<!-- truncated, original length {len(patch_ttx)} -->"
            )

        meta = {
            "id": int(case_id),
            "ts": ts,
            "diagnosis": diagnosis,
            "patch_summary": patch_summary,
            "table_tag": table_tag,
            "font_path": font_path or "",  # Chroma metadata cannot be None
            "validation_after": (validation_after or "").upper(),
            "patch_ttx": patch_ttx or "",
            "success_count": 0,
            "fail_count": 0,
        }
        self._col.add(
            documents=[symptom],
            metadatas=[meta],
            ids=[case_id],
        )
        return self._to_case(symptom, meta)

    def update_outcome(self, case_id: str | int, success: bool) -> dict:
        """과거 케이스를 재적용한 결과를 score 에 반영."""
        cid = str(case_id)
        result = self._col.get(ids=[cid], include=["documents", "metadatas"])
        if not result["ids"]:
            return {"error": f"Case {cid} not found"}

        meta = dict(result["metadatas"][0])
        if success:
            meta["success_count"] = int(meta.get("success_count", 0)) + 1
        else:
            meta["fail_count"] = int(meta.get("fail_count", 0)) + 1
        self._col.update(ids=[cid], metadatas=[meta])
        return self._to_case(result["documents"][0], meta)

    def search(self, query: str, k: int = 5) -> list[dict]:
        count = self._col.count()
        if count == 0:
            return []
        results = self._col.query(
            query_texts=[query],
            n_results=min(k, count),
            include=["documents", "metadatas"],
        )
        return [
            self._to_case(doc, meta)
            for doc, meta in zip(results["documents"][0], results["metadatas"][0])
        ]

    def all(self) -> list[dict]:
        results = self._col.get(include=["documents", "metadatas"])
        return [
            self._to_case(doc, meta)
            for doc, meta in zip(results["documents"], results["metadatas"])
        ]
