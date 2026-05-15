"""작업서 기반 폰트 패밀리 빌더.

build_spec.BuildSpec + 베이스 폰트들 → output_dir 에 OTF/TTF/WOFF/WOFF2/VF 산출.

원본 폰트는 절대 덮어쓰지 않는다 (server.py 의 설계 원칙).
모든 산출물에 대해 fontbakery 검증 + CaseMemory 자동 기록.

각 weight 의 정적 폰트는 다음 순서로 패치된다:
    head.fontRevision      ← meta.version
    OS/2.usWeightClass     ← weight.weight_class
    OS/2.fsType            ← metrics.fs_type
    OS/2.sTypo* / hhea / OS/2.usWin*  ← metrics.* (use_typo_metrics 비트 포함)
    OS/2.yStrikeoutPosition/Size, post.underline*
    OS/2.fsSelection / head.macStyle  ← weight.is_bold / is_italic
    name 테이블 (1, 2, 3, 4, 5, 6, 16, 17 자동 + extra_names)
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from fontTools.ttLib import TTFont

from build_spec import BuildSpec, NameRecord, WeightSpec
from font_ops import USE_TYPO_METRICS, FontOps
from memory import CaseMemory


_FS_SEL_ITALIC = 1 << 0
_FS_SEL_BOLD = 1 << 5
_FS_SEL_REGULAR = 1 << 6
_HEAD_BOLD = 1 << 0
_HEAD_ITALIC = 1 << 1


# 한국어 표준 nameID 1/2/4 의 platformID=3 plat_enc_id=1 lang_id=0x412 조합.
_LANG_EN = 0x0409
_LANG_KO = 0x0412
_WIN_UNI = (3, 1)


@dataclass
class BuildOutputResult:
    weight_class: int
    style_name: str
    fmt: str
    output_path: str
    validation_verdict: str = ""   # PASS|WARN|FAIL|"" (validate=False 시 빈 문자열)
    case_id: Optional[int] = None
    error: Optional[str] = None


class FontBuilder:
    """spec + 베이스 폰트들을 받아 전체 패밀리를 빌드."""

    def __init__(self, ops: FontOps, memory: CaseMemory):
        self._ops = ops
        self._memory = memory

    # ── 외부 API ─────────────────────────────────────────────────

    def build(
        self,
        spec: BuildSpec,
        base_otf: Optional[dict[str, str]] = None,
        base_ttf: Optional[dict[str, str]] = None,
        variable_font: Optional[str] = None,
        output_dir: str = "./out",
        validate: bool = True,
    ) -> dict:
        """spec 의 모든 enabled output 을 빌드.

        base_otf / base_ttf : weight_class 문자열 → 베이스 폰트 절대 경로.
            예) {"400": "/abs/HCSSansTxRg.otf", "700": "/abs/HCSSansHdBd.otf"}
        variable_font : VF 출력이 enabled 면 필수.
        """
        base_otf = base_otf or {}
        base_ttf = base_ttf or {}
        out_root = Path(output_dir).expanduser().resolve()
        out_root.mkdir(parents=True, exist_ok=True)

        results: list[BuildOutputResult] = []
        enabled = set(spec.enabled_outputs())

        # ── 1) 정적 폰트: OTF / TTF ─────────────────────────────
        # 산출 마스터를 캐시해두면 WOFF/서브셋이 재처리하지 않아도 됨.
        masters_by_weight_format: dict[tuple[int, str], Path] = {}

        for w in spec.weights:
            for fmt, base_map, ext in (
                ("OTF", base_otf, ".otf"),
                ("TTF", base_ttf, ".ttf"),
            ):
                if fmt not in enabled:
                    continue
                base_path = base_map.get(str(w.weight_class))
                if not base_path:
                    results.append(BuildOutputResult(
                        weight_class=w.weight_class,
                        style_name=w.style_name,
                        fmt=fmt,
                        output_path="",
                        error=f"base_{fmt.lower()}['{w.weight_class}'] 가 없어 건너뜀",
                    ))
                    continue

                out_dir = out_root / fmt
                out_dir.mkdir(parents=True, exist_ok=True)
                psname = self._psname_with_suffix(w, spec, fmt)
                out_path = out_dir / f"{psname}{ext}"
                try:
                    self._build_static(base_path, w, spec, out_path, fmt)
                    masters_by_weight_format[(w.weight_class, fmt)] = out_path
                    r = self._finalize(
                        out_path, fmt, w,
                        validate=validate,
                        symptom=f"[{spec.meta.project_name}] {w.style_name} {fmt} 빌드",
                        diagnosis="작업서 메트릭 + name 레코드 일괄 적용",
                        patch_summary=f"build_font_family fmt={fmt} weight={w.weight_class}",
                        table_tag="OS/2+hhea+head+post+name",
                    )
                    results.append(r)
                except Exception as e:
                    results.append(BuildOutputResult(
                        weight_class=w.weight_class,
                        style_name=w.style_name,
                        fmt=fmt,
                        output_path=str(out_path),
                        error=str(e),
                    ))

        # ── 2) 웹폰트: WOFF / WOFF2 / WOFF*_subset ─────────────
        # 마스터 우선순위: TTF > OTF (현대 웹 관례)
        def pick_master(weight_class: int) -> Optional[tuple[Path, str]]:
            if (weight_class, "TTF") in masters_by_weight_format:
                return masters_by_weight_format[(weight_class, "TTF")], "TTF"
            if (weight_class, "OTF") in masters_by_weight_format:
                return masters_by_weight_format[(weight_class, "OTF")], "OTF"
            return None

        subset_unicodes: list[int] = []
        if any(f in enabled for f in ("WOFF_subset", "WOFF2_subset")):
            subset_unicodes = spec.subset_unicodes()

        for w in spec.weights:
            master = pick_master(w.weight_class)
            if not master:
                continue
            master_path, master_fmt = master

            for fmt, flavor, do_subset in (
                ("WOFF", "woff", False),
                ("WOFF2", "woff2", False),
                ("WOFF_subset", "woff", True),
                ("WOFF2_subset", "woff2", True),
            ):
                if fmt not in enabled:
                    continue
                out_dir = out_root / fmt
                out_dir.mkdir(parents=True, exist_ok=True)
                psname = self._psname_with_suffix(w, spec, "TTF")  # PS는 마스터 기준
                suffix = "-subset" if do_subset else ""
                out_path = out_dir / f"{psname}{suffix}.{flavor}"

                try:
                    if do_subset:
                        # 임시 서브셋 파일 (마스터를 보존하기 위해 별도 파일)
                        tmp_path = out_dir / f".{psname}{suffix}.tmp{master_path.suffix}"
                        self._ops.subset_font(
                            str(master_path), str(tmp_path),
                            unicodes=subset_unicodes,
                            layout_features=spec.subset.layout_features,
                        )
                        self._ops.convert_format(str(tmp_path), str(out_path), flavor)
                        tmp_path.unlink(missing_ok=True)
                    else:
                        self._ops.convert_format(
                            str(master_path), str(out_path), flavor,
                        )
                    r = self._finalize(
                        out_path, fmt, w,
                        validate=False,  # WOFF 는 fontbakery 가 지원 제한적
                        symptom=f"[{spec.meta.project_name}] {w.style_name} {fmt}",
                        diagnosis=f"{master_fmt} 마스터 → {flavor} 래핑"
                                  + (" + subset" if do_subset else ""),
                        patch_summary=f"build_font_family fmt={fmt}",
                        table_tag="convert_format",
                    )
                    results.append(r)
                except Exception as e:
                    results.append(BuildOutputResult(
                        weight_class=w.weight_class,
                        style_name=w.style_name,
                        fmt=fmt,
                        output_path=str(out_path),
                        error=str(e),
                    ))

        # ── 3) VF ───────────────────────────────────────────────
        if "VF" in enabled:
            if not variable_font:
                results.append(BuildOutputResult(
                    weight_class=0, style_name="VF", fmt="VF",
                    output_path="",
                    error="outputs.VF=TRUE 인데 variable_font 인자가 없음",
                ))
            else:
                vf_out_dir = out_root / "VF"
                vf_out_dir.mkdir(parents=True, exist_ok=True)
                # VF PSname: weights[0] 의 psname base + VF suffix 를 쓰지 않고
                # meta.project_name 기반 — 실제 의뢰서는 'HCSSansVF' 같은 단일 이름.
                # 가장 간단하게는 첫 weight 의 PSname 기반 prefix 추출.
                vf_psname = self._vf_psname(spec)
                vf_path = vf_out_dir / f"{vf_psname}.ttf"
                try:
                    self._build_vf(variable_font, spec, vf_path)
                    r = self._finalize(
                        vf_path, "VF",
                        None,
                        validate=True,
                        symptom=f"[{spec.meta.project_name}] VF 빌드",
                        diagnosis="VF 메트릭 + name 레코드 일괄 적용",
                        patch_summary="build_font_family fmt=VF",
                        table_tag="OS/2+hhea+head+post+name+fvar",
                    )
                    results.append(r)
                except Exception as e:
                    results.append(BuildOutputResult(
                        weight_class=0, style_name="VF", fmt="VF",
                        output_path=str(vf_path),
                        error=str(e),
                    ))

        return {
            "project": spec.meta.project_name,
            "output_dir": str(out_root),
            "results": [r.__dict__ for r in results],
            "summary": self._summarize(results),
        }

    # ── 내부: 한 weight 의 OTF/TTF 마스터 ────────────────────────

    def _build_static(
        self,
        base_path: str,
        w: WeightSpec,
        spec: BuildSpec,
        output_path: Path,
        fmt: str,
    ) -> None:
        font = TTFont(base_path)
        self._patch_head(font, spec)
        self._patch_os2(font, w, spec)
        self._patch_hhea(font, spec)
        self._patch_post(font, spec)
        self._patch_style_bits(font, w)
        self._patch_name(font, w, spec, fmt)
        font.save(str(output_path))

    def _build_vf(self, vf_base: str, spec: BuildSpec, output_path: Path) -> None:
        font = TTFont(vf_base)
        if "fvar" not in font:
            raise ValueError(f"{vf_base} 는 가변 폰트가 아님 (fvar 없음)")
        # VF 는 사이즈 메타와 name 만 패치. weight_class 는 인스턴스마다 다르므로 건드리지 않음.
        self._patch_head(font, spec)
        self._patch_hhea(font, spec)
        self._patch_post(font, spec)
        # OS/2 의 vendor-wide 필드만 패치 (usWeight* 는 VF 가 자체 관리)
        os2 = font["OS/2"]
        os2.sTypoAscender = spec.metrics.typo_ascender
        os2.sTypoDescender = spec.metrics.typo_descender
        os2.sTypoLineGap = spec.metrics.typo_line_gap
        os2.usWinAscent = spec.metrics.win_ascent
        os2.usWinDescent = spec.metrics.win_descent
        os2.fsType = spec.metrics.fs_type
        if spec.metrics.strikeout_position is not None:
            os2.yStrikeoutPosition = spec.metrics.strikeout_position
        if spec.metrics.strikeout_size is not None:
            os2.yStrikeoutSize = spec.metrics.strikeout_size
        if spec.metrics.use_typo_metrics:
            os2.fsSelection |= USE_TYPO_METRICS

        # VF 의 이름은 첫 weight 의 family 를 베이스로
        first = spec.weights[0]
        vf_psname = self._vf_psname(spec)
        nm = font["name"]
        latin_fam = self._strip_style(first.latin_family)
        korean_fam = self._strip_style(first.korean_family)
        nm.setName(latin_fam, 1, *_WIN_UNI, _LANG_EN)
        nm.setName(korean_fam, 1, *_WIN_UNI, _LANG_KO)
        nm.setName("Regular", 2, *_WIN_UNI, _LANG_EN)
        nm.setName(f"{latin_fam} VF", 4, *_WIN_UNI, _LANG_EN)
        nm.setName(f"{korean_fam} VF", 4, *_WIN_UNI, _LANG_KO)
        nm.setName(vf_psname, 6, *_WIN_UNI, _LANG_EN)
        nm.setName(f"Version {spec.meta.version}", 5, *_WIN_UNI, _LANG_EN)
        nm.setName(
            f"{spec.meta.vendor}:{spec.meta.project_name}:{vf_psname}:{spec.meta.version}",
            3, *_WIN_UNI, _LANG_EN,
        )
        for nr in spec.extra_names:
            nm.setName(
                nr.value, nr.name_id, nr.platform_id, nr.plat_enc_id, nr.lang_id,
            )
        font.save(str(output_path))

    # ── 내부: 테이블별 패치 ──────────────────────────────────────

    @staticmethod
    def _patch_head(font: TTFont, spec: BuildSpec) -> None:
        head = font["head"]
        m = re.match(r"\s*(\d+)\.(\d+)\s*", spec.meta.version or "")
        if m:
            head.fontRevision = float(f"{m.group(1)}.{m.group(2)}")

    @staticmethod
    def _patch_os2(font: TTFont, w: WeightSpec, spec: BuildSpec) -> None:
        os2 = font["OS/2"]
        os2.usWeightClass = w.weight_class
        os2.fsType = spec.metrics.fs_type
        os2.sTypoAscender = spec.metrics.typo_ascender
        os2.sTypoDescender = spec.metrics.typo_descender
        os2.sTypoLineGap = spec.metrics.typo_line_gap
        os2.usWinAscent = spec.metrics.win_ascent
        os2.usWinDescent = spec.metrics.win_descent
        if spec.metrics.strikeout_position is not None:
            os2.yStrikeoutPosition = spec.metrics.strikeout_position
        if spec.metrics.strikeout_size is not None:
            os2.yStrikeoutSize = spec.metrics.strikeout_size
        if spec.metrics.use_typo_metrics:
            os2.fsSelection |= USE_TYPO_METRICS

    @staticmethod
    def _patch_hhea(font: TTFont, spec: BuildSpec) -> None:
        hhea = font["hhea"]
        hhea.ascent = spec.metrics.hhea_ascent
        hhea.descent = spec.metrics.hhea_descent
        hhea.lineGap = spec.metrics.hhea_line_gap

    @staticmethod
    def _patch_post(font: TTFont, spec: BuildSpec) -> None:
        post = font["post"]
        if spec.metrics.underline_position is not None:
            post.underlinePosition = spec.metrics.underline_position
        if spec.metrics.underline_thickness is not None:
            post.underlineThickness = spec.metrics.underline_thickness

    @staticmethod
    def _patch_style_bits(font: TTFont, w: WeightSpec) -> None:
        os2 = font["OS/2"]
        head = font["head"]
        fs = os2.fsSelection
        fs &= ~(_FS_SEL_ITALIC | _FS_SEL_BOLD | _FS_SEL_REGULAR)
        if w.is_italic:
            fs |= _FS_SEL_ITALIC
        if w.is_bold:
            fs |= _FS_SEL_BOLD
        if not w.is_bold and not w.is_italic:
            fs |= _FS_SEL_REGULAR
        os2.fsSelection = fs

        mac = head.macStyle & ~(_HEAD_BOLD | _HEAD_ITALIC)
        if w.is_bold:
            mac |= _HEAD_BOLD
        if w.is_italic:
            mac |= _HEAD_ITALIC
        head.macStyle = mac

    @staticmethod
    def _is_ribbi(style: str) -> bool:
        return style in ("Regular", "Bold", "Italic", "Bold Italic")

    @classmethod
    def _patch_name(
        cls,
        font: TTFont,
        w: WeightSpec,
        spec: BuildSpec,
        fmt: str,
    ) -> None:
        """name 테이블 패치. RIBBI 여부에 따라 16/17 사용 분기."""
        ps_suffix = ""
        out = spec.find_output(fmt)
        if out and out.psname_suffix:
            ps_suffix = out.psname_suffix
        ps_full = w.psname + ps_suffix

        latin_fam = w.latin_family
        korean_fam = w.korean_family
        style = w.style_name
        nm = font["name"]

        if cls._is_ribbi(style):
            # 레거시 RIBBI: nameID 1=family, 2=style.
            # 16/17 은 RIBBI 면 생략 가능하나, 베이스 폰트의 옛 값 잔류를 막기 위해 동일하게 덮어씀.
            nm.setName(latin_fam, 1, *_WIN_UNI, _LANG_EN)
            nm.setName(style, 2, *_WIN_UNI, _LANG_EN)
            nm.setName(latin_fam, 16, *_WIN_UNI, _LANG_EN)
            nm.setName(style, 17, *_WIN_UNI, _LANG_EN)
            nm.setName(korean_fam, 1, *_WIN_UNI, _LANG_KO)
            nm.setName(style, 2, *_WIN_UNI, _LANG_KO)
            nm.setName(korean_fam, 16, *_WIN_UNI, _LANG_KO)
            nm.setName(style, 17, *_WIN_UNI, _LANG_KO)
        else:
            # 비-RIBBI: nameID 16/17 필수. nameID 1/2 는 fallback.
            nm.setName(f"{latin_fam} {style}", 1, *_WIN_UNI, _LANG_EN)
            nm.setName("Regular", 2, *_WIN_UNI, _LANG_EN)
            nm.setName(latin_fam, 16, *_WIN_UNI, _LANG_EN)
            nm.setName(style, 17, *_WIN_UNI, _LANG_EN)
            nm.setName(f"{korean_fam} {style}", 1, *_WIN_UNI, _LANG_KO)
            nm.setName("Regular", 2, *_WIN_UNI, _LANG_KO)
            nm.setName(korean_fam, 16, *_WIN_UNI, _LANG_KO)
            nm.setName(style, 17, *_WIN_UNI, _LANG_KO)

        # 공통
        full_en = f"{latin_fam} {style}"
        full_ko = f"{korean_fam} {style}"
        nm.setName(full_en, 4, *_WIN_UNI, _LANG_EN)
        nm.setName(full_ko, 4, *_WIN_UNI, _LANG_KO)
        nm.setName(ps_full, 6, *_WIN_UNI, _LANG_EN)
        nm.setName(f"Version {spec.meta.version}", 5, *_WIN_UNI, _LANG_EN)
        nm.setName(
            f"{spec.meta.vendor}:{full_en}:{spec.meta.version}",
            3, *_WIN_UNI, _LANG_EN,
        )

        # extra_names: 0/7/8/9/11/12/13/14 등
        for nr in spec.extra_names:
            nm.setName(
                nr.value, nr.name_id, nr.platform_id, nr.plat_enc_id, nr.lang_id,
            )

    # ── 내부: 후처리 (검증 + 케이스 기록) ───────────────────────

    def _finalize(
        self,
        out_path: Path,
        fmt: str,
        w: Optional[WeightSpec],
        *,
        validate: bool,
        symptom: str,
        diagnosis: str,
        patch_summary: str,
        table_tag: str,
    ) -> BuildOutputResult:
        verdict = ""
        if validate:
            try:
                val = self._ops.validate(str(out_path), "opentype")
                counts = (val.get("summary") or {}).get("counts", {})
                if counts.get("FAIL", 0) or counts.get("ERROR", 0):
                    verdict = "FAIL"
                elif counts.get("WARN", 0):
                    verdict = "WARN"
                elif counts.get("PASS", 0):
                    verdict = "PASS"
            except Exception as e:
                verdict = f"ERROR: {e}"[:80]
        case = self._memory.add(
            symptom=symptom,
            diagnosis=diagnosis,
            patch_summary=patch_summary,
            table_tag=table_tag,
            font_path=str(out_path),
            validation_after=verdict if verdict in ("PASS", "WARN", "FAIL") else None,
        )
        return BuildOutputResult(
            weight_class=w.weight_class if w else 0,
            style_name=w.style_name if w else "VF",
            fmt=fmt,
            output_path=str(out_path),
            validation_verdict=verdict,
            case_id=case.get("id"),
        )

    # ── 내부: 작은 헬퍼 ──────────────────────────────────────────

    @staticmethod
    def _psname_with_suffix(w: WeightSpec, spec: BuildSpec, fmt: str) -> str:
        out = spec.find_output(fmt)
        return w.psname + (out.psname_suffix if out else "")

    @staticmethod
    def _vf_psname(spec: BuildSpec) -> str:
        """첫 weight 의 PSname 에서 weight 접미사 제거 + 'VF' 접미사."""
        first = spec.weights[0]
        # 'HCSSansHdEB' 같은 PSname 에서 fullname_suffix(EB/Bd/SB...) 제거 시도
        ps = first.psname
        suffix = first.fullname_suffix
        if suffix and ps.endswith(suffix):
            ps = ps[: -len(suffix)]
        # 'Hd' / 'Tx' 같은 prefix 도 일반화하기 어려우니 그대로 둠
        out = spec.find_output("VF")
        return ps + (out.psname_suffix if out else "VF")

    @staticmethod
    def _strip_style(family_with_style: str) -> str:
        """'현대캐피탈 산스 Head Bold' → '현대캐피탈 산스 Head'.

        의뢰서 관례상 family 칼럼에 style 까지 포함되는 경우(Bold/SemiBold/Light 등)가
        있어 VF 이름에서 분리. 한국어/영어 모두 대응.
        """
        for tail in (
            " ExtraBold", " SemiBold", " Bold", " Medium", " Regular",
            " Light", " Thin", " Black",
        ):
            if family_with_style.endswith(tail):
                return family_with_style[: -len(tail)]
        return family_with_style

    @staticmethod
    def _summarize(results: list[BuildOutputResult]) -> dict:
        total = len(results)
        ok = sum(1 for r in results if not r.error)
        errs = total - ok
        by_fmt: dict[str, int] = {}
        for r in results:
            by_fmt[r.fmt] = by_fmt.get(r.fmt, 0) + 1
        return {"total": total, "ok": ok, "errors": errs, "by_format": by_fmt}
