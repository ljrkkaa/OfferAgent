from khoj.processor.conversation.offeragent_intent_router import parse_route_decision


def test_route_decision_preserves_explicit_write_outcome():
    decision = parse_route_decision(
        {
            "route": "default",
            "intent": "create_daily_plan",
            "requires_vault_write": True,
            "needs_clarification": False,
            "question": "",
        }
    )

    assert decision.needs_clarification is False
    assert decision.requires_vault_write is True


def test_write_outcome_cannot_route_away_from_reviewed_main_planner():
    decision = parse_route_decision(
        {
            "route": "research",
            "intent": "research_and_save",
            "requires_vault_write": True,
            "needs_clarification": False,
            "question": "",
        }
    )

    assert decision.needs_clarification is True
    assert decision.requires_vault_write is False


def test_removed_code_route_requires_clarification():
    decision = parse_route_decision(
        {
            "route": "code",
            "intent": "generate_csv_report",
            "requires_vault_write": False,
            "needs_clarification": False,
            "question": "",
        }
    )

    assert decision.route == "default"
    assert decision.requires_vault_write is False
    assert decision.needs_clarification is True


def test_removed_diagram_route_requires_clarification():
    decision = parse_route_decision(
        {
            "route": "diagram",
            "intent": "visualize_agent_flow",
            "requires_vault_write": False,
            "needs_clarification": False,
            "question": "",
        }
    )

    assert decision.route == "default"
    assert decision.needs_clarification is True
    assert decision.question


def test_removed_overlapping_route_requires_clarification_instead_of_defaulting():
    decision = parse_route_decision(
        {
            "route": "notes",
            "intent": "read_notes",
            "requires_vault_write": False,
            "needs_clarification": False,
            "question": "",
        }
    )

    assert decision.route == "default"
    assert decision.needs_clarification is True
    assert decision.question


def test_invalid_or_incomplete_route_requires_clarification():
    malformed = parse_route_decision("not-json")
    missing_question = parse_route_decision(
        {
            "route": "default",
            "intent": "ambiguous_request",
            "requires_vault_write": False,
            "needs_clarification": True,
            "question": "",
        }
    )

    assert malformed.needs_clarification is True
    assert missing_question.needs_clarification is True
    assert malformed.question
    assert missing_question.question
