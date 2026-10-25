"""Unit tests for expanded command coverage (fillers, open verbs, media, read-page, typos, ordering, state).

Verifies strict test isolation: asserts that NONE of the test phrases in this file
appear in evals/commands/dev.json.
"""

from __future__ import annotations

import json
from pathlib import Path


from config.apps import ALLOWED_APPS
from config.sites import SITES
from core.chains import has_explicit_sequencing_separator, parse_chain
from core.session_state import SessionState
from core.shortcuts import ShortcutPlan, ToolCallSpec, parse_shortcut

APPS = ALLOWED_APPS
SITES_MAP = SITES

# Registry of all test phrases used in this file to verify isolation from dev.json
TESTED_PHRASES: set[str] = set()


def _parse(text: str, state: SessionState | None = None, browser_enabled: bool = True):
    """Parse text using ordering rule: chain parser first if sequencing separator present."""
    TESTED_PHRASES.add(text.strip().lower())
    if has_explicit_sequencing_separator(text):
        chain_plan = parse_chain(text, APPS, SITES_MAP, state=state, browser_enabled=browser_enabled)
        if chain_plan is not None:
            return chain_plan
    spec = parse_shortcut(text, APPS, SITES_MAP, state=state, browser_enabled=browser_enabled)
    if spec is not None:
        return spec
    return parse_chain(text, APPS, SITES_MAP, state=state, browser_enabled=browser_enabled)


def _parse_shortcut_only(text: str, state: SessionState | None = None, browser_enabled: bool = True):
    """Parse text strictly through parse_shortcut."""
    TESTED_PHRASES.add(text.strip().lower())
    return parse_shortcut(text, APPS, SITES_MAP, state=state, browser_enabled=browser_enabled)


def test_filler_rules():
    """Verify leading, trailing, and combination fillers are stripped."""
    # (a) Leading fillers: hey, hi, ok, okay, now, just, please, can you, could you, can u, would you
    res = _parse("hey open notepad")
    assert isinstance(res, ToolCallSpec) and res.name == "open_application" and res.args == {"name": "notepad"}

    res = _parse("hi launch calculator")
    assert isinstance(res, ToolCallSpec) and res.name == "open_application" and res.args == {"name": "calculator"}

    res = _parse("ok start vscode")
    assert isinstance(res, ToolCallSpec) and res.name == "open_application" and res.args == {"name": "vscode"}

    res = _parse("okay open youtube")
    assert isinstance(res, ToolCallSpec) and res.name == "open_website"

    res = _parse("now open github")
    assert isinstance(res, ToolCallSpec) and res.name == "open_website"

    res = _parse("just launch notepad")
    assert isinstance(res, ToolCallSpec) and res.name == "open_application"

    res = _parse("please open calculator")
    assert isinstance(res, ToolCallSpec) and res.name == "open_application"

    res = _parse("can you open vscode")
    assert isinstance(res, ToolCallSpec) and res.name == "open_application"

    res = _parse("could you launch notepad")
    assert isinstance(res, ToolCallSpec) and res.name == "open_application"

    res = _parse("can u open wikipedia")
    assert isinstance(res, ToolCallSpec) and res.name == "open_website"

    res = _parse("would you start calculator")
    assert isinstance(res, ToolCallSpec) and res.name == "open_application"

    # Trailing fillers: "please", "for me", "now"
    res = _parse("launch calculator please")
    assert isinstance(res, ToolCallSpec) and res.name == "open_application"

    res = _parse("open youtube for me")
    assert isinstance(res, ToolCallSpec) and res.name == "open_website"

    res = _parse("launch notepad now")
    assert isinstance(res, ToolCallSpec) and res.name == "open_application"

    # Combinations of leading and trailing
    res = _parse("hey could you please open notepad for me")
    assert isinstance(res, ToolCallSpec) and res.name == "open_application"

    res = _parse("hi please launch calculator now")
    assert isinstance(res, ToolCallSpec) and res.name == "open_application"

    res = _parse("okay just open wikipedia for me please")
    assert isinstance(res, ToolCallSpec) and res.name == "open_website"


def test_expanded_open_verbs():
    """Verify open, launch, start, run (apps only), fire up, bring up, pull up, show me (sites only)."""
    # open, launch, start
    res = _parse("start notepad")
    assert isinstance(res, ToolCallSpec) and res.name == "open_application"

    res = _parse("start wikipedia")
    assert isinstance(res, ToolCallSpec) and res.name == "open_website"

    # run (apps only)
    res = _parse("run calculator")
    assert isinstance(res, ToolCallSpec) and res.name == "open_application"

    assert _parse("run wikipedia") is None
    assert _parse("run youtube") is None

    # fire up (apps and sites)
    res = _parse("fire up vscode")
    assert isinstance(res, ToolCallSpec) and res.name == "open_application"

    res = _parse("fire up wikipedia")
    assert isinstance(res, ToolCallSpec) and res.name == "open_website"

    # bring up (apps and sites)
    res = _parse("bring up notepad")
    assert isinstance(res, ToolCallSpec) and res.name == "open_application"

    res = _parse("bring up github")
    assert isinstance(res, ToolCallSpec) and res.name == "open_website"

    # pull up (apps and sites)
    res = _parse("pull up calculator")
    assert isinstance(res, ToolCallSpec) and res.name == "open_application"

    res = _parse("pull up wikipedia")
    assert isinstance(res, ToolCallSpec) and res.name == "open_website"

    # show me (sites only, not apps)
    res = _parse("show me wikipedia")
    assert isinstance(res, ToolCallSpec) and res.name == "open_website"

    res = _parse("show me youtube")
    assert isinstance(res, ToolCallSpec) and res.name == "open_website"

    assert _parse("show me notepad") is None
    assert _parse("show me calculator") is None


def test_go_to_and_take_me_to_sites_only():
    """Verify 'go to <site>' and 'take me to <site>' map to open_website for known sites, never apps."""
    res = _parse("go to wikipedia")
    assert isinstance(res, ToolCallSpec) and res.name == "open_website"
    assert res.args["url"] == SITES["wikipedia"]["url"]

    res = _parse("take me to github")
    assert isinstance(res, ToolCallSpec) and res.name == "open_website"
    assert res.args["url"] == SITES["github"]["url"]

    # Not for apps
    assert _parse("go to notepad") is None
    assert _parse("take me to calculator") is None


def test_media_phrases():
    """Verify play it, play that, play the video, pause it, mute it."""
    res = _parse("play it")
    assert isinstance(res, ToolCallSpec) and res.name == "browser_media" and res.args == {"action": "play"}

    res = _parse("play that")
    assert isinstance(res, ToolCallSpec) and res.name == "browser_media" and res.args == {"action": "play"}

    res = _parse("play the video")
    assert isinstance(res, ToolCallSpec) and res.name == "browser_media" and res.args == {"action": "play"}

    res = _parse("pause it")
    assert isinstance(res, ToolCallSpec) and res.name == "browser_media" and res.args == {"action": "pause"}

    res = _parse("mute it")
    assert isinstance(res, ToolCallSpec) and res.name == "browser_media" and res.args == {"action": "mute"}


def test_read_page_phrases():
    """Verify read page, read the page, read the page content, what's on the page, what is on this page."""
    res = _parse("read page")
    assert isinstance(res, ToolCallSpec) and res.name == "browser_read_page"

    res = _parse("read the page")
    assert isinstance(res, ToolCallSpec) and res.name == "browser_read_page"

    res = _parse("read the page content")
    assert isinstance(res, ToolCallSpec) and res.name == "browser_read_page"

    res = _parse("what's on the page")
    assert isinstance(res, ToolCallSpec) and res.name == "browser_read_page"

    res = _parse("what is on this page")
    assert isinstance(res, ToolCallSpec) and res.name == "browser_read_page"

    res = _parse("whats on the page")
    assert isinstance(res, ToolCallSpec) and res.name == "browser_read_page"


def test_typo_rules():
    """Verify edit-distance-1 tolerance for open, launch, start, search, close, play, scroll."""
    # open -> opn
    res = _parse("opn calculator")
    assert isinstance(res, ToolCallSpec) and res.name == "open_application"

    # launch -> launhc
    res = _parse("launhc notepad")
    assert isinstance(res, ToolCallSpec) and res.name == "open_application"

    # start -> strt
    res = _parse("strt vscode")
    assert isinstance(res, ToolCallSpec) and res.name == "open_application"

    # search -> serach
    res = _parse("serach wikipedia for python programming")
    assert isinstance(res, ToolCallSpec) and res.name == "search_site"
    assert res.args == {"site": "wikipedia", "query": "python programming"}

    # close -> clsoe
    res = _parse("clsoe notepad")
    assert isinstance(res, ToolCallSpec) and res.name == "close_application"

    # play -> plai
    res = _parse("plai it")
    assert isinstance(res, ToolCallSpec) and res.name == "browser_media" and res.args == {"action": "play"}

    # scroll -> scrol
    res = _parse("scrol up")
    assert isinstance(res, ToolCallSpec) and res.name == "browser_scroll" and res.args == {"direction": "up", "screens": 1}

    # NEVER turn real words into verbs (e.g. oven, stop, plan)
    assert _parse("open the oven") is None
    assert _parse("stop notepad") is None
    assert _parse("plan calculator") is None


def test_near_misses_stay_none():
    """Verify specified near-misses stay None."""
    near_misses = [
        "open the oven",
        "go to the store",
        "start the car",
        "play it safe with my money",
        "now what is 2+2",
        "run notepad and delete my files",
        "hey can you explain recursion",
    ]
    for nm in near_misses:
        assert _parse(nm) is None, f"Expected None for near-miss: {nm!r}"


def test_ordering_and_swallowed_tail():
    """Verify explicit sequencing separator prioritizes chain parser over single shortcut parser."""
    # "search wikipedia for ... then click the first result and read the page"
    # Prior bug swallowed tail into search query "artificial intelligence then click..."
    text = "search wikipedia for artificial intelligence then click the first result and read the page"
    res = _parse(text)
    assert isinstance(res, (ShortcutPlan, list)) or hasattr(res, "steps")
    steps = res.steps if hasattr(res, "steps") else res
    assert len(steps) == 3
    assert steps[0].tool == "search_site" and steps[0].args == {"site": "wikipedia", "query": "artificial intelligence"}
    assert steps[1].tool == "browser_click_result" and steps[1].args == {"n": 1}
    assert steps[2].tool == "browser_read_page"

    # Another swallowed tail test
    text2 = "search github for python then click the second result and read the page"
    res2 = _parse(text2)
    steps2 = res2.steps if hasattr(res2, "steps") else res2
    assert len(steps2) == 3
    assert steps2[0].tool == "search_site" and steps2[0].args == {"site": "github", "query": "python"}
    assert steps2[1].tool == "browser_click_result" and steps2[1].args == {"n": 2}
    assert steps2[2].tool == "browser_read_page"

    # Plain ordinal click stays plain click when no query
    res_click1 = _parse("click the 4th result")
    assert isinstance(res_click1, ToolCallSpec) and res_click1.name == "browser_click_result" and res_click1.args == {"n": 4}

    res_click2 = _parse("open the third one")
    assert isinstance(res_click2, ToolCallSpec) and res_click2.name == "browser_click_result" and res_click2.args == {"n": 3}

    res_click3 = _parse("play the 5th video")
    assert isinstance(res_click3, ToolCallSpec) and res_click3.name == "browser_click_result" and res_click3.args == {"n": 5}

    # Query starting with conjunction or command verb returns None from shortcut parser
    assert _parse_shortcut_only("search wikipedia for and read the page") is None
    assert _parse_shortcut_only("search wikipedia for click the first result") is None


def test_followup_simulated_state():
    """Verify follow-ups with simulated search state resolve ordinal and action via chain parser."""
    state = SessionState()
    state.update_from_tool("search_site", {"site": "youtube", "query": "ambient space music"}, "success")

    text = "now click number three and play it"
    plan = _parse(text, state=state)
    assert plan is not None
    steps = plan.steps if hasattr(plan, "steps") else plan
    assert len(steps) == 2
    assert steps[0].tool == "browser_click_result" and steps[0].args == {"n": 3}
    assert steps[1].tool == "browser_media" and steps[1].args == {"action": "play"}


def test_click_result_n_without_ordinal_word():
    """Verify 'click result N' and related forms without an ordinal word map to browser_click_result."""
    res1 = _parse("click result 1")
    assert isinstance(res1, ToolCallSpec) and res1.name == "browser_click_result" and res1.args == {"n": 1}

    res2 = _parse("click result 2")
    assert isinstance(res2, ToolCallSpec) and res2.name == "browser_click_result" and res2.args == {"n": 2}

    res3 = _parse("click the result 3")
    assert isinstance(res3, ToolCallSpec) and res3.name == "browser_click_result" and res3.args == {"n": 3}

    res4 = _parse("open result 4")
    assert isinstance(res4, ToolCallSpec) and res4.name == "browser_click_result" and res4.args == {"n": 4}

    res5 = _parse("play video 2")
    assert isinstance(res5, ToolCallSpec) and res5.name == "browser_click_result" and res5.args == {"n": 2}

    res6 = _parse("click item 5")
    assert isinstance(res6, ToolCallSpec) and res6.name == "browser_click_result" and res6.args == {"n": 5}

    res7 = _parse("click result number 2")
    assert isinstance(res7, ToolCallSpec) and res7.name == "browser_click_result" and res7.args == {"n": 2}


def test_chain_comma_plus_after_that_boundaries():
    """Verify 'comma plus after that' boundaries are cleanly detected and chained."""
    text1 = "open google, after that search google for python"
    plan1 = _parse(text1)
    assert isinstance(plan1, ShortcutPlan) and len(plan1.steps) == 2
    assert plan1.steps[0].tool == "open_website" and plan1.steps[0].args == {"url": "https://www.google.com"}
    assert plan1.steps[1].tool == "search_site" and plan1.steps[1].args == {"site": "google", "query": "python"}

    text2 = "go to youtube, after that click result 1"
    plan2 = _parse(text2)
    assert isinstance(plan2, ShortcutPlan) and len(plan2.steps) == 2
    assert plan2.steps[0].tool == "open_website" and plan2.steps[0].args == {"url": "https://www.youtube.com"}
    assert plan2.steps[1].tool == "browser_click_result" and plan2.steps[1].args == {"n": 1}


def test_chain_other_sites_form():
    """Verify other sites in the 'go to <site>, search <site> for <q>, after that click result N' chain form."""
    text_gh = "go to github, search github for rust, after that click result 2"
    plan_gh = _parse(text_gh)
    assert isinstance(plan_gh, ShortcutPlan) and len(plan_gh.steps) == 3
    assert plan_gh.steps[0].tool == "open_website" and plan_gh.steps[0].args == {"url": "https://github.com"}
    assert plan_gh.steps[1].tool == "search_site" and plan_gh.steps[1].args == {"site": "github", "query": "rust"}
    assert plan_gh.steps[2].tool == "browser_click_result" and plan_gh.steps[2].args == {"n": 2}

    text_wiki = "go to wikipedia, search wikipedia for compilers, after that click result 3"
    plan_wiki = _parse(text_wiki)
    assert isinstance(plan_wiki, ShortcutPlan) and len(plan_wiki.steps) == 3
    assert plan_wiki.steps[0].tool == "open_website" and plan_wiki.steps[0].args == {"url": "https://www.wikipedia.org"}
    assert plan_wiki.steps[1].tool == "search_site" and plan_wiki.steps[1].args == {"site": "wikipedia", "query": "compilers"}
    assert plan_wiki.steps[2].tool == "browser_click_result" and plan_wiki.steps[2].args == {"n": 3}

    text_yt = "go to youtube, search youtube for tutorials, after that click result 4"
    plan_yt = _parse(text_yt)
    assert isinstance(plan_yt, ShortcutPlan) and len(plan_yt.steps) == 3
    assert plan_yt.steps[0].tool == "open_website" and plan_yt.steps[0].args == {"url": "https://www.youtube.com"}
    assert plan_yt.steps[1].tool == "search_site" and plan_yt.steps[1].args == {"site": "youtube", "query": "tutorials"}
    assert plan_yt.steps[2].tool == "browser_click_result" and plan_yt.steps[2].args == {"n": 4}

    text_maps = "go to maps, search maps for restaurants, after that click result 1"
    plan_maps = _parse(text_maps)
    assert isinstance(plan_maps, ShortcutPlan) and len(plan_maps.steps) == 3
    assert plan_maps.steps[0].tool == "open_website" and plan_maps.steps[0].args == {"url": "https://www.google.com/maps"}
    assert plan_maps.steps[1].tool == "search_site" and plan_maps.steps[1].args == {"site": "maps", "query": "restaurants"}
    assert plan_maps.steps[2].tool == "browser_click_result" and plan_maps.steps[2].args == {"n": 1}


def test_unsafe_chained_actions_stay_none():
    """Verify near-misses and unsafe chained actions stay None without falling through to shortcuts."""
    # Near-miss with dangerous non-command verb
    assert _parse("go to maps and delete my files") is None
    assert _parse("go to wikipedia and format disk") is None

    # Dangling follow-up boundary with no prior browser state
    assert _parse("after that click result 1", state=None) is None
    assert _parse_shortcut_only("after that click result 1", state=None) is None
    assert parse_chain("after that click result 1", APPS, SITES_MAP, state=None, browser_enabled=True) is None

    assert _parse("after that click result 2", state=None) is None
    assert _parse_shortcut_only("after that click result 2", state=None) is None
    assert parse_chain("after that click result 2", APPS, SITES_MAP, state=None, browser_enabled=True) is None


def test_isolation_from_dev_json():
    """CRITICAL: Assert that none of the test phrases in this file exist in evals/commands/dev.json."""
    dev_path = Path(__file__).resolve().parent.parent / "evals" / "commands" / "dev.json"
    assert dev_path.is_file(), f"dev.json not found at {dev_path}"
    dev_items = json.loads(dev_path.read_text(encoding="utf-8"))
    dev_phrases = {item["text"].strip().lower() for item in dev_items}

    overlap = TESTED_PHRASES.intersection(dev_phrases)
    assert not overlap, f"Test isolation failure! Phrases found in dev.json: {overlap}"
