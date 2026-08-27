from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass(frozen=True)
class DeviceFingerprint:
    """Platform identity discovered from real-DUT read-only sources.

    ``models`` is a tuple of lowercase model tokens (e.g. "apf3260"), ``platform_id``
    is the SoC/platform id (e.g. "mt7981"), ``vendor`` the release vendor id (e.g.
    "ruijie"), and ``raw`` keeps the exact probe payloads for audit.
    """

    platform_id: str | None
    models: tuple[str, ...]
    vendor: str | None
    soc: str | None
    raw: dict[str, str] = field(default_factory=dict)

    def tokens(self) -> set[str]:
        """Lowercased token set used to enrich effective-profile resolution.

        Tokens are deliberately conservative: dedicated fields plus ``mtNNNN`` SoC
        matches and short comma/space-separated pieces from the two device-tree
        sources.  Free-form release text is never tokenised to avoid false matches.
        """
        tokens: set[str] = set()
        for value in (self.platform_id, self.vendor, self.soc):
            if value:
                cleaned = value.strip().lower()
                if cleaned:
                    tokens.add(cleaned)
        for model in self.models:
            cleaned = model.strip().lower()
            if cleaned:
                tokens.add(cleaned)
        for key in ("compatible", "device_tree_model"):
            value = (self.raw.get(key) or "").strip().lower()
            if not value:
                continue
            for match in re.findall(r"\bmt\d{4}\b", value):
                tokens.add(match)
            for piece in re.split(r"[\s,/]+", value):
                piece = piece.strip()
                if piece and piece != "mediatek":
                    tokens.add(piece)
        return tokens


class DeviceFingerprintResolver:
    """Best-effort real-DUT platform fingerprinting over read-only commands.

    Never fails the caller: a probe error yields an empty fingerprint so the
    effective-profile resolver can still use DB-derived device tokens.
    """

    _MODEL_RE = re.compile(r"^model\s*=\s*['\"]?(?P<name>[^'\"]+)['\"]?$", re.MULTILINE)
    _TARGET_RE = re.compile(r"^DISTRIB_TARGET\s*=\s*['\"]?(?P<target>[^'\"]+)['\"]?$", re.MULTILINE)
    _ID_RE = re.compile(r"^DISTRIB_ID\s*=\s*['\"]?(?P<vendor>[^'\"]+)['\"]?$", re.MULTILINE)
    _SOC_RE = re.compile(r"\bmt\d{4}\b")

    def __init__(self, reader):
        self.reader = reader

    async def _read(self, command: str) -> str | None:
        try:
            return await self.reader.run(command, timeout=10.0)
        except Exception:
            return None

    async def resolve(self) -> DeviceFingerprint:
        compatible = await self._read("cat /proc/device-tree/compatible")
        model_raw = await self._read("cat /proc/device-tree/model")
        release = await self._read("cat /etc/openwrt_release")
        raw = {
            "compatible": (compatible or "").strip(),
            "device_tree_model": (model_raw or "").strip(),
            "openwrt_release": (release or "").strip(),
        }
        soc = self._extract_soc((compatible or "") + " " + (model_raw or ""))
        platform_id = self._extract_soc(compatible or "") or soc
        models = self._extract_models(model_raw or "", release or "")
        vendor = self._extract_vendor(release or "")
        return DeviceFingerprint(
            platform_id=platform_id,
            models=models,
            vendor=vendor,
            soc=soc,
            raw=raw,
        )

    @staticmethod
    def _extract_soc(text: str) -> str | None:
        match = DeviceFingerprintResolver._SOC_RE.search(text.lower())
        return match.group(0) if match else None

    @staticmethod
    def _extract_models(model_raw: str, release: str) -> tuple[str, ...]:
        models: list[str] = []
        target = DeviceFingerprintResolver._TARGET_RE.search(release)
        if target:
            parts = [p for p in target.group("target").strip().split("/") if p]
            if len(parts) >= 2:
                models.append(parts[-1])
        name = (model_raw or "").strip()
        if name and not name.lower().startswith("mediatek mt"):
            models.append(name)
        seen: list[str] = []
        for model in models:
            cleaned = model.strip().lower()
            if cleaned and cleaned not in seen:
                seen.append(cleaned)
        return tuple(seen)

    @staticmethod
    def _extract_vendor(release: str) -> str | None:
        match = DeviceFingerprintResolver._ID_RE.search(release or "")
        vendor = (match.group("vendor") if match else "").strip().lower()
        return vendor or None
