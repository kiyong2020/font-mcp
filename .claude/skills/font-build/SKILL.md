---
description: Build a font from a base font file + Excel work spec (key-value layout). Applies vertical metrics and name records via the font-tools MCP server, validates, and records the case for learning.
---

# font-build

베이스 폰트 + 엑셀 명세서로 폰트 1개를 빌드한다.

사용자 호출: `/font-build <base_font> <spec.xlsx>` (순서 무관, 확장자로 구분).

## Input

`$ARGUMENTS` 에 **두 파일 경로**가 들어온다:
- `.ttf` / `.otf` / `.woff` / `.woff2` 중 하나 → **base_font**
- `.xlsx` / `.xls` → **spec**

순서가 뒤바뀌어 들어와도 확장자로 구별해 처리. 둘 중 하나라도 없으면 사용자에게 보충 요청.

## Workflow

### 1. 인자 분리 및 명세서 파싱

확장자로 두 경로를 식별한 뒤:

```bash
.venv/bin/python .claude/skills/font-build/parse_spec.py "<spec.xlsx 경로>"
```

결과는 `{"spec": {...}, "unknown_keys": [...], "sheet": "..."}` 형태.

- `unknown_keys` 가 비어있지 않으면 사용자에게 알리고 무시할지 확인.
- `spec.output_path` 가 없으면: 베이스 폰트와 같은 디렉토리에 `<family_en or 베이스파일명>_build.<원래 확장자>` 로 자동 생성하고 사용자에게 통지.
- `spec` 에 `source_font` 키가 있어도 **CLI 로 받은 base_font 가 우선** (충돌 시 사용자에게 알림).

### 2. 사전 진단 (선택)

베이스 폰트의 현재 상태를 파악:

```
diagnose(base_font)
```

이슈 요약을 사용자에게 보여주고 빌드를 계속할지 확인.

### 3. 수직 메트릭 적용 (있는 경우)

`spec` 에 `ascender`/`descender`/`line_gap` 중 하나라도 있으면:

```
set_vertical_metrics(
  font_path=base_font,
  output_path=<중간 경로, 예: output_path + ".tmp1">,
  ascender=spec.ascender,    # 있을 때만
  descender=spec.descender,  # 있을 때만
  line_gap=spec.line_gap,    # 있을 때만
  sync_all=True,
)
```

수직 메트릭이 명세에 없으면 이 단계는 건너뛰고, 다음 단계의 입력으로 `base_font` 를 그대로 사용.

### 4. 메타데이터 체이닝

다음 매핑으로 `set_name_record` 를 순차 호출. 각 호출의 `output_path` 가 다음 호출의 `font_path` 가 된다 (체이닝). 마지막 호출의 `output_path` 만 사용자가 지정한 최종 `spec.output_path`.

| spec 키 | name_id | langID | platform |
|---|---|---|---|
| `copyright` | 0 | 0x409 | 3,1 |
| `family_en` | 1 | 0x409 | 3,1 |
| `subfamily_en` | 2 | 0x409 | 3,1 |
| `full_name_en` | 4 | 0x409 | 3,1 |
| `version` | 5 | 0x409 | 3,1 |
| `postscript_name` | 6 | 0x409 | 3,1 |
| `manufacturer` | 8 | 0x409 | 3,1 |
| `designer` | 9 | 0x409 | 3,1 |
| `license` | 13 | 0x409 | 3,1 |
| `family_ko` | 1 | 0x412 | 3,1 |
| `subfamily_ko` | 2 | 0x412 | 3,1 |

명세에 없는 키는 건너뛴다. 중간 파일들은 같은 디렉토리에 `*.tmpN` 로 만들고, 빌드 성공 후 정리.

### 5. 검증 + 학습

마지막 출력 파일에 대해:

```
validate_and_record(
  font_path=spec.output_path,
  symptom=f"엑셀 명세 빌드: {spec.family_en or spec.family_ko}",
  diagnosis=f"수직 메트릭 + 메타데이터 {len(applied_keys)}개 적용",
  patch_summary=f"set_vertical_metrics + set_name_record (keys: {applied_keys})",
  table_tag="OS/2+hhea+name",
  patch_ttx=None,
)
```

PASS/FAIL/WARN 결과를 사용자에게 보고.

### 6. 정리 및 보고

- 중간 `*.tmpN` 파일 삭제
- 최종 결과 표:
  - 입력 파일
  - 출력 파일
  - 적용된 키 목록
  - fontbakery 결과 (PASS/WARN/FAIL 카운트)
  - case ID (재사용 가능)

## Error handling

- `parse_spec.py` 실패: 출력을 그대로 사용자에게 보여주고 종료.
- 어떤 단계든 `{"error": ...}` 반환 시: 중간 파일을 정리하고 무엇이 실패했는지 보고. 다음 단계로 진행하지 말 것.
- `openpyxl` 미설치: `pip install openpyxl` 안내.

## Notes

- 원본 폰트는 절대 덮어쓰지 않는다 (MCP 서버 자체 제약).
- `validate_and_record` 가 호출되면 케이스가 ChromaDB 에 자동 저장되므로 별도 `record_case` 불필요.
- 같은 명세를 다른 폰트에 재적용할 때는 `find_similar_cases` 로 과거 결과를 검색하고 `update_case_outcome(id, success=True/False)` 로 score 누적.
