# Ralph Loop Fix Plan: Make all tests in tests/ pass

## Working Memory & Cross-Turn Learnings
- Python 3.13.7 and pytest 9.1.1 are installed.
- Core dependencies `google-genai` and `python-dotenv` are verified and functional.
- Implemented `core/llm.py` with `LLMProvider`, `GeminiProvider`, and `LLMError`. Deferred settings import to provider init to ensure unit tests run without credentials.
- Implemented `core/agent.py` with `Agent` supporting history tracking (grows by 2 per turn), bounding to the last 20 messages, and `reset()`.
- Implemented `tests/test_agent.py` covering:
  1. `test_chat_returns_reply`: verified
  2. `test_history_grows_by_two_per_turn`: verified
  3. `test_only_last_20_messages_sent_to_provider`: verified
  4. `test_reset_clears_history`: verified
- All 4 tests pass with exit code 0.
- Implemented resilient terminal chat loop in `main.py` and comprehensive `README.md`.

## Work Items Queue
- [x] 1. Implement core/llm.py (LLMProvider, GeminiProvider, LLMError)
- [x] 2. Implement core/agent.py (Agent with chat, history limit of 20, reset)
- [x] 3. Implement tests/test_agent.py (FakeProvider, test chat reply, test history grows by 2, test last 20 messages sent, test reset)
- [x] 4. Run verification tests (`python -m pytest`) and resolve any issues — 4 passed in 1.21s
- [x] 5. Implement main.py and finalize README.md

<promise>ALL_TESTS_PASSING</promise>
