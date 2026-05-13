# Font Tools MCP — 설계 문서

OTF/TTF 폰트를 AI 에이전트(Claude 등)가 대화형으로 진단·수정·검증하기 위한 MCP 서버 1차 스켈레톤.

## 설계 원칙

1. **모든 수정은 새 파일로 출력한다.** 원본을 절대 덮어쓰지 않는다 (디자이너 워크플로우 보호).
2. **Tool은 작고 명확하게.** "테이블 단위" 또는 "메트릭 단위"로 쪼갠다. 거대한 만능 도구를 만들지 않는다.
3. **읽기/쓰기 분리.** `get_*` / `list_*` 류는 사이드이펙트 없음. `set_*` / `apply_*` 는 반드시 `output_path` 파라미터 강제.
4. **구조화된 반환.** 에이전트가 다음 액션을 결정할 수 있도록 항상 JSON dict/list 반환. 자유 텍스트 X.
5. **검증을 도구화.** `validate_font`를 모든 수정 사이클의 끝에 두어 에이전트가 회귀를 자동 감지하게 한다.

## Tool 카탈로그

### 진단 (read-only)

| 도구 | 용도 |
| --- | --- |
| `font_info` | 패밀리·스타일·UPM·테이블 목록·가변 폰트 여부 |
| `list_tables` | 포함된 SFNT 테이블 태그 |
| `dump_table_ttx` | 특정 테이블을 TTX(XML)로 덤프 |
| `get_name_records` | name 테이블 전체 레코드 |
| `get_vertical_metrics` | OS/2 + hhea + vhea 수직 메트릭 한 번에 |
| `list_features` | GSUB/GPOS feature 태그 목록 |

### 수정 (write, output_path 필수)

| 도구 | 용도 |
| --- | --- |
| `apply_ttx_patch` | TTX XML 패치를 병합 (가장 범용적) |
| `set_name_record` | name 테이블 레코드 추가/갱신 |
| `set_vertical_metrics` | 수직 메트릭 변경 + sync_all 옵션으로 OS/2·hhea·vhea 일치화 |

### 검증

| 도구 | 용도 |
| --- | --- |
| `validate_font` | fontbakery 실행, 결과를 JSON으로 반환 |

### 학습 메모리 (RAG의 단순 버전)

| 도구 | 용도 |
| --- | --- |
| `record_case` | (증상, 진단, 패치 요약, 테이블) 사례 저장 |
| `find_similar_cases` | 키워드 매칭으로 유사 사례 검색 |

> 메모리는 1단계에서는 JSON 파일. 운영에 들어가면 `memory.py`의 `search()`만 임베딩 기반(Chroma/Qdrant)으로 교체하면 된다.

## 전형적인 대화 흐름

```
사용자: "Glyphs에서 뽑은 NotoSansKR-Bold.otf인데 InDesign에서 윗줄 간격이
        너무 뜬다. 고쳐줘."

에이전트:
 1) find_similar_cases("InDesign 행간 너무 큼")
    → 과거 사례: "OS/2 usWinAscent/Descent가 hhea와 다름" 발견
 2) get_vertical_metrics("...Bold.otf")
    → 실제로 usWinAscent=2400, hhea.ascent=1900 불일치 확인
 3) set_vertical_metrics(
        font_path=..., output_path="..._fixed.otf",
        ascender=1900, descender=-500, sync_all=True)
 4) validate_font("..._fixed.otf")
    → fontbakery 통과
 5) record_case(
        symptom="InDesign 행간 과도",
        diagnosis="OS/2 usWin* 와 hhea 불일치",
        patch_summary="sync_all로 메트릭 통일, USE_TYPO_METRICS 비트 set",
        table_tag="OS/2,hhea")
```

## 향후 확장 지점

- `subset_font` (pyftsubset 래퍼) — 한자 영역 분리
- `enable_dsig` — Windows 서명 자리 확보
- `add_cmap_entry` / `remove_cmap_entry` — PUA·이모지 매핑
- `set_panose` / `set_unicode_range` — OS/2 분류 비트
- `compare_fonts` — 두 버전 diff (회귀 추적)
- `render_preview` — HarfBuzz로 셰이핑 후 PNG 생성 (시각 회귀)

## 파일 구성

```
font-mcp/
├── README.md                         # 이 문서
├── server.py                         # FastMCP 진입점, tool 등록
├── font_ops.py                       # fontTools 래퍼 (실제 동작)
├── memory.py                         # 사례 저장소 (JSON → 추후 벡터 DB)
├── requirements.txt
└── claude_desktop_config.example.json
```

## 설치 & 실행

```bash
cd font-mcp
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 단독 동작 확인
python server.py
```

Claude Desktop에 등록하려면 `claude_desktop_config.example.json` 참조.
