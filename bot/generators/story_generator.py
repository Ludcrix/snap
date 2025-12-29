import os
import random
import openai
from ..config import OPENAI_API_KEY, MODEL_NAME

def generate_story():
    base_dir = os.path.dirname(os.path.dirname(__file__))
    prompts_dir = os.path.join(base_dir, "prompts")
    system_path = os.path.join(prompts_dir, "system.txt")
    story_path = os.path.join(prompts_dir, "story.txt")

    with open(system_path, "r", encoding="utf-8") as f:
        system_prompt = f.read()
    with open(story_path, "r", encoding="utf-8") as f:
        story_prompt = f.read()

    emotion = random.choice(["injustice", "malaise", "trahison"])
    story_prompt = story_prompt.replace("{EMOTION}", emotion)

    openai.api_key = OPENAI_API_KEY
    response = openai.ChatCompletion.create(
        model=MODEL_NAME,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": story_prompt}
        ]
    )
    return response.choices[0].message["content"]
