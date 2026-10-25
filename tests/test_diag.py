"""Unit tests for ORBIT diagnostics generation and secret redaction.

Verifies:
- No secret patterns survive (API keys, tokens, .env lines, passwords).
- Report includes ORBIT version and relevant settings.
- Size cap of 20 KB (20,480 bytes) is strictly enforced.
- Missing browser or empty session context does not crash.
- Generated file is written to disk properly.
"""

from pathlib import Path

from core.diagnostics import build_diagnostics_text, generate_diagnostics, redact_secrets_text
from core.version import VERSION
from tests.helpers.fake_secrets import (
    fake_aws_access_key,
    fake_github_token,
    fake_google_api_key,
    fake_jwt_token,
    fake_sk_key,
    fake_slack_token,
)


def test_redact_secrets_text_removes_keys_and_tokens():
    """Verify that standalone keys, tokens, and assignments are redacted from text."""
    google_key = fake_google_api_key(suffix="SyA1234567890abcdef123456")
    openai_key = fake_sk_key(suffix="proj-1234567890abcdef12345678")
    aws_key = fake_aws_access_key(middle="IOSFODNN7", suffix="EXAMPLE")
    gh_token = fake_github_token(kind="p", suffix="1234567890abcdef1234")
    slack_token = fake_slack_token(kind="b", team="1234567890", token="abcdef1234")
    jwt_token = fake_jwt_token(signature="abc")
    env_google_key = fake_google_api_key(suffix="SyB9876543210zyxwvu987654")

    raw = (
        f"Here is my Google key: {google_key}\n"
        f"Here is OpenAI key: {openai_key}\n"
        f"Here is AWS key: {aws_key}\n"
        f"Here is GitHub token: {gh_token}\n"
        f"Here is Slack token: {slack_token}\n"
        f"Here is Authorization: Bearer {jwt_token}\n"
        f"Here is env assignment: GEMINI_API_KEY={env_google_key}\n"
        "Here is secret assignment: password: MySuperSecretPassword123\n"
        "Here is token assignment: token=my_secret_token_value_456\n"
    )

    sanitized = redact_secrets_text(raw)

    assert google_key not in sanitized
    assert openai_key not in sanitized
    assert aws_key not in sanitized
    assert gh_token not in sanitized
    assert slack_token not in sanitized
    assert "MySuperSecretPassword123" not in sanitized
    assert "my_secret_token_value_456" not in sanitized
    assert env_google_key not in sanitized


def test_diagnostics_secret_redaction_in_chat_and_events():
    """Verify diagnostics generation redacts secrets passed in chat lines and events."""
    user_api_key = fake_google_api_key(suffix="SyA1234567890abcdef123456")
    orbit_token = fake_sk_key(suffix="proj-1234567890abcdef12345678")
    env_key = fake_google_api_key(suffix="SyC1234567890abcdef123456")

    fake_chat = [
        f"User: my api key is {user_api_key}",
        f"ORBIT: using token {orbit_token} to proceed",
        f"User: .env has GEMINI_API_KEY={env_key}",
    ]
    fake_events = [
        {
            "task": 1,
            "step": 1,
            "tool": "save_config",
            "status": "success",
            "detail": "saved password: VerySecretPassword99",
        }
    ]

    report = build_diagnostics_text(
        recent_chat_lines=fake_chat,
        activity_events=fake_events,
    )

    assert user_api_key not in report
    assert orbit_token not in report
    assert env_key not in report
    assert "VerySecretPassword99" not in report


def test_diagnostics_contains_version_and_settings():
    """Verify diagnostics text contains ORBIT version and relevant settings."""
    report = build_diagnostics_text()

    assert f"ORBIT Version: {VERSION}" in report
    assert "Python Version:" in report
    assert "OS Version:" in report
    assert "=== SETTINGS ===" in report
    assert "BROWSER_" in report
    assert "RAG_AUTO:" in report
    assert "COMMAND_SHORTCUTS:" in report
    assert "SESSION_CONTEXT:" in report
    assert "OLLAMA_NUM_CTX:" in report


def test_diagnostics_size_cap_20kb():
    """Verify diagnostics text enforces the 20 KB (20,480 bytes) size cap."""
    # Generate large chat lines exceeding 50 KB
    huge_chat = [f"User message {i}: " + ("x" * 150) for i in range(400)]
    huge_events = [
        {"task": i, "step": 1, "tool": "dummy", "status": "ok", "detail": "y" * 100}
        for i in range(200)
    ]

    report = build_diagnostics_text(
        recent_chat_lines=huge_chat,
        activity_events=huge_events,
    )

    encoded_bytes = report.encode("utf-8")
    assert len(encoded_bytes) <= 20480, f"Report size {len(encoded_bytes)} exceeded 20 KB cap"
    assert "[Truncated to 20 KB size cap]" in report


def test_diagnostics_missing_browser_and_empty_context():
    """Verify diagnostics generation does not crash when browser or context is missing."""
    # Pass None for everything
    report = build_diagnostics_text(
        app=None,
        agent=None,
        chat_controller=None,
        recent_chat_lines=None,
        activity_events=None,
    )

    assert "=== BROWSER STATUS ===" in report
    assert "Status: not configured" in report or "Status: closed" in report
    assert "=== SESSION CONTEXT ===" in report
    assert "No active session context." in report


def test_generate_diagnostics_writes_file_to_disk(tmp_path: Path):
    """Verify generate_diagnostics creates the output file and returns valid path."""
    file_path, report_text = generate_diagnostics(
        output_dir=str(tmp_path),
        provider_label="MOCK",
        model_name="mock-model",
    )

    assert file_path.exists()
    assert file_path.name.startswith("diag-")
    assert file_path.name.endswith(".txt")

    content = file_path.read_text(encoding="utf-8")
    assert content == report_text
    assert f"ORBIT Version: {VERSION}" in content
    assert "Provider: MOCK" in content
    assert "Active Model: mock-model" in content


def test_diagnostics_includes_browser_failure_and_selectors():
    """Verify diagnostics includes LAST BROWSER FAILURE and BROWSER SELECTORS sections."""
    class FakeBrowserController:
        def __init__(self):
            self.last_failure = {
                "page_type": "youtube_home",
                "url_host": "www.youtube.com",
                "url_path": "/",
                "viewport_size": {"width": 1920, "height": 1080},
                "anchors_per_selector": {"ytd-rich-item-renderer a#video-title-link": 0},
                "exclusions": {"shorts_shelf": 2, "ads": 1},
            }

        def status(self):
            return {"alive": True, "url": "https://www.youtube.com/", "title": "YouTube"}

    class FakeApp:
        def __init__(self):
            self.browser_controller = FakeBrowserController()

    app = FakeApp()
    report = build_diagnostics_text(app=app)

    assert "=== LAST BROWSER FAILURE ===" in report
    assert "Page Type: youtube_home" in report
    assert "Host: www.youtube.com" in report
    assert "Viewport: 1920x1080" in report
    assert "ytd-rich-item-renderer a#video-title-link: 0" in report
    assert "shorts_shelf: 2" in report
    assert "=== BROWSER SELECTORS ===" in report
    assert "YouTube Containers:" in report
    assert "YouTube Watch Anchors:" in report

