import pytest

from khoj.routers.helpers import parse_conversation_command
from khoj.utils.helpers import ConversationCommand


def test_explicit_conversation_command_matches_the_first_token_exactly():
    assert parse_conversation_command(" /notes read notes.md") == (
        ConversationCommand.Notes,
        "read notes.md",
        True,
    )
    with pytest.raises(ValueError, match="Unknown conversation command"):
        parse_conversation_command("/notes-extra read notes.md")
    assert parse_conversation_command("please use /notes literally") == (
        ConversationCommand.Default,
        "please use /notes literally",
        False,
    )


def test_default_command_is_explicit_and_removed_from_the_user_query():
    assert parse_conversation_command("/default explain this") == (
        ConversationCommand.Default,
        "explain this",
        True,
    )
