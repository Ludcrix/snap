
from bot.generators.story_generator import generate_story
from bot.generators.subtitle_generator import generate_subtitles
from bot.generators.voice_generator import generate_voice_with_duration, VOICE_LEAD_IN_SILENCE_SECONDS
from bot.generators.video_generator import generate_video

import json

def log(prefix, message):
    print(f"[{prefix}] {message}")

if __name__ == "__main__":
    story_obj = generate_story(log_fn=log)
    log("STORY", story_obj['story'])
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
        story_obj['story'],
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
