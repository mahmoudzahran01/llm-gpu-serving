from __future__ import annotations

from llmserve.quantize import calibration_texts


def test_calibration_texts_use_the_chat_template_with_system_question_and_answer():
    seen = []

    def template(messages, tokenize):
        seen.append(messages)
        assert tokenize is False
        return "|".join(m["content"] for m in messages)

    pairs = [{"question": f"q{i}", "sql": f"SELECT {i}"} for i in range(5)]
    texts = calibration_texts(template, pairs, "SYS", limit=3)
    assert texts == ["SYS|q0|SELECT 0", "SYS|q1|SELECT 1", "SYS|q2|SELECT 2"]
    assert [m["role"] for m in seen[0]] == ["system", "user", "assistant"]
    assert pairs[0] == {"question": "q0", "sql": "SELECT 0"}  # input untouched
