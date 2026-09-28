"""An updated summary keeps what the previous one held.

Measured before this change (our compaction eval suite v2, Core
a0effacd): after four compactions a scripted conversation kept 4, 9 and 4 of
11 planted facts across three trials; a summary carrying 9 was followed by one
carrying 3. The previous summary reached the summariser only as optional
"previous context" after the new turns, next to an instruction to prioritise
recent context.
"""
from __future__ import annotations

from flowly.compaction.service import CompactionService
from flowly.compaction.summarizer import (
    PREVIOUS_RECORD_FENCE,
    SUMMARIZE_SYSTEM_PROMPT,
    SUMMARIZE_UPDATE_PROMPT,
    TRANSCRIPT_FENCE,
    extract_detail_anchors,
    generate_summary,
    missing_detail_anchors,
    summarize_in_stages,
)
from flowly.compaction.types import CompactionConfig, build_summary_content
from flowly.providers.base import LLMResponse

REPAIR_MARKER = "These specific details were in the previous record"

PREVIOUS = """## Decisions
- The owner's daughter Elif's birthday is 14 Kasım.
- Project ATLAS-7731, notes at https://notion.so/deniz/atlas-7731-yol-haritasi.
- Accountant Mert, mert@ornekmuhasebe.com. Monthly savings target 12.500 TL.
- Consulate appointment on 21 Ekim at 09:40.
## Most Recent Historical Request
The owner asked at 11:30 for a meeting summary."""

ALL_DETAILS = ("14 Kasım ATLAS-7731 https://notion.so/deniz/atlas-7731-yol-haritasi "
               "mert@ornekmuhasebe.com 12.500 TL 21 Ekim 09:40")


class _Scripted:
    """Answers summary calls with ``update`` and repair calls with ``repair``."""

    provider_name = "stub"

    def __init__(self, update: str, repair: str | LLMResponse = ""):
        self.update = update
        self.repair = repair
        self.calls: list[str] = []

    async def chat(self, *args, **kwargs) -> LLMResponse:
        messages = kwargs.get("messages") or (args[0] if args else [])
        prompt = "\n".join(str(m.get("content") or "") for m in messages if m.get("role") == "user")
        self.calls.append(prompt)
        if REPAIR_MARKER in prompt:
            if isinstance(self.repair, LLMResponse):
                return self.repair
            return LLMResponse(content=self.repair, finish_reason="stop")
        return LLMResponse(content=self.update, finish_reason="stop")

    @property
    def repairs(self) -> list[str]:
        return [c for c in self.calls if REPAIR_MARKER in c]


def _conversation(turns: int, filler: str = "word " * 60) -> list[dict]:
    messages: list[dict] = []
    for i in range(turns):
        messages.append({"role": "user", "content": f"question {i} {filler}"})
        messages.append({"role": "assistant", "content": f"answer {i} {filler}"})
    return messages


def _history(turns: int = 12) -> list[dict]:
    return [{"role": "system", "content": build_summary_content(PREVIOUS)}, *_conversation(turns)]


def _service(provider) -> CompactionService:
    return CompactionService(provider=provider, model="m", config=CompactionConfig(mode="default"))


# ── The previous summary is the record being updated ──────────────────────


async def test_an_update_puts_the_previous_record_first_with_a_carry_forward_rule():
    provider = _Scripted("## Decisions\nupdated")
    await generate_summary(_conversation(2), provider, "m", 2_000, previous_summary=PREVIOUS)
    prompt = provider.calls[0]
    assert PREVIOUS_RECORD_FENCE in prompt
    assert prompt.index(PREVIOUS_RECORD_FENCE) < prompt.index(TRANSCRIPT_FENCE)
    assert "ATLAS-7731" in prompt[: prompt.index(TRANSCRIPT_FENCE)]
    assert "Carry forward every fact from the previous record" in prompt


async def test_a_first_summary_has_no_previous_record():
    provider = _Scripted("## Decisions\nfirst")
    await generate_summary(_conversation(2), provider, "m", 2_000)
    assert PREVIOUS_RECORD_FENCE not in provider.calls[0]


def test_the_record_stays_compact_without_losing_facts():
    # A long run's summaries listed every day a request recurred ("161., 173.,
    # … 293. günlerde") and repeated facts across sections, growing each cycle
    # until summary calls hit their timeout.
    assert "Never list each occurrence" in SUMMARIZE_SYSTEM_PROMPT
    assert "State each fact once" in SUMMARIZE_SYSTEM_PROMPT
    assert "Shorten wording, never the facts above." in SUMMARIZE_SYSTEM_PROMPT


def test_compacting_finished_work_keeps_its_identifiers():
    # "Reduce finished work to one line" alone contradicts the Completed
    # Actions rule for a coding session: the paths, commits and errors are
    # what the next turn needs.
    rule = "Reduce finished work to one line, keeping what it changed and where"
    assert rule in SUMMARIZE_SYSTEM_PROMPT
    assert "commit ids, test counts, exact error messages and results stay" in SUMMARIZE_SYSTEM_PROMPT


def test_people_keep_their_relationship_to_the_owner():
    # Summaries kept "Elif's birthday is 14 Kasım" but never that Elif is the
    # owner's daughter, so "what is my daughter's name?" went unanswered.
    assert "Name every person with who they are to the owner" in SUMMARIZE_SYSTEM_PROMPT
    assert "with who they are to the owner" in SUMMARIZE_UPDATE_PROMPT


def test_the_system_prompt_no_longer_trades_old_facts_for_recent_context():
    assert "Prioritize recent context over older" not in SUMMARIZE_SYSTEM_PROMPT
    assert "never drop a fact that is still\ntrue because it is old" in SUMMARIZE_SYSTEM_PROMPT


async def test_staged_parts_are_merged_into_the_previous_record():
    provider = _Scripted("## Decisions\npart")
    await summarize_in_stages(
        _conversation(30), provider, "m", 2_000, max_chunk_tokens=1_500,
        context_window=100_000, previous_summary=PREVIOUS,
    )
    assert len(provider.calls) >= 3, "the history must have been split into parts"
    parts, merge = provider.calls[:-1], provider.calls[-1]
    # A part may chain its own chunks; the committed previous summary reaches
    # only the final merge, as the record being updated.
    assert all("ATLAS-7731" not in call for call in parts)
    record = merge[merge.index(PREVIOUS_RECORD_FENCE): merge.index(TRANSCRIPT_FENCE)]
    assert "ATLAS-7731" in record


# ── Detail anchors ─────────────────────────────────────────────────────────


def test_details_are_recognised_in_their_usual_forms():
    anchors = extract_detail_anchors(PREVIOUS)
    assert set(anchors.values()) >= {
        "14 Kasım", "ATLAS-7731", "https://notion.so/deniz/atlas-7731-yol-haritasi",
        "mert@ornekmuhasebe.com", "12.500", "21 Ekim", "09:40",
    }
    # The historical request is regenerated every time; its details may change.
    assert "11:30" not in anchors.values()


def test_equivalent_spellings_are_not_reported_missing():
    updated = "elif 14 kasım; atlas-7731 https://notion.so/deniz/atlas-7731-yol-haritasi. " \
              "Mert mert@ornekmuhasebe.com, 12500 TL. 21 ekim, 9.40."
    assert missing_detail_anchors(PREVIOUS, updated) == []


def test_a_dropped_detail_is_reported():
    updated = ALL_DETAILS.replace("ATLAS-7731 ", "").replace("09:40", "")
    assert missing_detail_anchors(PREVIOUS, updated) == ["ATLAS-7731", "09:40"]


def test_a_code_inside_a_link_does_not_count_as_the_code():
    updated = ALL_DETAILS.replace("ATLAS-7731 ", "")
    assert "atlas-7731" in updated  # still inside the notion link
    assert missing_detail_anchors(PREVIOUS, updated) == ["ATLAS-7731"]
    assert missing_detail_anchors(PREVIOUS, updated + " (code atlas-7731)") == []


def test_a_number_inside_a_longer_number_is_not_a_time():
    assert extract_detail_anchors("version 1.2.3 and 12.500 items") == {"amount:12500": "12.500"}


# ── The repair pass ────────────────────────────────────────────────────────


async def test_a_dropped_detail_is_repaired_in_one_pass():
    dropped = "## Decisions\n" + ALL_DETAILS.replace("ATLAS-7731 ", "")
    provider = _Scripted(update=dropped, repair="## Decisions\n" + ALL_DETAILS)
    result = await _service(provider).compact(_history())
    assert len(provider.repairs) == 1
    assert "- ATLAS-7731" in provider.repairs[0]
    assert "ATLAS-7731" in result.summary
    assert result.details_missing_before_repair == ["ATLAS-7731"]
    assert result.details_missing_after_repair == []


async def test_nothing_missing_means_no_repair_call():
    provider = _Scripted(update="## Decisions\n" + ALL_DETAILS)
    result = await _service(provider).compact(_history())
    assert provider.repairs == []
    assert result.details_missing_before_repair == []


async def test_the_code_never_puts_a_detail_back_itself():
    # The repair decided the detail no longer applies (or failed to restore
    # it): the summary goes on without it. A re-inserted "10:00" after the
    # owner moved meetings to 09:00 would be worse than the loss.
    dropped = "## Decisions\n" + ALL_DETAILS.replace("ATLAS-7731 ", "")
    provider = _Scripted(update=dropped, repair=dropped)
    result = await _service(provider).compact(_history())
    assert "ATLAS-7731" not in result.summary
    assert result.details_missing_after_repair == ["ATLAS-7731"]


async def test_a_failed_repair_keeps_the_update_and_the_compaction():
    dropped = "## Decisions\n" + ALL_DETAILS.replace("ATLAS-7731 ", "")
    provider = _Scripted(
        update=dropped,
        repair=LLMResponse(content="Error calling LLM: outage", finish_reason="error"),
    )
    result = await _service(provider).compact(_history())
    assert "ATLAS-7731" not in result.summary
    assert result.details_missing_before_repair == result.details_missing_after_repair == ["ATLAS-7731"]
    assert result.tokens_after < result.tokens_before


async def test_a_first_compaction_has_nothing_to_repair():
    provider = _Scripted(update="## Decisions\nfirst summary")
    result = await _service(provider).compact(_conversation(12))
    assert provider.repairs == []
    assert result.details_missing_before_repair == []


# ── Identifiers stay character for character ───────────────────────────────

CODING = """## Active State
- Branch feature/iade-akisi; changed src/kargo/iade.py and tests/test_iade.py.
- Commit 3f9c2a71; IADE_LIMIT_GUN defaults to 14; PR https://github.com/ornek/kargo/pull/318."""


def test_identifiers_are_details_too():
    anchors = set(extract_detail_anchors(CODING).values())
    assert anchors >= {"feature/iade-akisi", "src/kargo/iade.py", "tests/test_iade.py",
                       "3f9c2a71", "IADE_LIMIT_GUN", "https://github.com/ornek/kargo/pull/318"}
    # The path inside a link is the link, not a second identifier.
    assert "ornek/kargo/pull/318" not in anchors
    # Dates, versions and words are not identifiers.
    assert set(extract_detail_anchors("on 14/11, version 1.2.3, the 2020s, a decade").values()) == set()


def test_an_identifier_given_turkish_letters_is_reported_missing():
    # Observed: a summary in Turkish rewrote the branch as feature/iade-akışı.
    updated = CODING.replace("feature/iade-akisi", "feature/iade-akışı")
    assert missing_detail_anchors(CODING, updated) == ["feature/iade-akisi"]


def test_identifiers_keep_their_case():
    updated = CODING.replace("IADE_LIMIT_GUN", "iade_limit_gun").replace("src/kargo/iade.py", "src/Kargo/iade.py")
    assert missing_detail_anchors(CODING, updated) == ["src/kargo/iade.py", "IADE_LIMIT_GUN"]


def test_the_prompt_forbids_translating_identifiers():
    assert "never translate an identifier or give it Turkish letters" in SUMMARIZE_SYSTEM_PROMPT


async def test_an_altered_identifier_is_repaired():
    provider = _Scripted(update=CODING.replace("feature/iade-akisi", "feature/iade-akışı"), repair=CODING)
    history = [{"role": "system", "content": build_summary_content(CODING)}, *_conversation(12)]
    result = await _service(provider).compact(history)
    assert len(provider.repairs) == 1 and "- feature/iade-akisi" in provider.repairs[0]
    assert "feature/iade-akisi" in result.summary
    assert result.details_missing_after_repair == []


def test_identifiers_to_keep_are_the_ones_the_summary_lists_as_exact():
    # A coding summary names every file it read under tool results; the next
    # one rightly drops them. Only the Exact Identifiers section is binding.
    previous = """## Tool Results & Actions Taken
- Read src/kargo/log_4.py, src/kargo/lint_7.py and src/kargo/ci_10.py.
## Exact Identifiers
- feature/iade-akisi, src/kargo/iade.py, 3f9c2a71, IADE_LIMIT_GUN"""
    updated = """## Tool Results & Actions Taken
- Routine reads of lint and CI helpers.
## Exact Identifiers
- feature/iade-akisi, src/kargo/iade.py, 3f9c2a71, IADE_LIMIT_GUN"""
    assert missing_detail_anchors(previous, updated) == []
    assert missing_detail_anchors(previous, updated.replace("feature/iade-akisi", "feature/iade-akışı")) == [
        "feature/iade-akisi"]

