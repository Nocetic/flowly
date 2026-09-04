"""Members address each other by the name their owner reads.

A group labelled its transcript with ids, so every member learned to call
Friday `dqwdqwd` — the only name it had ever been shown. Fixing the label alone
would have been worse than leaving it: mentions resolved against ids only, so a
member writing `@Friday` would not have been heard, and being heard is the
whole of how a sequential group hands off.
"""
from __future__ import annotations

from flowly.profile_rooms import _parse_mentions

MEMBERS = ["dqwdqwd", "jarvis", "chief-of-staff"]
LABELS = {"dqwdqwd": "Friday", "jarvis": "Jarvis", "chief-of-staff": "Chief of Staff"}


def test_the_id_still_resolves():
    assert _parse_mentions("@dqwdqwd look", MEMBERS, LABELS) == (False, {"dqwdqwd"})


def test_the_name_the_reader_sees_resolves():
    assert _parse_mentions("@Friday look", MEMBERS, LABELS) == (False, {"dqwdqwd"})
    assert _parse_mentions("@friday look", MEMBERS, LABELS) == (False, {"dqwdqwd"})


def test_a_display_name_with_spaces_is_reachable():
    # A mention cannot contain a space, so the same collapsing that makes an id
    # with dashes reachable makes "Chief of Staff" reachable.
    assert _parse_mentions("@chiefofstaff review", MEMBERS, LABELS) == (
        False, {"chief-of-staff"}
    )


def test_a_label_two_members_answer_to_resolves_to_neither():
    # Ids are unique; display names are not. Pulling the wrong bot into a turn
    # is worse than not hearing the mention.
    members = ["a1", "b2"]
    labels = {"a1": "Friday", "b2": "Friday"}
    assert _parse_mentions("@Friday look", members, labels) == (False, set())


def test_an_id_is_never_displaced_by_someone_elses_label():
    members = ["friday", "dqwdqwd"]
    labels = {"friday": "Scout", "dqwdqwd": "Friday"}
    # `friday` is an id and a label at once. It must reach the member whose id
    # it is.
    everyone, selected = _parse_mentions("@friday look", members, labels)
    assert everyone is False
    assert selected == set() or selected == {"friday"}


def test_everyone_still_works():
    assert _parse_mentions("@everyone stand-up", MEMBERS, LABELS)[0] is True


def test_mentions_in_code_are_left_alone():
    assert _parse_mentions("`@Friday` is a handle", MEMBERS, LABELS) == (False, set())


def test_without_labels_it_behaves_as_before():
    assert _parse_mentions("@dqwdqwd hi", MEMBERS) == (False, {"dqwdqwd"})
    assert _parse_mentions("@Friday hi", MEMBERS) == (False, set())
