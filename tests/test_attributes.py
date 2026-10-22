import pytest

from pitchside.etl.attributes import classify_batting, classify_bowling, clean_wikitext, infobox_fields


@pytest.mark.parametrize("raw, expected", [
    ("Right-arm [[Fast bowling|medium]]", "Right-arm medium"),
    ("Right-arm [[off break]]<ref>x</ref>", "Right-arm off break"),
    ("{{ubl|Right-arm [[leg break]]|Right-arm [[off break]]}}", "Right-arm leg break"),
    ("{{plainlist|\n* Left-arm [[fast-medium]]\n* Left-arm orthodox\n}}", "Left-arm fast-medium"),
    ("<!-- comment -->Slow left-arm orthodox", "Slow left-arm orthodox"),
])
def test_clean_wikitext(raw, expected):
    assert clean_wikitext(raw) == expected


@pytest.mark.parametrize("style, expected", [
    ("Right-arm fast", ("right", "pace", "RF")),
    ("Right-arm fast-medium", ("right", "pace", "RFM")),
    ("Right-arm medium-fast", ("right", "pace", "RMF")),
    ("Right-arm medium", ("right", "pace", "RM")),
    ("Left-arm fast-medium", ("left", "pace", "LFM")),
    ("Right-arm off break", ("right", "spin", "OB")),
    ("Right-arm off-break", ("right", "spin", "OB")),
    ("Right-arm leg break googly", ("right", "spin", "LB")),
    ("Leg break googly", ("right", "spin", "LB")),
    ("Slow left-arm orthodox", ("left", "spin", "SLA")),
    ("Left-arm unorthodox spin", ("left", "spin", "LWS")),
    ("Left-arm wrist spin", ("left", "spin", "LWS")),
    ("", (None, None, None)),
])
def test_classify_bowling(style, expected):
    assert classify_bowling(style) == expected


def test_classify_batting_and_infobox():
    text = "{{Infobox cricketer\n| name = X\n| batting = Left-handed\n| bowling = {{ubl|Right-arm [[off break]]}}\n| role = x\n}}"
    fields = infobox_fields(text)
    assert fields == {"batting": "Left-handed", "bowling": "Right-arm off break"}
    assert classify_batting(fields["batting"]) == "left"
    assert classify_batting("") is None
