"""Shared full-screen forms, reviews and cooperative workflow execution."""
from __future__ import annotations

import json
import re
import signal
import subprocess
import threading
from collections.abc import Callable

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, Collapsible, Footer, Header, Input, Label, LoadingIndicator, OptionList, RichLog, Select, Static, TextArea
from textual.widgets.option_list import Option

from tools.interaction import OperationPaused, confirmation_prefix, using


class DialogScreen(ModalScreen):
    def compose(self) -> ComposeResult:
        steps = "\n\n".join(("> " if item == self.app.active_stage else "  ") + item for item in self.app.steps)
        self.set_class(self.app.size.width < 90, "compact")
        yield Header()
        yield Static("", id="dialog-size-warning", markup=False)
        yield Footer()
        with Horizontal(id="dialog-workspace"):
            yield Static(steps, id="dialog-steps", markup=False)
            yield from self.dialog()
        yield Static(self.app.activity_text, id="dialog-status", classes="activity-status", markup=False)

    def on_resize(self, event):
        self.set_class(event.size.width < 90, "compact")
        for warning in self.query("#dialog-size-warning"):
            warning.update("Enlarge the terminal to 80 × 24; Ctrl+Q still pauses safely."
                           if event.size.width < 80 or event.size.height < 24 else "")

    def context_banner(self):
        with VerticalScroll(classes="context-scroll"):
            yield Static(self.app.context_text, id="dialog-context", classes="context-banner", markup=False)

    def technical_details(self, details):
        if details:
            with Collapsible(title="Technical details — complete artifact", collapsed=True, id="technical-details"):
                yield TextArea(details, read_only=True, soft_wrap=True, id="technical-body")


class FormScreen(DialogScreen):
    BINDINGS = [Binding("escape", "back", "Back / pause")]

    def __init__(self, title, fields, *, description="", details="", submit="Continue", back=False):
        super().__init__()
        self.heading, self.fields = title, fields
        self.description, self.submit_label, self.allow_back = description, submit, back
        self.details = details
        self._suggestions = {}
        self._overrides = set()

    def dialog(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield from self.context_banner()
            yield Label(self.heading, classes="heading", markup=False)
            with VerticalScroll(id="form-fields"):
                if self.description:
                    yield Static(self.description, markup=False)
                for index, field in enumerate(self.fields):
                    identifier = f"field-{index}"
                    if field.kind == "bool":
                        yield Checkbox(Text(field.label), value=bool(field.default), id=identifier)
                    else:
                        yield Label(field.label, markup=False)
                        if field.kind == "select":
                            values = {value for _, value in field.choices}
                            selected = field.default if field.default in values else Select.BLANK
                            yield Select(field.choices, value=selected, allow_blank=True, id=identifier)
                        elif field.kind == "multiline":
                            yield TextArea(str(field.default or ""), soft_wrap=True, id=identifier, classes="multi-input")
                        else:
                            yield Input(value=str(field.default or ""), id=identifier)
                    if field.help:
                        yield Static(field.help, classes="field-help", markup=False)
                    yield Static("", id=f"error-{index}", classes="field-error", markup=False)
                yield from self.technical_details(self.details)
            with Horizontal(classes="buttons"):
                yield Button("Back" if self.allow_back else "Pause / quit", id="back")
                yield Button(self.submit_label, variant="primary", id="submit")
            yield Static("Tab / Shift+Tab: move focus • Space: toggle • Ctrl+Q: safe pause", classes="hint")

    def on_mount(self):
        for error in self.query(".field-error"):
            error.display = False
        for index, field in enumerate(self.fields):
            if field.default_from:
                suggestion = self._suggestion(field)
                self._suggestions[field.name] = suggestion
                control = self.query_one(f"#field-{index}", Input)
                if not control.value:
                    control.value = suggestion
                elif control.value != suggestion:
                    self._overrides.add(field.name)
        controls = self.query("Input, Select, Checkbox, TextArea")
        if controls:
            controls.first().focus()

    def action_back(self):
        if self.allow_back:
            self.dismiss(None)
        else:
            self.app.action_request_quit()

    def on_button_pressed(self, event: Button.Pressed):
        event.stop()
        if event.button.id == "back":
            self.action_back()
        elif event.button.id == "submit":
            self.submit()

    def on_input_submitted(self, event: Input.Submitted):
        event.stop()
        self.submit()

    def _suggestion(self, field):
        source_index = next(index for index, source in enumerate(self.fields)
                            if source.name == field.default_from)
        value = self.query_one(f"#field-{source_index}", Input).value.strip()
        return value if not field.pattern or re.fullmatch(field.pattern, value) else ""

    def on_input_changed(self, event: Input.Changed):
        for index, field in enumerate(self.fields):
            if field.name not in self._suggestions:
                continue
            source_index = next(index for index, source in enumerate(self.fields)
                                if source.name == field.default_from)
            if event.input.id != f"field-{source_index}":
                continue
            control = self.query_one(f"#field-{index}", Input)
            suggestion = self._suggestion(field)
            if control.value != self._suggestions[field.name]:
                self._overrides.add(field.name)
            if field.name not in self._overrides:
                control.value = suggestion
            self._suggestions[field.name] = suggestion

    def submit(self):
        result, invalid = {}, []
        for index, field in enumerate(self.fields):
            control = self.query_one(f"#field-{index}")
            value = control.text if isinstance(control, TextArea) else control.value
            if value is Select.BLANK:
                value = ""
            if isinstance(value, str):
                value = value.strip()
            message = ""
            if field.required and field.kind != "bool" and not value:
                message = "A value is required."
            elif isinstance(value, str) and any(ord(c) < 32 and c not in ("\n", "\t") for c in value):
                message = "Control characters are not allowed."
            elif isinstance(value, str) and value and field.pattern and not re.fullmatch(field.pattern, value):
                message = field.help or "This value does not match the required format."
            error = self.query_one(f"#error-{index}", Static)
            error.update(message)
            error.display = bool(message)
            if message:
                invalid.append(control)
            result[field.name] = value
        if invalid:
            invalid[0].focus()
        else:
            self.dismiss(result)


class ChooseScreen(DialogScreen):
    """Visible action list, including the local operation dashboard."""

    BINDINGS = [Binding("escape", "pause", "Pause / quit")]

    def __init__(self, title, choices, description=""):
        super().__init__()
        self.heading, self.choices, self.description = title, tuple(choices), description

    def dialog(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield from self.context_banner()
            yield Label(self.heading, classes="heading", markup=False)
            if self.description:
                yield TextArea(self.description, read_only=True, soft_wrap=True, id="choice-details")
            yield OptionList(*(Option(Text(label), id=f"option-{index}") for index, (label, _) in enumerate(self.choices)), id="choices")
            yield Static("Up / Down: select • Enter: open • Tab: review details • Ctrl+Q: safe pause", classes="hint")

    def on_mount(self):
        self.query_one("#choices", OptionList).focus()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected):
        event.stop()
        self.dismiss(self.choices[event.option_index][1])

    def action_pause(self):
        self.app.action_request_quit()


class ReviewScreen(DialogScreen):
    BINDINGS = [Binding("escape", "back", "Back / decline")]

    def __init__(self, title, body, *, identity=None, destructive=False, accept="Approve", back=False,
                 description="", details="", identity_label="Exact target"):
        super().__init__()
        self.heading, self.body, self.identity = title, body, identity
        self.prefix = confirmation_prefix(identity) if destructive else None
        self.accept_label, self.allow_back = accept, back
        self.description, self.details = description, details
        self.identity_label = identity_label

    def dialog(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield from self.context_banner()
            yield Label(self.heading, classes="heading", markup=False)
            with VerticalScroll(id="review-content"):
                if self.description:
                    yield Static(self.description, markup=False)
                yield TextArea(self.body, read_only=True, soft_wrap=True, id="review-body")
                yield from self.technical_details(self.details)
            if self.identity is not None:
                yield Static(self.identity_label + ": " + self.identity, markup=False, classes="target")
            if self.prefix is not None:
                yield Label(f"Type {self.prefix!r} to confirm this target (up to six characters):", markup=False)
                yield Input(id="confirmation")
                yield Static("", id="confirmation-error", classes="field-error", markup=False)
            with Horizontal(classes="buttons"):
                if self.allow_back:
                    yield Button("Back", id="back")
                yield Button("Decline", id="decline")
                yield Button(self.accept_label, variant="error" if self.prefix is not None else "primary", id="accept")
            yield Static("Review the full content above. Ctrl+Q pauses without approving.", classes="hint")

    def on_mount(self):
        self.query_one("#review-body").focus()

    def action_decline(self):
        self.dismiss(False)

    def action_back(self):
        self.dismiss(None if self.allow_back else False)

    def accept(self):
        if self.prefix is not None and self.query_one("#confirmation", Input).value != self.prefix:
            self.query_one("#confirmation-error", Static).update("The entry does not match this exact target's required prefix.")
            self.query_one("#confirmation", Input).focus()
            return
        self.dismiss(True)

    def on_button_pressed(self, event: Button.Pressed):
        event.stop()
        if event.button.id == "accept":
            self.accept()
        elif event.button.id == "decline":
            self.dismiss(False)
        elif event.button.id == "back":
            self.action_back()

    def on_input_submitted(self, event: Input.Submitted):
        event.stop()
        self.accept()


class Interaction:
    """Synchronous controller interface implemented using main-thread Textual screens."""

    def __init__(self, app):
        self.app = app
        self.pause_requested = threading.Event()

    def checkpoint(self):
        if self.pause_requested.is_set() or not self.app.is_running:
            raise OperationPaused("Paused at a safe checkpoint. Keep existing receipts and resume the same operation.")

    def _request(self, screen):
        self.checkpoint()
        answered = threading.Event()
        answer = []

        def completed(value):
            self.app.show_activity("working")
            answer.append(value)
            answered.set()

        def present():
            if not self.pause_requested.is_set():
                self.app.show_activity("input", screen.heading)
                self.app.push_screen(screen, completed)

        self.app.call_from_thread(present)
        while not answered.wait(0.1):
            self.checkpoint()
        self.checkpoint()
        return answer[0]

    def form(self, title, fields, *, description="", details="", submit="Continue", back=False):
        return self._request(FormScreen(title, fields, description=description, details=details, submit=submit, back=back))

    def choose(self, title, choices, *, description=""):
        return self._request(ChooseScreen(title, choices, description))

    def review(self, title, body, *, identity=None, destructive=False, accept="Approve", back=False,
               description="", details="", identity_label="Exact target"):
        return self._request(ReviewScreen(title, body, identity=identity, destructive=destructive,
                                         accept=accept, back=back, description=description, details=details,
                                         identity_label=identity_label))

    def confirm(self, prompt):
        return self.review("Confirm selection", prompt, accept="Confirm")

    def message(self, text):
        self.app.call_from_thread(self.app.show_message, str(text))

    def stage(self, name):
        self.checkpoint()
        self.app.call_from_thread(self.app.show_stage, str(name))

    def set_context(self, text):
        """Keep access purpose and selected identity visible across every screen."""
        self.app.call_from_thread(self.app.show_context, str(text))

    def external(self, command, **kwargs):
        self.checkpoint()
        # Authentication owns the terminal temporarily; its output is never retained.
        if any(key in kwargs for key in ("stdout", "stderr", "capture_output", "input")):
            raise ValueError("External authentication must use the terminal directly")
        self.app.call_from_thread(self.app.show_activity, "input", "External authentication")
        try:
            result = self.app.call_from_thread(self.app.external, command, kwargs)
        finally:
            self.app.call_from_thread(self.app.show_activity, "working")
        self.checkpoint()
        return result

    def close_when_finished(self):
        """Close only after this controller returns successfully."""
        self.app.call_from_thread(setattr, self.app, "close_on_finish", True)


class WorkflowApp(App):
    TITLE = "MAVEN"
    BINDINGS = [
        Binding("ctrl+q", "request_quit", "Pause / quit", priority=True),
        Binding("ctrl+c", "request_quit", "Safe pause", show=False, priority=True),
    ]
    CSS = """
    Screen { background: $surface; }
    #workspace { height: 1fr; }
    #steps { width: 25; border-right: solid $primary; padding: 1; overflow-y: auto; }
    #activity { width: 1fr; border: thick $primary; padding: 1 2; }
    Screen.compact #activity { border: solid $primary; padding: 0 1; }
    #events { height: 1fr; min-height: 3; background: $panel; }
    #activity-bar { height: auto; min-height: 1; margin-top: 1; }
    .activity-status { height: auto; width: 1fr; text-style: bold; }
    #busy { height: 1; width: 11; margin-right: 2; }
    #size-warning { height: auto; color: $warning; }
    .context-banner { height: auto; padding: 0 1; background: $boost; }
    .context-scroll { height: auto; max-height: 9; margin-bottom: 1; }
    Screen.compact .context-scroll { max-height: 4; margin-bottom: 0; }
    #dialog-workspace { height: 1fr; }
    Screen.compact #steps { display: none; }
    ModalScreen { background: $surface; }
    #dialog-steps { width: 25; height: 1fr; border-right: solid $primary; padding: 1; overflow-y: auto; }
    #dialog-size-warning { height: auto; color: $warning; }
    ModalScreen.compact #dialog-steps { display: none; }
    #dialog { width: 1fr; height: 1fr; border: thick $primary; background: $surface; padding: 1 2; }
    ModalScreen.compact #dialog { border: solid $primary; padding: 0 1; }
    ModalScreen.compact .heading { margin-bottom: 0; }
    ModalScreen.compact .buttons { margin-top: 0; }
    ModalScreen.compact .hint { display: none; }
    .heading { height: auto; text-style: bold; color: $accent; margin-bottom: 1; }
    #form-fields { height: 1fr; }
    #form-fields Label { height: auto; margin-top: 1; }
    #form-fields Input, #form-fields Select { margin-bottom: 0; }
    .multi-input { height: 6; min-height: 3; }
    .field-error { height: auto; color: $error; }
    .field-help { height: auto; color: $text-muted; }
    .buttons { height: auto; min-height: 3; align-horizontal: right; margin-top: 1; }
    .buttons Button { margin-left: 1; }
    .hint { height: auto; color: $text-muted; }
    .target { height: auto; margin: 1 0; }
    ModalScreen.compact .target { margin: 0; }
    #review-content { height: 1fr; }
    #technical-details { height: auto; }
    #technical-body { height: 12; min-height: 3; }
    #review-body { height: 1fr; min-height: 3; }
    #confirmation { height: 3; }
    #choice-details { height: 35%; min-height: 3; max-height: 12; }
    #choices { height: 1fr; }
    """

    def __init__(self, title: str, steps: list[str], operation: Callable):
        super().__init__()
        self.title = title
        self.steps = list(steps)
        self.active_stage = self.steps[0] if self.steps else "Ready"
        self.context_text = ""
        self.operation = operation
        self.interaction = Interaction(self)
        self.finished = False
        self.exit_code = 0
        self.close_on_finish = False
        self._signals = {}
        self.activity_state = "working"
        self.activity_text = "Waiting… " + self.active_stage
        self._main_screen = None

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("", id="size-warning")
        with Horizontal(id="workspace"):
            yield Static("", id="steps", markup=False)
            with Vertical(id="activity"):
                with VerticalScroll(classes="context-scroll"):
                    yield Static(self.context_text, id="context", classes="context-banner", markup=False)
                yield RichLog(id="events", wrap=True, markup=False, highlight=False, max_lines=2000)
                with Horizontal(id="activity-bar"):
                    yield LoadingIndicator(id="busy")
                    yield Static(self.activity_text, id="status", classes="activity-status", markup=False)
        yield Footer()

    def on_mount(self):
        self._main_screen = self.screen
        self.on_resize()
        if threading.current_thread() is threading.main_thread():
            self._signals = {number: signal.signal(number, self._signal_pause)
                             for number in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)}
        self.show_stage(self.active_stage)
        self.run_worker(self._execute, thread=True, exit_on_error=False)

    def _signal_pause(self, *_):
        self.interaction.pause_requested.set()
        self.call_later(self.action_request_quit)

    def on_unmount(self):
        self.interaction.pause_requested.set()
        for number, handler in self._signals.items():
            signal.signal(number, handler)

    def on_resize(self):
        if self._main_screen is None:
            return
        self._main_screen.set_class(self.size.width < 90, "compact")
        banner = self._main_screen.query_one("#size-warning", Static)
        banner.update("Terminal too small: enlarge to at least 80 columns × 24 rows. Ctrl+Q still pauses safely." if self.size.width < 80 or self.size.height < 24 else "")

    def show_stage(self, name):
        self.active_stage = name
        if name not in self.steps:
            self.steps.append(name)
        self._main_screen.query_one("#steps", Static).update("\n\n".join(("> " if item == name else "  ") + item for item in self.steps))
        self.show_activity(self.activity_state)
        self.show_message(name)

    def show_activity(self, state, detail=""):
        """Update lifecycle-driven status only on Textual's UI thread."""
        if self.interaction.pause_requested.is_set() and not self.finished:
            state = "pause_requested"
        self.activity_state = state
        if state == "working":
            text = "Waiting… " + self.active_stage
        elif state == "input":
            text = "Awaiting input — " + (detail or self.active_stage)
        elif state == "pause_requested":
            text = "Pause requested: waiting for a safe checkpoint. Submitted remote operations may continue."
        else:
            text = {"paused": "Paused", "failed": "Failed", "complete": "Complete"}[state]
            if detail:
                text += " — " + detail
        self.activity_text = text
        self._main_screen.query_one("#status", Static).update(text)
        self._main_screen.query_one("#busy").display = state in ("working", "pause_requested")
        for screen in self.screen_stack:
            for status in screen.query("#dialog-status"):
                status.update(text)

    def show_context(self, text):
        self.context_text = text
        for screen in self.screen_stack:
            for banner in screen.query(".context-banner"):
                banner.update(text)

    def show_message(self, text):
        self._main_screen.query_one("#events", RichLog).write(Text(text))

    def external(self, command, kwargs):
        try:
            with self.suspend():
                return subprocess.run(command, **kwargs)
        except KeyboardInterrupt:
            self.interaction.pause_requested.set()
            raise OperationPaused("External authentication interrupted; resume after completing your normal login.") from None

    def _execute(self):
        code, message = 0, "Completed. Review the results; Ctrl+Q closes this screen."
        with using(self.interaction):
            try:
                result = self.operation(self.interaction)
                if isinstance(result, int):
                    code = result
                elif isinstance(result, dict):
                    self.interaction.message(json.dumps(result, indent=2, ensure_ascii=False))
                    code = 130 if result.get("status") == "paused" else (2 if result.get("status") in ("blocked", "conflict") else 0)
                if code:
                    message = "Paused; preserve the receipts and resume." if code in (3, 130) else "Not complete. Review the diagnostics and preserved recovery state."
            except OperationPaused as error:
                code, message = 130, str(error)
            except Exception as error:
                # Raw SDK/subprocess exceptions can contain private request material.
                code, message = 2, f"Stopped ({type(error).__name__}). Preserve existing receipts; no success or rollback is implied."
            self.app_finish(code, message)

    def app_finish(self, code, message):
        if self.is_running:
            self.call_from_thread(self._finished, code, message)

    def _finished(self, code, message):
        while len(self.screen_stack) > 1:
            self.pop_screen()
        self.finished, self.exit_code = True, code
        state = "paused" if code in (3, 130) else ("failed" if code else "complete")
        self.show_activity(state, message)
        self.show_message(message)
        if self.interaction.pause_requested.is_set() or (self.close_on_finish and code == 0):
            self.exit(code)

    def action_request_quit(self):
        if self.finished:
            self.exit(self.exit_code)
            return
        self.interaction.pause_requested.set()
        self.show_activity("pause_requested")
        while len(self.screen_stack) > 1:
            self.pop_screen()
