"""Font Tools MCP Server.

FastMCP 기반. stdio 트랜스포트로 Claude Desktop / Claude Code와 직접 연동.

설계 원칙:
- 원본 폰트는 절대 덮어쓰지 않는다. 모든 set_* / apply_* 는 output_path를 강제한다.
- 모든 tool은 JSON 직렬화 가능한 dict/list/scalar 만 반환한다.
- 예외는 잡아서 {"error": "..."} 로 반환 — 에이전트가 다음 액션을 정할 수 있게.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

import build_spec
import extract_spec
from font_builder import FontBuilder
from font_ops import FontOps
from memory import CaseMemory


load_dotenv()

_PROJECT_ROOT = Path(__file__).resolve().parent
_DATA_DIR = Path(
    os.getenv("FONT_MCP_DATA_DIR") or str(_PROJECT_ROOT / "data")
).expanduser()

mcp = FastMCP("font-tools")
ops = FontOps()
memory = CaseMemory(_DATA_DIR / "cases.json")
builder = FontBuilder(ops, memory)


# ──────────────────────────────────────────────────────────────────
# 진단 (read-only)
# ──────────────────────────────────────────────────────────────────

@mcp.tool()
def font_info(path: str) -> dict:
    """폰트 기본 정보 조회: 패밀리명·스타일·UPM·글리프 수·테이블 목록·가변 여부."""
    try:
        return ops.info(path)
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def list_tables(path: str) -> list[str] | dict:
    """폰트에 포함된 SFNT 테이블 태그 목록."""
    try:
        return ops.list_tables(path)
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def dump_table_ttx(
    path: str,
    table_tag: str,
    output_path: Optional[str] = None,
) -> dict:
    """특정 테이블을 TTX(XML)로 덤프.

    output_path 지정 시 해당 경로에 저장 후 경로 반환.
    미지정 시 XML 문자열을 결과에 포함하여 반환.
    """
    try:
        return ops.dump_table(path, table_tag, output_path)
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def get_name_records(path: str) -> dict:
    """name 테이블의 모든 레코드를 (nameID, platformID, platEncID, langID, string)로 반환."""
    try:
        return {"records": ops.get_name_records(path)}
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def get_vertical_metrics(path: str) -> dict:
    """OS/2 · hhea · vhea 수직 메트릭을 한 번에 조회. 디자이너 보고용 진단의 시작점."""
    try:
        return ops.vertical_metrics(path)
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def list_features(path: str) -> dict:
    """GSUB / GPOS 의 feature 태그 목록 (한글 합자, 커닝 등 적용 여부 진단)."""
    try:
        return ops.list_features(path)
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def diagnose(path: str) -> dict:
    """흔한 폰트 문제(수직 메트릭/이름/스타일 비트/cmap)를 한 번에 자동 검출.

    수정 사이클의 시작점. 반환된 issues[*].message 를 그대로
    find_similar_cases 에 넣으면 처방 매칭이 가능하다.
    """
    try:
        return ops.diagnose(path)
    except Exception as e:
        return {"error": str(e)}


# ──────────────────────────────────────────────────────────────────
# 수정 (write — output_path 필수)
# ──────────────────────────────────────────────────────────────────

@mcp.tool()
def apply_ttx_patch(
    font_path: str,
    ttx_patch_path: str,
    output_path: str,
) -> dict:
    """TTX XML 패치를 폰트에 병합하여 새 파일로 저장.

    가장 범용적인 수정 도구. 에이전트가 dump_table_ttx → 텍스트 편집 →
    apply_ttx_patch 흐름으로 거의 모든 테이블을 다룰 수 있다.
    """
    try:
        return ops.apply_ttx(font_path, ttx_patch_path, output_path)
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def set_name_record(
    font_path: str,
    output_path: str,
    name_id: int,
    string: str,
    platform_id: int = 3,
    plat_enc_id: int = 1,
    lang_id: int = 0x409,
) -> dict:
    """name 테이블 레코드 추가/갱신.

    기본값은 Windows / Unicode BMP / en-US. 한국어 레코드 추가 시
    platform_id=3, plat_enc_id=1, lang_id=0x412 사용.
    """
    try:
        return ops.set_name_record(
            font_path, output_path, name_id, string,
            platform_id, plat_enc_id, lang_id,
        )
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def set_vertical_metrics(
    font_path: str,
    output_path: str,
    ascender: Optional[int] = None,
    descender: Optional[int] = None,
    line_gap: Optional[int] = None,
    sync_all: bool = True,
) -> dict:
    """수직 메트릭 갱신.

    sync_all=True (기본): OS/2 sTypo*, hhea, OS/2 usWin* 를 모두 일치시키고
    OS/2.fsSelection 의 USE_TYPO_METRICS 비트를 설정. InDesign / Word /
    브라우저 간 행간 차이를 막는 가장 흔한 처방.
    """
    try:
        return ops.set_vertical_metrics(
            font_path, output_path,
            ascender, descender, line_gap, sync_all,
        )
    except Exception as e:
        return {"error": str(e)}


# ──────────────────────────────────────────────────────────────────
# 변환
# ──────────────────────────────────────────────────────────────────

@mcp.tool()
def subset_font(
    font_path: str,
    output_path: str,
    text: Optional[str] = None,
    unicodes: Optional[list[int]] = None,
    glyphs: Optional[list[str]] = None,
    layout_features: str = "*",
) -> dict:
    """글리프 서브셋 추출. text / unicodes / glyphs 중 하나는 필수."""
    try:
        return ops.subset_font(
            font_path, output_path, text, unicodes, glyphs, layout_features,
        )
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def merge_fonts(font_paths: list[str], output_path: str) -> dict:
    """여러 폰트 병합 (라틴 + 한글 등). 2개 이상 필요."""
    try:
        return ops.merge_fonts(font_paths, output_path)
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def convert_format(
    font_path: str,
    output_path: str,
    flavor: Optional[str] = None,
) -> dict:
    """WOFF/WOFF2 래핑 변경. flavor: None(sfnt) | 'woff' | 'woff2'."""
    try:
        return ops.convert_format(font_path, output_path, flavor)
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def instance_variable(
    font_path: str,
    output_path: str,
    axes: dict,
) -> dict:
    """가변 폰트를 정적 인스턴스로. axes 예: {"wght": 400, "wdth": 100}."""
    try:
        return ops.instance_variable(font_path, output_path, axes)
    except Exception as e:
        return {"error": str(e)}


# ──────────────────────────────────────────────────────────────────
# 시각 / 비교
# ──────────────────────────────────────────────────────────────────

@mcp.tool()
def render_sample(
    font_path: str,
    text: str,
    output_png: str,
    font_size: int = 48,
) -> dict:
    """샘플 텍스트를 PNG로 렌더 + cmap 미커버 문자 보고.

    fontbakery 가 못 잡는 시각 오류(글리프 누락)를 정량 검출.
    missing_ratio 가 0보다 크면 .notdef('두부')로 표시되는 문자가 있음.
    """
    try:
        return ops.render_sample(font_path, text, output_png, font_size)
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def diff_fonts(a: str, b: str) -> dict:
    """두 폰트의 핵심 필드 + name 레코드 차이를 비교.

    apply_ttx_patch / set_* 후 의도한 필드만 변경됐는지 회귀 확인용.
    """
    try:
        return ops.diff_fonts(a, b)
    except Exception as e:
        return {"error": str(e)}


# ──────────────────────────────────────────────────────────────────
# 검증
# ──────────────────────────────────────────────────────────────────

@mcp.tool()
def validate_font(path: str, profile: str = "opentype") -> dict:
    """fontbakery 검증 실행.

    profile: opentype | googlefonts | adobefonts | notofonts.
    수정 사이클의 끝에서 회귀 확인용으로 호출.
    """
    try:
        return ops.validate(path, profile)
    except Exception as e:
        return {"error": str(e)}


# ──────────────────────────────────────────────────────────────────
# 학습 메모리 (RAG)
# ──────────────────────────────────────────────────────────────────

@mcp.tool()
def record_case(
    symptom: str,
    diagnosis: str,
    patch_summary: str,
    table_tag: str,
    font_path: Optional[str] = None,
    validation_after: Optional[str] = None,
    patch_ttx: Optional[str] = None,
) -> dict:
    """수정 사례를 메모리에 저장.

    에이전트는 매 수정 사이클이 끝날 때마다 이 도구를 호출해야 한다.
    가능하면 validate_and_record 를 대신 써서 자동으로 호출되게 하라.

    validation_after: PASS / FAIL / WARN — fontbakery 결과
    patch_ttx: 재적용 가능한 TTX 본문 (20KB 캡)
    """
    return memory.add(
        symptom, diagnosis, patch_summary, table_tag, font_path,
        validation_after, patch_ttx,
    )


@mcp.tool()
def update_case_outcome(case_id: int, success: bool) -> dict:
    """과거 케이스를 다른 폰트에 재적용한 결과를 score 에 반영."""
    try:
        return memory.update_outcome(case_id, success)
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def find_similar_cases(symptom: str, k: int = 5) -> dict:
    """과거에 처리한 유사 사례를 검색해 반환 (ChromaDB 의미 검색).

    각 매치는 score(success - fail) 와 patch_ttx 를 포함해
    에이전트가 검증된 처방을 우선 적용할 수 있게 한다.
    """
    return {"matches": memory.search(symptom, k)}


@mcp.tool()
def validate_and_record(
    font_path: str,
    symptom: str,
    diagnosis: str,
    patch_summary: str,
    table_tag: str,
    patch_ttx: Optional[str] = None,
    profile: str = "opentype",
) -> dict:
    """검증 → 케이스 자동 저장. 수정 사이클의 마지막 한 줄.

    fontbakery 결과(PASS/FAIL/WARN)를 case 메타에 자동 첨부하므로
    에이전트가 record_case 를 까먹어도 학습 루프가 끊기지 않는다.
    """
    try:
        val = ops.validate(font_path, profile)
        counts = (val.get("summary") or {}).get("counts", {})
        if counts.get("FAIL", 0) or counts.get("ERROR", 0):
            verdict = "FAIL"
        elif counts.get("WARN", 0):
            verdict = "WARN"
        elif counts.get("PASS", 0):
            verdict = "PASS"
        else:
            verdict = ""
        case = memory.add(
            symptom=symptom,
            diagnosis=diagnosis,
            patch_summary=patch_summary,
            table_tag=table_tag,
            font_path=font_path,
            validation_after=verdict,
            patch_ttx=patch_ttx,
        )
        return {"validation": val, "case": case}
    except Exception as e:
        return {"error": str(e)}


# ──────────────────────────────────────────────────────────────────
# Prompts — 호출 패턴 표준화 (학습 데이터 일관성 ↑)
# ──────────────────────────────────────────────────────────────────

@mcp.prompt()
def diagnose_then_fix(font_path: str) -> str:
    """진단 → 처방 매칭 → 수정 → 검증 자동 워크플로."""
    return f"""폰트 {font_path} 를 다음 순서로 처리하세요:

1. diagnose('{font_path}') 호출 → issues 받기
2. 각 issue.message 를 find_similar_cases() 에 넣어 과거 처방 검색
   - score(success-fail) 가 양수인 매치를 우선 적용
   - patch_ttx 가 있으면 apply_ttx_patch 로 그대로 재사용 가능
3. 매칭이 없으면 issue.hint 에 따라 set_vertical_metrics / set_name_record / apply_ttx_patch
4. 새 파일 경로로 출력 (절대 원본 덮어쓰지 말 것)
5. validate_and_record(font_path=출력파일, symptom=issue.message, diagnosis=..., patch_summary=..., table_tag=issue.table, patch_ttx=...) 로 마무리"""


@mcp.prompt()
def fix_vertical_metrics(font_path: str) -> str:
    """행간 불일치(InDesign vs Word vs 브라우저) 수정 워크플로."""
    return f"""다음 순서:

1. find_similar_cases('vertical metrics line spacing inconsistency')
2. get_vertical_metrics('{font_path}') 로 현재값 확인
3. set_vertical_metrics(font_path='{font_path}', output_path=<새 경로>,
   ascender=..., descender=..., line_gap=..., sync_all=True)
   sync_all=True 가 OS/2 sTypo*, hhea, OS/2 usWin* 동기화 + USE_TYPO_METRICS 비트 설정
4. validate_and_record(font_path=<새 경로>, symptom='행간 불일치',
   diagnosis='OS/2 sTypo* 와 hhea/usWin* 가 어긋났음',
   patch_summary='set_vertical_metrics sync_all=True',
   table_tag='OS/2+hhea')"""


@mcp.prompt()
def add_korean_name_records(font_path: str, korean_family: str) -> str:
    """한국어 nameID 1/4/16/17 레코드 추가."""
    return f"""다음 4번 호출로 한국어 이름을 추가:

각각 platform_id=3, plat_enc_id=1, lang_id=0x412 (ko-KR) 로:
1. set_name_record(name_id=1, string='{korean_family}')
2. set_name_record(name_id=4, string='{korean_family}')   # full name
3. set_name_record(name_id=16, string='{korean_family}')  # typographic family
4. set_name_record(name_id=17, string='Regular' 등)        # typographic subfamily

각 단계마다 output_path 를 다음 단계의 font_path 로 사용 (체이닝).
마지막에 validate_and_record."""


# ──────────────────────────────────────────────────────────────────
# 작업서(XLSX) 기반 패밀리 빌드
# ──────────────────────────────────────────────────────────────────

@mcp.tool()
def parse_build_sheet(sheet_path: str) -> dict:
    """[제작의뢰서] 양식의 XLSX 작업서를 파싱.

    scripts/make_build_template.py 가 생성하는 6-시트 워크북 포맷을 따른다.
    구조: meta / metrics / weights / outputs / names / subset.

    반환:
        meta·metrics·weights·outputs·extra_names·subset 가 풀린 dict.
        오류 시 {"error": "..."}.
    """
    try:
        spec = build_spec.parse(sheet_path)
        d = spec.to_dict()
        # 프리셋 unicodes 는 매우 클 수 있어 카운트만 노출
        if d["subset"]["unicodes"]:
            cnt = len(d["subset"]["unicodes"])
            d["subset"]["unicodes"] = f"<list of {cnt} codepoints>"
        return d
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def build_font_family(
    sheet_path: str,
    output_dir: str,
    base_otf: Optional[dict[str, str]] = None,
    base_ttf: Optional[dict[str, str]] = None,
    variable_font: Optional[str] = None,
    validate: bool = True,
) -> dict:
    """작업서(XLSX) + 베이스 폰트들로 전체 패밀리 빌드.

    Args:
        sheet_path: parse_build_sheet 가 받는 XLSX 경로.
        output_dir: 산출물 루트. 하위에 OTF/ TTF/ WOFF2/ VF/ ... 디렉터리 생성.
        base_otf: {weight_class: path} — OTF 가 outputs 에 enabled 면 필수.
            예) {"400": "/abs/HCSSansTxRg.otf", "700": "/abs/HCSSansHdBd.otf"}
        base_ttf: 같은 구조의 TTF 베이스. TTF 가 enabled 면 필수.
        variable_font: VF 가 enabled 면 필수. 베이스 VF 의 절대 경로.
        validate: 각 OTF/TTF 산출물에 fontbakery 실행 여부.
            WOFF 류는 항상 검증 스킵 (fontbakery 지원 제한).

    Returns:
        {project, output_dir, results: [...], summary: {total, ok, errors, by_format}}
        각 결과는 weight_class/style_name/fmt/output_path/validation_verdict/case_id.
    """
    try:
        spec = build_spec.parse(sheet_path)
        return builder.build(
            spec=spec,
            base_otf=base_otf or {},
            base_ttf=base_ttf or {},
            variable_font=variable_font,
            output_dir=output_dir,
            validate=validate,
        )
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def extract_build_sheet(font_paths: list[str], output_xlsx: str) -> dict:
    """기존 폰트 패밀리에서 6-시트 작업서(XLSX) 자동 생성.

    parse_build_sheet 가 읽는 것과 동일한 스키마로, 입력 폰트들의 name 테이블·
    head·hhea·OS/2·post 값을 채워 넣는다. 디자이너는 결과 XLSX 에서 필요한
    필드만 검토·수정한 뒤 build_font_family 의 sheet_path 인자로 넘기면 된다.

    매핑:
        - meta      ← nameID 0/7/8/9(en+ko)/11/12/13/14 + head.fontRevision,
                       project_name = nameID 1, scope 는 ko-KR 레코드 유무로 추정
        - metrics   ← head.unitsPerEm, hhea, OS/2.sTypo*/usWin*/yStrikeout*/fsType,
                       post.underline*, USE_TYPO_METRICS 비트 (Regular=400 기준)
        - weights   ← 폰트별 한 행, usWeightClass 오름차순
        - outputs   ← 입력 확장자 기반 권장값 (.otf→OTF, .ttf→TTF, fvar→VF, WOFF2 기본)
        - names     ← auto-generated 외의 nameID (0/7/8/9/11~14 등) 만
        - subset    ← 기본값 (preset=common_kr)

    Args:
        font_paths: 패밀리에 속한 폰트들의 절대 경로 리스트 (1개 이상).
        output_xlsx: 결과 XLSX 의 절대 경로.
    """
    try:
        return extract_spec.extract(font_paths, output_xlsx)
    except Exception as e:
        return {"error": str(e)}


@mcp.prompt()
def build_from_sheet(sheet_path: str) -> str:
    """작업서(XLSX) → 폰트 패밀리 빌드 표준 워크플로."""
    return f"""다음 순서로 처리:

1. parse_build_sheet('{sheet_path}') 로 spec 미리보기
   - outputs.enabled 가 어떤 포맷인지, weights[*].weight_class 가 무엇인지 확인
2. 베이스 폰트 경로를 weight_class 별로 묶어 dict 작성:
   - base_otf={{"400": "/abs/...Rg.otf", "700": "/abs/...Bd.otf", ...}}
   - base_ttf={{...}}  (TTF 가 enabled 면)
   - variable_font="/abs/...VF.ttf"  (VF 가 enabled 면)
3. build_font_family(sheet_path='{sheet_path}', output_dir='/abs/out', base_otf=..., base_ttf=..., variable_font=...)
4. summary.errors > 0 이면 results 의 error 메시지 확인
5. PASS/WARN/FAIL 결과는 cases.json 에 자동 저장됨 — find_similar_cases 로 재활용 가능"""


if __name__ == "__main__":
    mcp.run()
