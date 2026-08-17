import sys
from pathlib import Path

# Fix import path for tests
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import config

def test_load_chat_skill_template_rendering():
    result = config.load_chat_skill()
    # It should not contain raw YAML keys if rendered properly
    assert "persona:" not in result
    assert "tone_of_voice:" not in result
    # It should contain rendered text (for example, the persona content)
    assert len(result) > 0
