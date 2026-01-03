from bot.generators.story_generator import generate_story
from bot.generators.subtitle_generator import generate_subtitles
from bot.generators.voice_generator import generate_voice_with_duration, VOICE_LEAD_IN_SILENCE_SECONDS
from bot.generators.video_generator import generate_video

import sys
import json


def log(prefix, message):
    print(f"[{prefix}] {message}")


if __name__ == "__main__":
    # Legacy generator entrypoint (renamed from bot/main.py to avoid confusion with V3).
    # Usage:
    #   py -m bot.generate_cli anomalie_objet
    #   py -m bot.generate_cli --format=anomalie_objet
    argv = [a.strip() for a in (sys.argv[1:] or []) if a and a.strip()]
    if any(a == "anomalie_objet" or a == "--format=anomalie_objet" for a in argv):
        from bot.formats.anomalie_objet.pipeline import generate_one_anomalie_objet

        def _parse_count(args: list[str]) -> int:
            # Supported:
            #   --count N
            #   --count=N
            MAX_COUNT = 20
            count: int | None = None
            for i, a in enumerate(args):
                if a.startswith("--count="):
                    try:
                        count = int(a.split("=", 1)[1])
                    except Exception:
                        count = None
                elif a == "--count" and i + 1 < len(args):
                    try:
                        count = int(args[i + 1])
                    except Exception:
                        count = None
            if count is None:
                return 1
            if count < 1:
                return 1
            if count > MAX_COUNT:
                return MAX_COUNT
            return count

        n = _parse_count(argv)
        for idx in range(1, n + 1):
            log("AO", f"Generating clip {idx}/{n}")
            res = generate_one_anomalie_objet(log_fn=log)
            log("AO", f"Video: {res.video_path}")
        sys.exit(0)

    story_obj = generate_story(log_fn=log)
    log("STORY", story_obj["story"])
    visual_fingerprint = json.dumps(story_obj["visual"], sort_keys=True, separators=(",", ":"))
    log("STORY", f"Visual: {visual_fingerprint}")
    log("IMAGE", "Prompt generated")
    log("IMAGE", f"Prompt: {story_obj['image_prompt']}")

    # Voice must be generated immediately after GPT output, and only once per run.
    # Use a dedicated voice_script (not raw story text) for TTS.
    voice_script = str(story_obj["voice_script"]).strip()
    voice_style = story_obj.get("voice")
    audio_path, audio_duration = generate_voice_with_duration(voice_script, voice_style=voice_style, log_fn=log)

    # Subtitles must be generated AFTER audio and use audio duration for timing.
    subtitle_path = generate_subtitles(
        story_obj["story"],
        audio_duration_seconds=audio_duration,
        start_offset_seconds=VOICE_LEAD_IN_SILENCE_SECONDS,
    )
    log("SUBTITLES", f"File: {subtitle_path}")
    video_path = generate_video(
        audio_path,
        subtitle_path,
        story_obj["visual"],
        image_path=None,
        image_prompt=story_obj["image_prompt"],
        expected_visual_fingerprint=visual_fingerprint,
        log_fn=log,
    )
    log("VIDEO", f"File: {video_path}")
