import os
from dotenv import load_dotenv

dotenv_path = os.path.join(os.path.dirname(__file__), "..", ".env")
load_dotenv(dotenv_path=dotenv_path)

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
MODEL_NAME = "gpt-4.1-mini"

if not OPENAI_API_KEY:
    raise RuntimeError("OPENAI_API_KEY is missing in .env file")
