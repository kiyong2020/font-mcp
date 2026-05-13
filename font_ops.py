"""Font 조작 핵심 로직 (fontTools 래퍼).

server.py 와 분리한 이유:
- MCP 의존성 없이 단위 테스트 가능
- 추후 CLI / 다른 인터페이스에서 재사용
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from io import StringIO
from pathlib import Path
from typing import Optional

from fontTools.misc.xmlWriter import XMLWriter
from fontTools.ttLib import TTFont


# OS/2.fsSelection 의 USE_TYPO_METRICS 비트 (Windows/Mac 행간 일관성)
USE_TYPO_METRICS = 1 << 7

# fsSelection
_OS2_ITALIC = 1 << 0
_OS2_BOLD = 1 << 5
# head.macStyle
_HEAD_BOLD = 1 << 0
_HEAD_ITALIC = 1 << 1

_REQUIRED_NAME_IDS = {1: "family", 2: "subfamily", 4: "fullName", 6: "postScriptName"}
_INVALID_PS_CHARS = set("[](){}<>/%\x00 ")


class FontOps:
    """fontTools 작업의 얇은 래퍼. 모든 메서드는 JSON 직렬화 가능한 값만 반환."""

    # ── 진단 ─────────────────────────────────────────────────────

    def info(self, path: str) -> dict:
        font = TTFont(path)
        name = font["name"]
        head = font["head"]
        return {
            "path": str(path),
            "family": name.getBestFamilyName(),
            "subfamily": name.getBestSubFamilyName(),
            "full_name": name.getBestFullName(),
            "version": str(head.fontRevision),
            "units_per_em": head.unitsPerEm,
            "num_glyphs": font["maxp"].numGlyphs,
            "tables": sorted(font.keys()),
            "is_variable": "fvar" in font,
            "flavor": font.flavor,  # None | 'woff' | 'woff2'
        }

    def list_tables(self, path: str) -> list[str]:
        return sorted(TTFont(path).keys())

    def dump_table(
        self,
        path: str,
        table_tag: str,
        output_path: Optional[str],
    ) -> dict:
        font = TTFont(path)
        if table_tag not in font:
            raise ValueError(
                f"Table '{table_tag}' not found. Available: {sorted(font.keys())}"
            )

        if output_path:
            font.saveXML(output_path, tables=[table_tag])
            return {"output_path": output_path, "table_tag": table_tag}

        # XML 문자열로 반환
        buf = StringIO()
        writer = XMLWriter(buf)
        writer.begintag("ttFont")
        writer.newline()
        font[table_tag].toXML(writer, font)
        writer.endtag("ttFont")
        writer.newline()
        return {"table_tag": table_tag, "ttx": buf.getvalue()}

    def get_name_records(self, path: str) -> list[dict]:
        font = TTFont(path)
        return [
            {
                "nameID": r.nameID,
                "platformID": r.platformID,
                "platEncID": r.platEncID,
                "langID": r.langID,
                "string": r.toUnicode(),
            }
            for r in font["name"].names
        ]

    def vertical_metrics(self, path: str) -> dict:
        font = TTFont(path)
        os2 = font["OS/2"]
        hhea = font["hhea"]
        result = {
            "OS/2": {
                "sTypoAscender": os2.sTypoAscender,
                "sTypoDescender": os2.sTypoDescender,
                "sTypoLineGap": os2.sTypoLineGap,
                "usWinAscent": os2.usWinAscent,
                "usWinDescent": os2.usWinDescent,
                "fsSelection": os2.fsSelection,
                "use_typo_metrics": bool(os2.fsSelection & USE_TYPO_METRICS),
            },
            "hhea": {
                "ascent": hhea.ascent,
                "descent": hhea.descent,
                "lineGap": hhea.lineGap,
            },
        }
        if "vhea" in font:
            vhea = font["vhea"]
            result["vhea"] = {
                "ascent": getattr(vhea, "ascent", getattr(vhea, "ascender", None)),
                "descent": getattr(vhea, "descent", getattr(vhea, "descender", None)),
                "lineGap": vhea.lineGap,
            }
        return result

    def list_features(self, path: str) -> dict:
        font = TTFont(path)
        result: dict = {}
        for tag in ("GSUB", "GPOS"):
            if tag in font:
                table = font[tag].table
                if table is None or table.FeatureList is None:
                    result[tag] = []
                    continue
                features = sorted({
                    fr.FeatureTag for fr in table.FeatureList.FeatureRecord
                })
                result[tag] = features
        return result

    # ── 통합 진단 ────────────────────────────────────────────────

    def diagnose(self, path: str) -> dict:
        """흔한 폰트 문제를 한 번에 검사. {issues, summary} 반환.

        에이전트는 결과의 issues[*].message 를 그대로 find_similar_cases 에
        넣어 처방을 매칭할 수 있다.
        """
        font = TTFont(path)
        issues: list[dict] = []
        issues.extend(self._diag_vertical_metrics(font))
        issues.extend(self._diag_name(font))
        issues.extend(self._diag_style_bits(font))
        issues.extend(self._diag_cmap(font))

        by_severity: dict[str, int] = {}
        for issue in issues:
            sev = issue["severity"]
            by_severity[sev] = by_severity.get(sev, 0) + 1

        return {
            "path": str(path),
            "issues": issues,
            "summary": {"total": len(issues), "by_severity": by_severity},
        }

    @staticmethod
    def _diag_vertical_metrics(font: TTFont) -> list[dict]:
        if "OS/2" not in font or "hhea" not in font or "head" not in font:
            return []
        os2 = font["OS/2"]
        hhea = font["hhea"]
        upm = font["head"].unitsPerEm
        out: list[dict] = []

        if not (os2.fsSelection & USE_TYPO_METRICS):
            out.append({
                "severity": "warn",
                "code": "USE_TYPO_METRICS_OFF",
                "table": "OS/2",
                "message": "USE_TYPO_METRICS (fsSelection bit 7) is off — apps may use usWin* causing line-height inconsistency.",
                "hint": "Call set_vertical_metrics(sync_all=True) to enable.",
            })

        if os2.sTypoAscender != hhea.ascent:
            out.append({
                "severity": "warn",
                "code": "ASCENT_MISMATCH",
                "table": "OS/2 vs hhea",
                "message": f"OS/2.sTypoAscender ({os2.sTypoAscender}) != hhea.ascent ({hhea.ascent}).",
                "hint": "Set both via set_vertical_metrics(ascender=..., sync_all=True).",
            })

        if os2.sTypoDescender != hhea.descent:
            out.append({
                "severity": "warn",
                "code": "DESCENT_MISMATCH",
                "table": "OS/2 vs hhea",
                "message": f"OS/2.sTypoDescender ({os2.sTypoDescender}) != hhea.descent ({hhea.descent}).",
                "hint": "Set both via set_vertical_metrics(descender=..., sync_all=True).",
            })

        if os2.usWinAscent != abs(os2.sTypoAscender):
            out.append({
                "severity": "info",
                "code": "USWIN_ASCENT_MISMATCH",
                "table": "OS/2",
                "message": f"usWinAscent ({os2.usWinAscent}) != |sTypoAscender| ({abs(os2.sTypoAscender)}).",
                "hint": "Acceptable when USE_TYPO_METRICS is on (controls clipping); otherwise sync.",
            })

        typo_height = os2.sTypoAscender - os2.sTypoDescender
        if typo_height != upm:
            out.append({
                "severity": "info",
                "code": "TYPO_HEIGHT_NEQ_UPM",
                "table": "OS/2",
                "message": f"sTypoAscender - sTypoDescender ({typo_height}) != unitsPerEm ({upm}).",
                "hint": "Often intentional for CJK; flagged for awareness.",
            })

        return out

    @staticmethod
    def _diag_name(font: TTFont) -> list[dict]:
        if "name" not in font:
            return [{
                "severity": "fail",
                "code": "NO_NAME_TABLE",
                "table": "name",
                "message": "Font has no name table.",
                "hint": "This font is malformed.",
            }]

        names = font["name"].names
        win_ids = {r.nameID for r in names if r.platformID == 3}
        out: list[dict] = []

        for nid, label in _REQUIRED_NAME_IDS.items():
            if nid not in win_ids:
                out.append({
                    "severity": "fail",
                    "code": f"MISSING_NAME_{nid}",
                    "table": "name",
                    "message": f"Missing nameID {nid} ({label}) on Windows platform (3,1,0x409).",
                    "hint": f"Call set_name_record(name_id={nid}, string=...).",
                })

        ps_record = next(
            (r for r in names if r.nameID == 6 and r.platformID == 3), None
        )
        if ps_record is not None:
            ps = ps_record.toUnicode()
            invalid = sorted(set(ps) & _INVALID_PS_CHARS)
            if invalid:
                out.append({
                    "severity": "fail",
                    "code": "INVALID_POSTSCRIPT_NAME",
                    "table": "name",
                    "message": f"PostScript name {ps!r} contains invalid chars: {invalid}.",
                    "hint": "Replace with [A-Za-z0-9-] only via set_name_record(name_id=6, ...).",
                })

        return out

    @staticmethod
    def _diag_style_bits(font: TTFont) -> list[dict]:
        if "OS/2" not in font or "head" not in font:
            return []
        os2 = font["OS/2"]
        head = font["head"]
        out: list[dict] = []

        os2_bold = bool(os2.fsSelection & _OS2_BOLD)
        head_bold = bool(head.macStyle & _HEAD_BOLD)
        if os2_bold != head_bold:
            out.append({
                "severity": "warn",
                "code": "BOLD_BIT_MISMATCH",
                "table": "OS/2 vs head",
                "message": f"fsSelection.BOLD={os2_bold} != macStyle.BOLD={head_bold}.",
                "hint": "These bits must agree. Patch via apply_ttx_patch on OS/2 or head.",
            })

        os2_italic = bool(os2.fsSelection & _OS2_ITALIC)
        head_italic = bool(head.macStyle & _HEAD_ITALIC)
        if os2_italic != head_italic:
            out.append({
                "severity": "warn",
                "code": "ITALIC_BIT_MISMATCH",
                "table": "OS/2 vs head",
                "message": f"fsSelection.ITALIC={os2_italic} != macStyle.ITALIC={head_italic}.",
                "hint": "These bits must agree. Patch via apply_ttx_patch on OS/2 or head.",
            })

        return out

    @staticmethod
    def _diag_cmap(font: TTFont) -> list[dict]:
        if "cmap" not in font:
            return [{
                "severity": "fail",
                "code": "NO_CMAP",
                "table": "cmap",
                "message": "Font has no cmap table — unusable.",
            }]

        has_unicode = any(
            (st.platformID == 3 and st.platEncID in (1, 10)) or st.platformID == 0
            for st in font["cmap"].tables
        )
        if not has_unicode:
            return [{
                "severity": "fail",
                "code": "NO_UNICODE_CMAP",
                "table": "cmap",
                "message": "No Unicode cmap subtable (need platform 3 enc 1/10 or platform 0).",
                "hint": "Font is unusable in modern systems.",
            }]
        return []

    # ── 수정 ─────────────────────────────────────────────────────

    def apply_ttx(
        self,
        font_path: str,
        ttx_patch_path: str,
        output_path: str,
    ) -> dict:
        font = TTFont(font_path)
        font.importXML(ttx_patch_path)
        font.save(output_path)
        return {
            "input": str(font_path),
            "patch": str(ttx_patch_path),
            "output": str(output_path),
            "status": "ok",
        }

    def set_name_record(
        self,
        font_path: str,
        output_path: str,
        name_id: int,
        string: str,
        platform_id: int = 3,
        plat_enc_id: int = 1,
        lang_id: int = 0x409,
    ) -> dict:
        font = TTFont(font_path)
        font["name"].setName(string, name_id, platform_id, plat_enc_id, lang_id)
        font.save(output_path)
        return {
            "output": str(output_path),
            "nameID": name_id,
            "value": string,
            "platform": (platform_id, plat_enc_id, lang_id),
        }

    def set_vertical_metrics(
        self,
        font_path: str,
        output_path: str,
        ascender: Optional[int] = None,
        descender: Optional[int] = None,
        line_gap: Optional[int] = None,
        sync_all: bool = True,
    ) -> dict:
        font = TTFont(font_path)
        os2 = font["OS/2"]
        hhea = font["hhea"]
        before = self.vertical_metrics(font_path)

        if ascender is not None:
            os2.sTypoAscender = ascender
            if sync_all:
                hhea.ascent = ascender
                os2.usWinAscent = abs(ascender)

        if descender is not None:
            os2.sTypoDescender = descender
            if sync_all:
                hhea.descent = descender
                os2.usWinDescent = abs(descender)

        if line_gap is not None:
            os2.sTypoLineGap = line_gap
            if sync_all:
                hhea.lineGap = line_gap

        if sync_all:
            os2.fsSelection |= USE_TYPO_METRICS

        font.save(output_path)
        after = self.vertical_metrics(output_path)
        return {
            "output": str(output_path),
            "synced": sync_all,
            "before": before,
            "after": after,
        }

    # ── 변환 ─────────────────────────────────────────────────────

    def subset_font(
        self,
        font_path: str,
        output_path: str,
        text: Optional[str] = None,
        unicodes: Optional[list[int]] = None,
        glyphs: Optional[list[str]] = None,
        layout_features: str = "*",
    ) -> dict:
        """글리프 서브셋팅. text / unicodes / glyphs 중 하나는 필수.

        layout_features: "*" 면 모든 GSUB/GPOS 유지, 아니면 ","구분 태그.
        """
        from fontTools import subset

        if not (text or unicodes or glyphs):
            raise ValueError("Specify one of: text, unicodes, glyphs")

        options = subset.Options()
        options.layout_features = (
            ["*"] if layout_features == "*" else layout_features.split(",")
        )
        options.notdef_outline = True
        options.recommended_glyphs = True
        options.name_IDs = ["*"]

        font = TTFont(font_path)
        before = font["maxp"].numGlyphs
        subsetter = subset.Subsetter(options=options)
        if text:
            subsetter.populate(text=text)
        elif unicodes:
            subsetter.populate(unicodes=unicodes)
        else:
            subsetter.populate(glyphs=glyphs)
        subsetter.subset(font)
        font.save(output_path)
        after = TTFont(output_path)["maxp"].numGlyphs
        return {
            "output": str(output_path),
            "glyphs_before": before,
            "glyphs_after": after,
        }

    def merge_fonts(self, font_paths: list[str], output_path: str) -> dict:
        """여러 폰트를 하나로 병합 (라틴 + 한글 등)."""
        from fontTools import merge

        if len(font_paths) < 2:
            raise ValueError("Need at least 2 fonts to merge")
        merger = merge.Merger()
        font = merger.merge(font_paths)
        font.save(output_path)
        return {
            "output": str(output_path),
            "inputs": list(font_paths),
            "num_glyphs": font["maxp"].numGlyphs,
        }

    def convert_format(
        self,
        font_path: str,
        output_path: str,
        flavor: Optional[str] = None,
    ) -> dict:
        """WOFF/WOFF2 래핑 변경. flavor: None(sfnt) | 'woff' | 'woff2'.

        TTF↔OTF outline 변환은 별개 문제로 다루지 않는다.
        """
        if flavor not in (None, "", "woff", "woff2"):
            raise ValueError("flavor must be None, 'woff', or 'woff2'")
        font = TTFont(font_path)
        font.flavor = flavor or None
        font.save(output_path)
        return {"output": str(output_path), "flavor": flavor or "sfnt"}

    def instance_variable(
        self,
        font_path: str,
        output_path: str,
        axes: dict[str, float],
    ) -> dict:
        """가변 폰트를 정적 인스턴스로 추출.

        axes: {"wght": 400, "wdth": 100} 등. 일부만 고정해 부분 인스턴스도 가능.
        """
        from fontTools.varLib import instancer

        font = TTFont(font_path)
        if "fvar" not in font:
            raise ValueError("Font is not variable (no fvar table)")
        instance = instancer.instantiateVariableFont(font, axes)
        instance.save(output_path)
        return {"output": str(output_path), "axes": axes}

    # ── 시각 검증 ────────────────────────────────────────────────

    def render_sample(
        self,
        font_path: str,
        text: str,
        output_png: str,
        font_size: int = 48,
    ) -> dict:
        """샘플 텍스트를 PNG로 렌더 + cmap 미커버 문자 보고.

        fontbakery 가 못 잡는 "한글 깨져 보임" 류 시각 오류 진단용.
        반환 missing_ratio 가 높으면 글리프 누락 의심.
        """
        try:
            from PIL import Image, ImageDraw, ImageFont
        except ImportError:
            return {"error": "Pillow not installed", "hint": "pip install Pillow"}

        font = TTFont(font_path)
        cmap = font.getBestCmap()
        missing = sorted({c for c in text if ord(c) not in cmap})

        pil_font = ImageFont.truetype(font_path, font_size)
        bbox = pil_font.getbbox(text)
        width = max(1, bbox[2] - bbox[0]) + 20
        height = max(1, bbox[3] - bbox[1]) + 20
        img = Image.new("RGB", (width, height), "white")
        ImageDraw.Draw(img).text((10, 10), text, font=pil_font, fill="black")
        img.save(output_png)

        text_len = len(text)
        return {
            "output": str(output_png),
            "text_length": text_len,
            "missing_chars": missing,
            "missing_count": len(missing),
            "missing_ratio": (len(missing) / text_len) if text_len else 0.0,
        }

    # ── 비교 ─────────────────────────────────────────────────────

    def diff_fonts(self, a: str, b: str) -> dict:
        """두 폰트의 핵심 필드 차이를 비교. 의도한 필드만 바뀌었는지 확인용."""
        fa, fb = TTFont(a), TTFont(b)

        def _snap(font: TTFont) -> dict:
            os2 = font["OS/2"]
            head = font["head"]
            hhea = font["hhea"]
            return {
                "num_glyphs": font["maxp"].numGlyphs,
                "tables": sorted(font.keys()),
                "fontRevision": str(head.fontRevision),
                "macStyle": head.macStyle,
                "fsSelection": os2.fsSelection,
                "sTypoAscender": os2.sTypoAscender,
                "sTypoDescender": os2.sTypoDescender,
                "sTypoLineGap": os2.sTypoLineGap,
                "usWinAscent": os2.usWinAscent,
                "usWinDescent": os2.usWinDescent,
                "hhea_ascent": hhea.ascent,
                "hhea_descent": hhea.descent,
                "hhea_lineGap": hhea.lineGap,
            }

        sa, sb = _snap(fa), _snap(fb)
        diffs: dict[str, dict] = {}
        for key in sa:
            if sa[key] != sb[key]:
                diffs[key] = {"a": sa[key], "b": sb[key]}

        names_a = {(r.platformID, r.platEncID, r.langID, r.nameID): r.toUnicode()
                   for r in fa["name"].names}
        names_b = {(r.platformID, r.platEncID, r.langID, r.nameID): r.toUnicode()
                   for r in fb["name"].names}
        name_diffs: list[dict] = []
        for k in sorted(set(names_a) | set(names_b)):
            if names_a.get(k) != names_b.get(k):
                name_diffs.append({
                    "key": list(k),
                    "a": names_a.get(k),
                    "b": names_b.get(k),
                })

        return {
            "a": str(a),
            "b": str(b),
            "field_diffs": diffs,
            "name_diffs": name_diffs,
            "identical": not diffs and not name_diffs,
        }

    # ── 검증 ─────────────────────────────────────────────────────

    def validate(self, path: str, profile: str = "opentype") -> dict:
        """fontbakery 검증. 미설치 시 안내 dict 반환."""
        with tempfile.NamedTemporaryFile(
            suffix=".json", delete=False, mode="w"
        ) as tmp:
            json_out = tmp.name
        try:
            result = subprocess.run(
                [
                    "fontbakery",
                    f"check-{profile}",
                    str(path),
                    "--json", json_out,
                    "--no-progress",
                    "--succinct",
                ],
                capture_output=True, text=True, timeout=180,
            )
            json_path = Path(json_out)
            if json_path.exists() and json_path.stat().st_size > 0:
                report = json.loads(json_path.read_text())
                # 에이전트가 다음 액션을 결정하기 좋게 요약 추가
                summary = self._summarize_fontbakery(report)
                return {"summary": summary, "report": report}
            return {
                "error": "fontbakery did not produce a JSON report",
                "stdout": result.stdout[-2000:],
                "stderr": result.stderr[-2000:],
                "returncode": result.returncode,
            }
        except FileNotFoundError:
            return {
                "error": "fontbakery is not installed",
                "hint": "pip install fontbakery",
            }
        except subprocess.TimeoutExpired:
            return {"error": "fontbakery timed out (>180s)"}
        finally:
            Path(json_out).unlink(missing_ok=True)

    @staticmethod
    def _summarize_fontbakery(report: dict) -> dict:
        """fontbakery 리포트에서 PASS/FAIL/WARN 카운트를 뽑아낸다."""
        counts: dict[str, int] = {}
        failures: list[dict] = []
        for section in report.get("sections", []):
            for check in section.get("checks", []):
                status = check.get("result") or check.get("status") or "UNKNOWN"
                counts[status] = counts.get(status, 0) + 1
                if status in ("FAIL", "ERROR"):
                    failures.append({
                        "id": check.get("key") or check.get("id"),
                        "message": (check.get("logs") or [{}])[-1].get("message", "")[:300],
                    })
        return {"counts": counts, "failures": failures[:20]}
