import pytest

from khoj.routers.helpers import parse_summary_command


def test_summary_command_matches_only_the_first_token():
    assert parse_summary_command(" /summarize focus on decisions") == ("focus on decisions", True)
    assert parse_summary_command("mention /summarize literally") == ("mention /summarize literally", False)
    assert parse_summary_command("/summarize") == ("Create a general summary of the selected files.", True)


@pytest.mark.parametrize(
    "query",
    ["/default hi", "/general hi", "/notes hi", "/online hi", "/webpage hi", "/research hi"],
)
def test_removed_chat_modes_are_unknown(query):
    with pytest.raises(ValueError, match="Unknown conversation command"):
        parse_summary_command(query)
