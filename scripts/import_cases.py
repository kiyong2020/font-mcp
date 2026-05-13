"""Confluence 캐시(wiki_cache/*.html) → ChromaDB 케이스 일괄 임포트.

scripts/fetch_confluence.py 로 받은 페이지들을 파싱해서
memory.CaseMemory.add() 로 등록한다.

Usage:
    python scripts/import_cases.py                  # 모든 페이지 임포트
    python scripts/import_cases.py --dry-run        # 미리보기만 (DB 변경 없음)
    python scripts/import_cases.py --cache wiki_cache --limit 5

페이지 구조 (Confluence 폰트제작이슈 템플릿 기준):
    <상태 패널> 해당 이슈는 해결되었습니다 / 해결되지 않았습니다 / 해결할 수 없습니다
    <h2>개요</h2>  (메타데이터 표 — 무시)
    <h2>이슈</h2>  → symptom
    <h2>원인</h2>  → diagnosis
    <h2>해결</h2>  → patch_summary
    (<h2>배경</h2>, <h2>참고</h2> 등은 보조)
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# 프로젝트 루트를 import path 에 추가 (memory.py 임포트용)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bs4 import BeautifulSoup

from memory import CaseMemory


# 본 페이지 자신 + 8개 카테고리 페이지 — 스킵
SKIP_IDS = {
    "741998709",   # 루트 (템플릿 가이드)
    "2889187364",  # Adobe
    "2888958008",  # MSOffice
    "2889384052",  # OS
    "2888958052",  # Glyphs
    "2888990834",  # 테이블/언어/피쳐/커닝
    "2353430605",  # 배리어블
    "2644017469",  # 컬러
    "2889384119",  # 기타
}

# 텍스트에서 언급되는 폰트 테이블 키워드 → 정규화된 태그
_TABLE_KEYWORDS = {
    "cmap": "cmap",
    "OS/2": "OS/2", "os/2": "OS/2", "fsSelection": "OS/2", "usWin": "OS/2",
    "sTypo": "OS/2", "WeightClass": "OS/2", "weightclass": "OS/2",
    "hhea": "hhea", "vhea": "vhea",
    "name": "name", "nameID": "name", "포스트스크립트": "name", "패밀리 네임": "name",
    "head": "head", "macStyle": "head",
    "GSUB": "GSUB", "피쳐": "GSUB", "ligature": "GSUB", "리거쳐": "GSUB",
    "GPOS": "GPOS", "커닝": "GPOS", "kern": "GPOS",
    "fvar": "fvar", "avar": "avar", "배리어블": "fvar", "Variable": "fvar",
    "fpgm": "fpgm", "prep": "prep", "post": "post",
    "BASE": "BASE", "icft": "BASE", "icfb": "BASE",
    "CFF": "CFF",
    "COLR": "COLR", "컬러폰트": "COLR", "컬러 폰트": "COLR",
}

_STATUS_PATTERNS = [
    (re.compile(r"해결.{0,3}되었습니다"), "PASS"),
    (re.compile(r"해결.{0,3}되지\s*않았습니다"), "FAIL"),
    (re.compile(r"해결할\s*수\s*없습니다"), "FAIL"),
    (re.compile(r"\(작성중\)"), "WIP"),
]


def detect_status(soup: BeautifulSoup, title: str) -> str:
    if "(작성중)" in title:
        return "WIP"
    # 상단 note macro 의 텍스트만 본다
    note = soup.find("ac:structured-macro")
    sample = note.get_text(" ", strip=True) if note else soup.get_text(" ", strip=True)[:200]
    for pat, verdict in _STATUS_PATTERNS:
        if pat.search(sample):
            return verdict
    return ""


def detect_tables(text: str) -> list[str]:
    hits: list[str] = []
    for kw, tag in _TABLE_KEYWORDS.items():
        if kw in text and tag not in hits:
            hits.append(tag)
    return hits


def extract_section(soup: BeautifulSoup, heading_text: str) -> str:
    """<h2>제목</h2> 다음 형제들을 다음 <h2> 직전까지 모은 텍스트."""
    for h2 in soup.find_all(["h2", "h3"]):
        if h2.get_text(strip=True) == heading_text:
            parts: list[str] = []
            for sib in h2.next_siblings:
                if getattr(sib, "name", None) in ("h2", "h3"):
                    break
                if hasattr(sib, "get_text"):
                    parts.append(sib.get_text(" ", strip=True))
                else:
                    parts.append(str(sib).strip())
            return "\n".join(p for p in parts if p).strip()
    return ""


def build_case(html: str, title: str, page_id: str) -> dict | None:
    """페이지 HTML → 케이스 dict. 추출할 게 없으면 None."""
    soup = BeautifulSoup(html, "html.parser")

    issue = extract_section(soup, "이슈") or extract_section(soup, "현상")
    cause = extract_section(soup, "원인") or extract_section(soup, "분석")
    fix = extract_section(soup, "해결") or extract_section(soup, "해결방법") or extract_section(soup, "조치")

    # 셋 다 비어있으면 의미 있는 케이스가 아님
    if not (issue or cause or fix):
        return None

    status = detect_status(soup, title)
    body_text = f"{title}\n{issue}\n{cause}\n{fix}"
    tables = detect_tables(body_text)
    table_tag = "+".join(tables) if tables else "unknown"

    symptom = f"{title}\n\n{issue}".strip() if issue else title
    return {
        "symptom": symptom,
        "diagnosis": cause or "(원인 미기재)",
        "patch_summary": fix or "(해결방법 미기재)",
        "table_tag": table_tag,
        "validation_after": status,
        "page_id": page_id,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", default="./wiki_cache")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=0, help="0=전체")
    args = ap.parse_args()

    cache_dir = Path(args.cache)
    html_files = sorted(cache_dir.glob("*.html"))
    html_files = [p for p in html_files if p.stem not in SKIP_IDS]
    if args.limit:
        html_files = html_files[: args.limit]

    print(f"대상: {len(html_files)} 페이지")

    if not args.dry_run:
        memory = CaseMemory(Path.home() / ".font-mcp" / "cases.json")

    stats = {"imported": 0, "skipped": 0, "failed": 0}
    for html_path in html_files:
        page_id = html_path.stem
        title_path = cache_dir / f"{page_id}.title"
        title = title_path.read_text().strip() if title_path.exists() else page_id

        try:
            html = html_path.read_text()
            case = build_case(html, title, page_id)
        except Exception as e:
            print(f"  [FAIL] {page_id} {title}: {e}")
            stats["failed"] += 1
            continue

        if case is None:
            print(f"  [SKIP] {page_id} {title}  (이슈/원인/해결 섹션 없음)")
            stats["skipped"] += 1
            continue

        print(f"  [{case['validation_after'] or '?':4}] {page_id} {title}")
        print(f"         tables={case['table_tag']}")

        if not args.dry_run:
            memory.add(
                symptom=case["symptom"],
                diagnosis=case["diagnosis"],
                patch_summary=case["patch_summary"],
                table_tag=case["table_tag"],
                font_path=f"confluence://{page_id}",
                validation_after=case["validation_after"] or None,
                patch_ttx=None,
            )
        stats["imported"] += 1

    print()
    print(f"임포트 {stats['imported']}건, 건너뜀 {stats['skipped']}건, 실패 {stats['failed']}건")
    if args.dry_run:
        print("(--dry-run 모드 — DB 변경 없음)")


if __name__ == "__main__":
    main()
