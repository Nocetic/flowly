from __future__ import annotations

import pytest

from flowly.tui.panes.composer import (
    Composer,
    InlineSecretPrompt,
    InlineSecretPromptRequest,
    InlineSetupField,
    InlineSetupFieldRow,
    InlineSetupPrompt,
    InlineSetupPromptRequest,
    LOCAL_SLASH_COMMANDS,
    _Editor,
    _filter_slash_palette,
    _merge_slash_palette,
    _normalize_editor_key,
    approval_decision_for_key,
)


class _FakeKey:
    def __init__(self, key: str) -> None:
        self.key = key
        self.stopped = False
        self.default_prevented = False

    def stop(self) -> None:
        self.stopped = True

    def prevent_default(self) -> None:
        self.default_prevented = True


@pytest.mark.parametrize(
    "key",
    ["shift+enter", "shift_enter", "shift+return", "alt+enter", "ctrl+j", "newline"],
)
def test_editor_modified_enter_inserts_newline_without_submit(key: str) -> None:
    editor = _Editor()
    editor.text = "first"
    editor.move_cursor((0, len(editor.text)))
    messages: list[object] = []
    editor.post_message = messages.append  # type: ignore[method-assign]

    event = _FakeKey(key)
    editor.on_key(event)  # type: ignore[arg-type]

    assert event.stopped is True
    assert event.default_prevented is True
    assert editor.text == "first\n"
    assert not any(isinstance(message, _Editor.Submit) for message in messages)


def test_editor_plain_enter_submits_current_text() -> None:
    editor = _Editor()
    editor.text = "send me"
    messages: list[object] = []
    editor.post_message = messages.append  # type: ignore[method-assign]

    event = _FakeKey("enter")
    editor.on_key(event)  # type: ignore[arg-type]

    assert event.stopped is True
    assert event.default_prevented is True
    submit = next(message for message in messages if isinstance(message, _Editor.Submit))
    assert submit.text == "send me"


def test_editor_ctrl_a_routes_to_subagent_toggle(monkeypatch) -> None:
    editor = _Editor()
    actions = []

    class _FakeApp:
        def run_action(self, action: str) -> bool:
            actions.append(action)
            return True

    monkeypatch.setattr(_Editor, "app", property(lambda self: _FakeApp()))

    event = _FakeKey("ctrl+a")
    editor.on_key(event)  # type: ignore[arg-type]

    assert event.stopped is True
    assert event.default_prevented is True
    assert actions == ["toggle_subagents"]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("", ""),
        ("\n", ""),
        ("   \n\t", ""),
        ("hello", "hello"),
        ("hello ./src", "./src"),
    ],
)
def test_composer_completion_token_ignores_whitespace_only_text(
    text: str,
    expected: str,
) -> None:
    assert Composer._last_completion_token(text) == expected


def test_slash_palette_keeps_core_commands_before_dynamic_skills() -> None:
    palette = _merge_slash_palette(
        [
            ("/sprint-planning", "Plan a sprint  (skill)"),
            ("/help", "Remote duplicate should not replace local help"),
            ("/workspace-lint", "Run workspace lint  (plugin)"),
        ]
    )

    local_names = [name for name, _desc in LOCAL_SLASH_COMMANDS]
    palette_names = [name for name, _desc in palette]

    assert palette_names[: len(local_names)] == local_names
    assert palette_names.count("/help") == 1
    assert palette_names.index("/sprint-planning") > palette_names.index("/quit")


def test_slash_palette_filter_preserves_core_order_for_prefix_matches() -> None:
    palette = _merge_slash_palette(
        [
            ("/sprint-planning", "Plan a sprint  (skill)"),
            ("/security-audit", "Audit security  (skill)"),
        ]
    )

    matches = [name for name, _desc in _filter_slash_palette(palette, "/s")]

    assert matches.index("/status") < matches.index("/sprint-planning")
    assert matches.index("/sessions") < matches.index("/security-audit")
    assert matches.index("/skills") < matches.index("/security-audit")


@pytest.mark.parametrize(
    ("text", "expected_height"),
    [
        ("one", 1),
        ("one\ntwo", 2),
        ("\n".join(str(i) for i in range(10)), 10),
        ("\n".join(str(i) for i in range(12)), 10),
    ],
)
def test_composer_input_height_tracks_explicit_lines_up_to_ten(
    text: str,
    expected_height: int,
) -> None:
    composer = Composer()
    editor = _Editor()
    editor.text = text

    composer._resize_editor_for_content(editor)

    assert editor.styles.height.value == expected_height


@pytest.mark.parametrize(
    ("key", "expected"),
    [
        ("escape", "deny"),
        ("ctrl+c", "deny"),
        ("1", "allow-once"),
        ("a", "allow-once"),
        ("2", "allow-always"),
        ("s", "allow-always"),
        ("3", "deny"),
        ("d", "deny"),
        ("down", None),
    ],
)
def test_approval_direct_shortcut_keys(key: str, expected: str | None) -> None:
    assert approval_decision_for_key(key) == expected


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("option+delete", "alt+delete"),
        ("cmd+delete", "super+delete"),
        ("command+backspace", "super+backspace"),
        ("ctrl_shift_backspace", "ctrl+shift+backspace"),
        ("ctrl+meta+h", "ctrl+meta+h"),
        ("super+delete_left", "super+backspace"),
    ],
)
def test_editor_key_normalization(raw: str, expected: str) -> None:
    assert _normalize_editor_key(raw) == expected


@pytest.mark.parametrize("key", ["option+delete", "alt+backspace", "ctrl+backspace"])
def test_editor_delete_word_left_shortcuts(key: str) -> None:
    editor = _Editor()
    editor.text = "hello world"
    editor.move_cursor((0, len(editor.text)))

    event = _FakeKey(key)
    editor.on_key(event)  # type: ignore[arg-type]

    assert event.stopped is True
    assert event.default_prevented is True
    assert editor.text == "hello "


@pytest.mark.parametrize(
    "key",
    [
        "command+delete",
        "command+backspace",
        "cmd+delete",
        "super+backspace",
        "shift+super+delete",
        "shift+super+backspace",
        "ctrl+super+h",
        "meta+delete",
        "meta+backspace",
        "meta+shift+delete",
        "meta+shift+backspace",
        "ctrl+meta+h",
        "ctrl+u",
    ],
)
def test_editor_delete_to_start_of_line_shortcuts(key: str) -> None:
    editor = _Editor()
    editor.text = "first line\nsecond line"
    editor.move_cursor((1, len("second line")))

    event = _FakeKey(key)
    editor.on_key(event)  # type: ignore[arg-type]

    assert event.stopped is True
    assert event.default_prevented is True
    assert editor.text == "first line\n"


@pytest.mark.asyncio
async def test_composer_input_grows_for_soft_wrapped_text() -> None:
    from textual.app import App

    class _Host(App):
        def compose(self):
            yield Composer(id="composer")

    app = _Host()
    async with app.run_test(size=(50, 20)) as pilot:
        await pilot.pause()
        composer = app.query_one(Composer)
        editor = composer.query_one("#composer-input", _Editor)
        editor.text = "x" * 160
        composer._resize_editor_for_content(editor)
        await pilot.pause()

        height = editor.styles.height
        assert height is not None
        assert int(height.value) > 1


@pytest.mark.asyncio
async def test_inline_secret_prompt_mounts_and_submits() -> None:
    from textual.app import App
    from textual.widgets import Input

    submitted: list[str] = []

    class _Host(App):
        def compose(self):
            yield Composer(id="composer")

        def on_mount(self) -> None:
            self.query_one(Composer).show_secret_prompt(
                InlineSecretPromptRequest(
                    title="Configure OpenAI",
                    label="API key for OpenAI",
                    placeholder="sk-...",
                )
            )

        def on_inline_secret_prompt_submitted(
            self,
            event: InlineSecretPrompt.Submitted,
        ) -> None:
            submitted.append(event.value)

    app = _Host()
    async with app.run_test() as pilot:
        await pilot.pause()
        composer = app.query_one(Composer)
        assert composer.has_class("secret-open")
        inp = composer.query_one("#secret-value", Input)
        inp.value = "sk-test"
        await inp.action_submit()
        await pilot.pause()
        assert submitted == ["sk-test"]


@pytest.mark.asyncio
async def test_inline_secret_prompt_escape_cancels() -> None:
    from textual.app import App

    cancelled: list[bool] = []

    class _Host(App):
        def compose(self):
            yield Composer(id="composer")

        def on_mount(self) -> None:
            self.query_one(Composer).show_secret_prompt(
                InlineSecretPromptRequest(title="Configure", label="Secret")
            )

        def on_inline_secret_prompt_cancelled(
            self,
            event: InlineSecretPrompt.Cancelled,
        ) -> None:
            cancelled.append(True)

    app = _Host()
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        assert cancelled == [True]


@pytest.mark.asyncio
async def test_inline_setup_prompt_collects_staged_values() -> None:
    from textual.app import App
    from textual.widgets import Input

    submitted: list[dict[str, object]] = []

    class _Host(App):
        def compose(self):
            yield Composer(id="composer")

        def on_mount(self) -> None:
            self.query_one(Composer).show_setup_prompt(
                InlineSetupPromptRequest(
                    title="Configure Telegram",
                    fields=[
                        InlineSetupField("token", "Bot token", "password", required=True),
                        InlineSetupField(
                            "dm_policy",
                            "DM policy",
                            "select",
                            choices=[
                                ("open", "open"),
                                ("pairing", "pairing"),
                            ],
                        ),
                    ],
                )
            )

        def on_inline_setup_prompt_submitted(
            self,
            event: InlineSetupPrompt.Submitted,
        ) -> None:
            submitted.append(event.values)

    app = _Host()
    async with app.run_test() as pilot:
        await pilot.pause()
        composer = app.query_one(Composer)
        assert composer.has_class("setup-open")
        inp = composer.query_one("#setup-value", Input)
        inp.value = "123:abc"
        await inp.action_submit()
        await pilot.pause()
        await pilot.press("down", "enter")
        await pilot.pause()
        assert submitted == [{"token": "123:abc", "dm_policy": "pairing"}]


@pytest.mark.asyncio
async def test_inline_setup_prompt_shows_staged_field_rows() -> None:
    from textual.app import App
    from textual.widgets import Input

    class _Host(App):
        def compose(self):
            yield Composer(id="composer")

        def on_mount(self) -> None:
            self.query_one(Composer).show_setup_prompt(
                InlineSetupPromptRequest(
                    title="Configure Telegram",
                    fields=[
                        InlineSetupField("token", "Bot token", "password", required=True),
                        InlineSetupField(
                            "dm_policy",
                            "DM policy",
                            "select",
                            choices=[("open", "open"), ("pairing", "pairing")],
                        ),
                    ],
                )
            )

    app = _Host()
    async with app.run_test() as pilot:
        await pilot.pause()
        composer = app.query_one(Composer)
        rows = list(composer.query(InlineSetupFieldRow))
        assert rows[0].display is True
        assert rows[1].display is True
        assert rows[0].has_class("selected")
        assert rows[1].has_class("empty")

        inp = composer.query_one("#setup-value", Input)
        inp.value = "123:abc"
        await inp.action_submit()
        await pilot.pause()

        assert rows[0].has_class("complete")
        assert rows[1].has_class("selected")


@pytest.mark.asyncio
async def test_inline_setup_prompt_escape_backs_out_of_select() -> None:
    from textual.app import App
    from textual.widgets import Input

    class _Host(App):
        def compose(self):
            yield Composer(id="composer")

        def on_mount(self) -> None:
            self.query_one(Composer).show_setup_prompt(
                InlineSetupPromptRequest(
                    title="Configure",
                    fields=[
                        InlineSetupField("name", "Name", "text"),
                        InlineSetupField(
                            "mode",
                            "Mode",
                            "select",
                            choices=[("a", "A"), ("b", "B")],
                        ),
                    ],
                )
            )

    app = _Host()
    async with app.run_test() as pilot:
        await pilot.pause()
        composer = app.query_one(Composer)
        inp = composer.query_one("#setup-value", Input)
        inp.value = "first"
        await inp.action_submit()
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        assert composer.query_one("#setup-value", Input).value == "first"


@pytest.mark.asyncio
async def test_empty_draft_down_selects_chat_artifact_and_enter_opens() -> None:
    from textual import on
    from textual.app import App
    from textual.widgets import Static

    opened: list[dict[str, object]] = []

    class _Host(App):
        def compose(self):
            yield Composer(id="composer")

        def on_mount(self) -> None:
            composer = self.query_one(Composer)
            composer.set_artifacts(
                [
                    {
                        "id": "art-1",
                        "type": "markdown",
                        "title": "Session report",
                        "updated_at": 1.0,
                    }
                ]
            )
            composer.focus_input()

        @on(Composer.ArtifactOpen)
        def capture_artifact(self, event: Composer.ArtifactOpen) -> None:
            opened.append(event.artifact)

    app = _Host()
    async with app.run_test(size=(100, 24)) as pilot:
        await pilot.pause()
        composer = app.query_one(Composer)
        hint = composer.query_one("#composer-hint", Static)
        assert "1 artifact" in str(hint.render())

        await pilot.press("down")
        await pilot.pause()
        assert composer.artifact_navigation_active() is True
        assert "1/1" in str(hint.render())

        await pilot.press("enter")
        await pilot.pause()

    assert opened == [
        {
            "id": "art-1",
            "type": "markdown",
            "title": "Session report",
            "updated_at": 1.0,
        }
    ]


@pytest.mark.asyncio
async def test_down_does_not_select_artifact_when_draft_has_text() -> None:
    from textual.app import App

    class _Host(App):
        def compose(self):
            yield Composer(id="composer")

        def on_mount(self) -> None:
            composer = self.query_one(Composer)
            composer.set_artifacts(
                [{"id": "art-1", "type": "markdown", "title": "Report"}]
            )
            editor = composer.query_one("#composer-input", _Editor)
            editor.text = "draft"
            editor.focus()

    app = _Host()
    async with app.run_test() as pilot:
        await pilot.pause()
        composer = app.query_one(Composer)
        await pilot.press("down")
        await pilot.pause()
        assert composer.artifact_navigation_active() is False


@pytest.mark.asyncio
async def test_artifact_hint_compacts_on_narrow_terminal() -> None:
    from textual.app import App
    from textual.widgets import Static

    class _Host(App):
        def compose(self):
            yield Composer(id="composer")

        def on_mount(self) -> None:
            self.query_one(Composer).set_artifacts(
                [
                    {
                        "id": "art-1",
                        "type": "markdown",
                        "title": "A very long artifact title that cannot fit",
                    }
                ]
            )

    app = _Host()
    async with app.run_test(size=(50, 20)) as pilot:
        await pilot.pause()
        hint = app.query_one("#composer-hint", Static)
        rendered = str(hint.render())
        assert "commands" not in rendered
        assert "A very long artifact title that cannot fit" not in rendered


@pytest.mark.asyncio
async def test_artifact_selection_flips_hint_background_class() -> None:
    from textual.app import App
    from textual.widgets import Static

    class _Host(App):
        def compose(self):
            yield Composer(id="composer")

        def on_mount(self) -> None:
            composer = self.query_one(Composer)
            composer.set_artifacts(
                [
                    {"id": "art-1", "title": "Report", "updated_at": 2.0},
                    {"id": "art-2", "title": "Chart", "updated_at": 1.0},
                ]
            )
            composer.focus_input()

    app = _Host()
    async with app.run_test(size=(100, 24)) as pilot:
        await pilot.pause()
        composer = app.query_one(Composer)
        assert composer.has_class("artifact-nav") is False

        await pilot.press("down")
        await pilot.pause()
        assert composer.has_class("artifact-nav") is True
        hint = str(composer.query_one("#composer-hint", Static).render())
        assert "artifacts 1/2" in hint
        assert "markdown" not in hint  # type names stay out of the hint

        await pilot.press("escape")
        await pilot.pause()
        assert composer.has_class("artifact-nav") is False
