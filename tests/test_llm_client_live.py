import asyncio
import os

import pytest

from server.llm import chat_completion

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_LIVE_LLM_TESTS") != "1",
    reason="Set RUN_LIVE_LLM_TESTS=1 to call the real LLM API.",
)


def test_live_json_mode_smoke():
    result = asyncio.run(
        chat_completion(
            system_prompt=(
                "You are a JSON-only intent parser. Return one valid query_servants "
                "intent for the user's FGO servant query."
            ),
            user_message="30 自充的从者有哪些",
            max_tokens=300,
            temperature=0,
            json_mode=True,
        )
    )

    assert result["intent"] == "query_servants"
    assert isinstance(result["conditions"], dict)
    assert result["_response_format"] in {"json_schema", "text_fallback"}
    print(f"LIVE_LLM_JSON_MODE_RESULT model={result['_model']} response_format={result['_response_format']}")
