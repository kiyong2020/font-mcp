"""Confluence Cloud 페이지를 받아 로컬에 저장 (오프라인 파싱용).

Setup (한 번만):
    1. https://id.atlassian.com/manage-profile/security/api-tokens 에서 토큰 발급
    2. 프로젝트 루트에 .env 파일 생성:
        ATLASSIAN_EMAIL=your@sandoll.com
        ATLASSIAN_TOKEN=your-api-token
       또는 셸에서 export. 셸 env 가 .env 보다 우선한다.

Usage:
    python scripts/fetch_confluence.py <page_id> [output_dir]
    python scripts/fetch_confluence.py 741998709
    python scripts/fetch_confluence.py 741998709 ./wiki_cache

결과:
    <output_dir>/<page_id>.json   — 전체 API 응답
    <output_dir>/<page_id>.html   — body.storage (XHTML, 파싱하기 좋음)
    <output_dir>/<page_id>.title  — 페이지 제목

자식 페이지까지 재귀적으로 받으려면 --recursive.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

DOMAIN = "sandoll.atlassian.net"


def _load_dotenv() -> None:
    """프로젝트 루트의 .env 파일에서 변수 로드. 셸 env 가 우선."""
    # scripts/fetch_confluence.py → 부모 디렉터리가 프로젝트 루트
    candidates = [
        Path.cwd() / ".env",
        Path(__file__).resolve().parent.parent / ".env",
    ]
    for path in candidates:
        if not path.exists():
            continue
        for raw in path.read_text().splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            os.environ.setdefault(key, value)
        return  # 첫 번째로 찾은 .env 만 사용


def _auth_header() -> str:
    _load_dotenv()
    email = os.environ.get("ATLASSIAN_EMAIL")
    token = os.environ.get("ATLASSIAN_TOKEN")
    if not email or not token:
        sys.exit(
            "error: ATLASSIAN_EMAIL and ATLASSIAN_TOKEN required.\n"
            "  set in .env at project root, or `export` in shell."
        )
    return "Basic " + base64.b64encode(f"{email}:{token}".encode()).decode()


def _get(url: str) -> dict:
    req = urllib.request.Request(url, headers={
        "Authorization": _auth_header(),
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        sys.exit(f"HTTP {e.code} on {url}\n{e.read().decode(errors='replace')[:500]}")


def fetch_page(page_id: str) -> dict:
    """API v2 로 시도, 실패 시 v1 폴백."""
    v2 = f"https://{DOMAIN}/wiki/api/v2/pages/{page_id}?body-format=storage"
    try:
        return _get(v2)
    except SystemExit:
        v1 = f"https://{DOMAIN}/wiki/rest/api/content/{page_id}?expand=body.storage"
        return _get(v1)


def fetch_children(page_id: str) -> list[dict]:
    url = f"https://{DOMAIN}/wiki/api/v2/pages/{page_id}/children?limit=250"
    data = _get(url)
    return data.get("results", [])


def _body(data: dict) -> str:
    """v1/v2 응답 모두에서 body 추출."""
    body = data.get("body", {})
    return (body.get("storage") or {}).get("value") or ""


def _title(data: dict) -> str:
    return data.get("title", "")


def save_page(page_id: str, out_dir: Path) -> dict:
    data = fetch_page(page_id)
    body = _body(data)
    title = _title(data)
    (out_dir / f"{page_id}.json").write_text(
        json.dumps(data, indent=2, ensure_ascii=False)
    )
    (out_dir / f"{page_id}.html").write_text(body)
    (out_dir / f"{page_id}.title").write_text(title)
    print(f"  [{page_id}] {title}  ({len(body):,} chars)")
    return data


def _fetch_tree(page_id: str, out_dir: Path, depth: int, current: int = 0) -> None:
    indent = "  " * current
    print(indent, end="")
    save_page(page_id, out_dir)
    if current >= depth:
        return
    children = fetch_children(page_id)
    for child in children:
        _fetch_tree(child["id"], out_dir, depth, current + 1)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("page_id")
    ap.add_argument("output_dir", nargs="?", default="./wiki_cache")
    ap.add_argument("--recursive", "-r", action="store_true",
                    help="자식 페이지까지 1단계 재귀 (--depth 1 과 동일)")
    ap.add_argument("--depth", "-d", type=int, default=0,
                    help="재귀 깊이 (0=현재 페이지만, 1=자식까지, 2=손자까지)")
    args = ap.parse_args()

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    depth = max(args.depth, 1 if args.recursive else 0)
    _fetch_tree(args.page_id, out_dir, depth)


if __name__ == "__main__":
    main()
