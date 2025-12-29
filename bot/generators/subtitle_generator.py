def split_subtitles(text, max_length=40):
    # Naive split by sentences
    return [s.strip() for s in text.split('.') if s.strip()]
