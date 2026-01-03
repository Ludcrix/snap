from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import subprocess
import time
import shutil

from .base import BaseMediaResolver, MediaResolution


@dataclass(frozen=True)
class SimulatedMediaResolverConfig:
    ffmpeg_path: str = "ffmpeg"


class SimulatedMediaResolver(BaseMediaResolver):
    """Creates a small local MP4 placeholder for simulation.

    This does NOT download Instagram.
    It produces a working copy for offline preview in Telegram.

    Behavior:
    - If URL looks like instagram.com -> mark RESOLUTION_REQUIRED (no scraping).
    - Otherwise (simulated/fictive URLs) -> generate a short MP4 placeholder via ffmpeg.
    """

    def __init__(self, cfg: SimulatedMediaResolverConfig | None = None):
        self._cfg = cfg or SimulatedMediaResolverConfig()

    def _log(self, msg: str) -> None:
        print(f"[MEDIA] {msg}")

    def _looks_like_instagram(self, url: str) -> bool:
        u = (url or "").lower()
        return ("instagram.com" in u) or ("instagr.am" in u)

    def resolve(self, *, source_url: str, video_id: str, out_dir: str) -> MediaResolution:
        source_url = str(source_url or "").strip()
        out_dir_p = Path(out_dir)
        out_dir_p.mkdir(parents=True, exist_ok=True)

        self._log(f"resolve_attempt video_id={video_id} url={source_url}")

        is_ig = self._looks_like_instagram(source_url)

        ffmpeg = self._cfg.ffmpeg_path
        if not shutil.which(ffmpeg) and not Path(ffmpeg).exists():
            self._log("failed reason=ffmpeg_not_found")
            return MediaResolution(
                status="FAILED",
                source_url=source_url,
                local_path=None,
                message="ffmpeg introuvable: impossible de générer la preview locale.",
            )

        out_path = out_dir_p / f"{video_id}.mp4"

        # 6s 720x1280, black background.
        # Note: drawtext is intentionally avoided for Windows robustness (quoting/fonts/newlines).
        cmd = [
            ffmpeg,
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=720x1280:r=25:d=6",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            str(out_path),
        ]

        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            if p.returncode != 0:
                self._log(f"failed reason=ffmpeg_error stderr={p.stderr.strip()[:200]}")
                return MediaResolution(
                    status="FAILED",
                    source_url=source_url,
                    local_path=None,
                    message="ffmpeg a échoué: preview non générée.",
                )
        except Exception as e:
            self._log(f"failed reason=exception err={type(e).__name__}")
            return MediaResolution(
                status="FAILED",
                source_url=source_url,
                local_path=None,
                message="Exception pendant la génération preview.",
            )

        self._log(f"resolved local_path={out_path}")
        if is_ig:
            # We generated a placeholder only. We did not download the real IG media.
            self._log(f"resolution_required reason=instagram_url_no_scrape preview_only video_id={video_id}")
            return MediaResolution(
                status="RESOLUTION_REQUIRED",
                source_url=source_url,
                local_path=str(out_path),
                message="Preview simulée générée (placeholder), mais résolution IG réelle requise (pas de scraping).",
            )

        return MediaResolution(
            status="RESOLVED",
            source_url=source_url,
            local_path=str(out_path),
            message="Preview locale générée.",
        )
