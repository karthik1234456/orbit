"""Unit tests for deterministic voice alias variants and parsing resolution."""

from core.shortcuts import ToolCallSpec, parse_shortcut
from voice.aliases import variants, resolve
from config.apps import ALLOWED_APPS
from config.sites import SITES


def dummy_parser(text: str):
    """Parser using real shortcuts parser with browser_enabled."""
    return parse_shortcut(text, allowed_apps=ALLOWED_APPS, sites=SITES, browser_enabled=True)


def test_variants_only_from_config_keys():
    """Verify variants are generated strictly from registered apps and sites."""
    v1 = variants("open note pad")
    assert "open notepad" in v1

    v2 = variants("search you tube for music")
    assert "search youtube for music" in v2

    v3 = variants("open git hub")
    assert "open github" in v3

    v4 = variants("open wiki pedia")
    assert "open wikipedia" in v4

    # Spaced and apostrophe forms for whatsapp
    v5 = variants("open whats app")
    assert "open whatsapp" in v5

    v6 = variants("open what's app")
    assert "open whatsapp" in v6

    # Text without any match produces empty list
    v_none = variants("open unrelated browser window")
    assert v_none == []


def test_variants_whole_word_matching():
    """Verify alias splits are matched as whole words and not partial substrings."""
    # 'note padding' should not replace 'note pad' if word boundary is respected
    v = variants("note padding something")
    assert not any("notepad" in x for x in v)


def test_resolve_original_parses_untouched():
    """If original text already parses as a valid shortcut, return it untouched."""
    text = "open notepad"
    res_text, understood = resolve(text, dummy_parser)
    assert res_text == "open notepad"
    assert understood is None


def test_resolve_single_variant_parses_used_and_reported():
    """If original does not parse, but exactly ONE candidate parses, use and report it."""
    text = "open note pad"
    res_text, understood = resolve(text, dummy_parser)
    assert res_text == "open notepad"
    assert understood == "open notepad"

    text2 = "open what's app"
    res_text2, understood2 = resolve(text2, dummy_parser)
    assert res_text2 == "open whatsapp"
    assert understood2 == "open whatsapp"


def test_resolve_none_parse_returns_original():
    """If no variants parse, return the original text untouched (never guess)."""
    text = "open note pad into some invalid syntax"
    res_text, understood = resolve(text, dummy_parser)
    assert res_text == text
    assert understood is None


def test_resolve_ambiguous_returns_original():
    """If several distinct parsed results exist, return original untouched (never guess)."""
    # Create an artificial parser where two different candidate variants parse into different actions
    def ambiguous_parser(t: str):
        if "notepad" in t:
            return ToolCallSpec(name="open_application", args={"name": "notepad"})
        if "calculator" in t:
            return ToolCallSpec(name="open_application", args={"name": "calculator"})
        return None

    # Synthetic text that matches multiple alias patterns
    text = "open note pad and calc ulator"
    # Even if variants generate multiple options, if ambiguous parser finds >1 distinct action,
    # resolve must return original text untouched
    mock_apps = {"notepad": {}, "calculator": {}}
    res_text, understood = resolve(text, ambiguous_parser, allowed_apps=mock_apps, sites={})
    assert res_text == text
    assert understood is None

