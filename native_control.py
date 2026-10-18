"""Explicit, identity-checked native Codex actions for the companion service."""
from __future__ import annotations
import hashlib
import json
import time
import codex_app as app


def signature(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:24]


def document(window):
    control = window.control.DocumentControl(searchDepth=18, AutomationId='RootWebArea')
    if not control.Exists(0, 0):
        return []
    return list(app.control_nodes(control))


def prompt(window, *, observed_view=None):
    # Only an immediate read-only service observation supplies its View.
    # Every action calls prompt(window), obtaining a fresh native read.
    view = observed_view if observed_view is not None else app.read_text(window)
    if view is None or not view.screen.identified or not view.thread_hint:
        return None
    if view.approval_button is not None:
        actions = view.approval_button.GetParentControl()
        buttons = [c for c in actions.GetChildren() if c.ControlTypeName == 'ButtonControl' and not c.IsOffscreen and c.IsEnabled]
        reject = next((c for c in buttons if c.Name in {'拒绝', 'Decline', 'Deny', 'Reject'}), None)
        if reject is None:
            return None
        card = actions.GetParentControl()
        text = '\n'.join(c.Name for c in app.walk(card) if c.ControlTypeName == 'TextControl' and c.Name)
        choices = [(view.approval_button, '允许一次', False), (reject, '拒绝', False)]
        kind, submit, entry, back = 'perm', None, None, None
    elif getattr(view, 'question_card', None) is not None:
        card = view.question_card
        inside = list(app.walk(card))
        choices = [(c, c.Name, False) for c in inside if c.ControlTypeName == 'RadioButtonControl'
                   and c.Name and not c.IsOffscreen and c.IsEnabled]
        entries = [c for c in inside if c.ControlTypeName == 'EditControl' and c.IsEnabled and not c.IsOffscreen]
        entry = entries[0] if len(entries) == 1 else None
        if entry is not None:
            choices.append((entry, '语音回答', None))
        if not choices or len(choices) > 6 or len(entries) > 1:
            return None
        # Native choices answer directly; a text answer reveals its Send button
        # after input. Keep the actual question separate from answer labels.
        text = next((c.Name for c in inside if c.ControlTypeName == 'TextControl' and c.Name), '')
        direct = card.GetChildren()
        if (len(direct) >= 3 and direct[0].ControlTypeName == 'TextControl'
                and direct[0].Name in {'问题', 'Question'}
                and direct[1].ControlTypeName == 'ButtonControl'
                and direct[1].Name in {'忽略', 'Dismiss'}
                and direct[2].ControlTypeName == 'TextControl' and direct[2].Name):
            # The current native card adds a chrome header before the actual
            # question. Its captured direct-child structure distinguishes it
            # from a question whose text itself happens to be "问题".
            text = direct[2].Name
        submits = [c for c in inside if c.ControlTypeName == 'ButtonControl' and c.IsEnabled and not c.IsOffscreen
                   and c.Name in {'发送', 'Send', '提交答案', 'Submit answers', '提交', 'Submit'}]
        submit = submits[0] if len(submits) == 1 else None
        backs = [c for c in inside if c.ControlTypeName == 'ButtonControl' and c.Name in {'上一步', 'Previous', 'Back'}
                 and c.IsEnabled and not c.IsOffscreen]
        back = backs[0] if len(backs) == 1 else None
        kind = 'ask'
    else:
        nodes = getattr(view, 'controls', None)
        if nodes is None:
            nodes = document(window)
        # A question form must have its own submit button and choices, outside
        # menus. Arbitrary buttons in conversation prose are never options.
        if any(c.ControlTypeName == 'MenuControl' and not c.IsOffscreen for c in nodes):
            return None
        forms = []
        for button in nodes:
            if button.ControlTypeName != 'ButtonControl' or button.Name not in {'提交答案', 'Submit answers', '提交', 'Submit', '下一步', 'Next', '继续', 'Continue'} or button.IsOffscreen:
                continue
            card = button.GetParentControl()
            for _ in range(4):
                inside = list(app.walk(card))
                choices = [(c, c.Name, c.ControlTypeName == 'CheckBoxControl') for c in inside
                           if c.ControlTypeName in {'RadioButtonControl', 'CheckBoxControl'} and c.Name and not c.IsOffscreen and c.IsEnabled]
                edits=[c for c in inside if c.ControlTypeName=='EditControl' and c.Name not in app._EMPTY and not c.IsOffscreen and c.IsEnabled and not getattr(c,'IsPassword',False)]
                if choices or len(edits)==1:
                    forms.append((card, choices, button, edits))
                    break
                card = card.GetParentControl()
        unique = {tuple(card.GetRuntimeId()): (card, choices, button, edits) for card, choices, button, edits in forms}
        if len(unique) != 1:
            return None
        card, choices, submit, edits = next(iter(unique.values()))
        if len(edits)>1:
            return None
        entry=edits[0] if edits else None
        if entry is not None:
            choices.append((entry,'语音回答',None))
        back_buttons=[c for c in app.walk(card) if c.ControlTypeName=='ButtonControl' and c.Name in {'上一步','Previous','Back'} and c.IsEnabled and not c.IsOffscreen]
        back=back_buttons[0] if len(back_buttons)==1 else None
        text = '\n'.join(c.Name for c in app.walk(card) if c.ControlTypeName in {'TextControl', 'HeadingControl'} and c.Name)
        if not text or len(choices) > 6:
            return None
        kind = 'ask'
    sig = signature([view.identity, text, [(tuple(c.GetRuntimeId()), label) for c, label, _ in choices]])
    return {'sig': sig, 'kind': kind, 'text': text, 'choices': choices, 'submit': submit, 'entry':entry,'back':back,'identity': view.identity}


def public_prompt(value):
    if value is None:
        return None
    return {'sig': value['sig'], 'kind': value['kind'], 'text': value['text'],
            'options': [{'label': label, 'check': bool(getattr(c.GetTogglePattern(), 'ToggleState', 0)) if multi else None, 'input':multi is None}
                        for c, label, multi in value['choices']]}


def _read_stable(window):
    """Wait through a failed UIA read before any input, never retry an action."""
    view = app.read_text(window)
    until = time.monotonic() + 1
    while view is None and time.monotonic() < until:
        time.sleep(.05)
        view = app.read_text(window)
    return view


def _prompt_after_focus(window, identity, hint, prompt_sig):
    """Re-read a remount after focus; never repeat focus, text or submission.

Electron can invalidate UIA controls while focusing a new question's input.
A missing read is not a new target. A positively changed identity, mode or
prompt is rejected immediately; only an exact recovered card allows input.
"""
    until = time.monotonic() + 1
    while True:
        view = app.read_text(window)
        if view is not None:
            if (not view.screen.identified or
                    view.identity and view.identity != identity or
                    view.thread_hint and view.thread_hint != hint):
                return None
            if view.identity == identity and view.thread_hint == hint:
                try:
                    value = prompt(window)
                except Exception:
                    # This is a read-only UIA traversal; no write is retried.
                    value = None
                if value is not None:
                    return value if value['sig'] == prompt_sig and value['identity'] == identity else None
        if time.monotonic() >= until:
            return None
        time.sleep(.05)


def _commit_native_choice(window, identity, hint, prompt_sig, n, label):
    """SelectionItem only highlights Electron radio choices; Enter commits.

    Keep keyboard input bound to the exact foreground HWND and focused option.
    Never retry activation or redirect Enter to the current working window.
    """
    user = app.ctypes.windll.user32
    previous = user.GetForegroundWindow()
    try:
        value = prompt(window)
        if value is None or value['sig'] != prompt_sig or not 1 <= n <= len(value['choices']):
            return False
        control, current_label, multi = value['choices'][n - 1]
        if current_label != label or multi is not False or not control.IsEnabled or control.IsOffscreen:
            return False
        selected = getattr(control, 'GetSelectionItemPattern', lambda: None)()
        if selected is None or not selected.IsSelected:
            return False
        if previous != window.hwnd:
            user.SetForegroundWindow(app.wintypes.HWND(window.hwnd))
        if user.GetForegroundWindow() != window.hwnd:
            return False
        control.SetFocus()
        fresh = _prompt_after_focus(window, identity, hint, prompt_sig)
        if fresh is None or fresh['sig'] != prompt_sig:
            return False
        focused = app.auto.GetFocusedControl()
        if (user.GetForegroundWindow() != window.hwnd or focused.Name != label or
                tuple(focused.GetRuntimeId()) != tuple(control.GetRuntimeId()) or
                not selected.IsSelected):
            return False
        # Control.SendKeys would focus again after the checks. Send only the
        # guarded Enter, without changing focus or replaying a selection.
        app.auto.SendKeys('{Enter}', waitTime=0)
        return True
    except Exception:
        return False
    finally:
        if previous and previous != window.hwnd and user.GetForegroundWindow() == window.hwnd:
            user.SetForegroundWindow(app.wintypes.HWND(previous))


def _focus_stop_and_invoke(window, identity, hint, button):
    """Focus Electron's background Stop, then invoke that exact control once."""
    user = app.ctypes.windll.user32
    user.GetForegroundWindow.restype = app.wintypes.HWND
    previous = user.GetForegroundWindow()
    try:
        runtime_id = tuple(button.GetRuntimeId())
        button.SetFocus()
        fresh = _read_stable(window)
        if (fresh is None or not fresh.screen.identified or not fresh.screen.running or
                fresh.identity != identity or fresh.thread_hint != hint):
            return False
        nodes = getattr(fresh, 'controls', None)
        if nodes is None:
            nodes = document(window)
        matches = [c for c in nodes if c.ControlTypeName == 'ButtonControl'
                   and c.Name in app._STOP and c.IsEnabled and not c.IsOffscreen
                   and tuple(c.GetRuntimeId()) == runtime_id]
        if len(matches) != 1 or not matches[0].HasKeyboardFocus:
            return False
        return app._act(matches[0], patterns_only=True)
    except Exception:
        return False
    finally:
        if previous and previous != window.hwnd and user.GetForegroundWindow() == window.hwnd:
            user.SetForegroundWindow(app.wintypes.HWND(previous))


def act(window, identity, hint, action, prompt_sig='', n=0, text='', nav=''):
    view = _read_stable(window)
    if view is None or not view.screen.identified or view.identity != identity or view.thread_hint != hint:
        return {'ok': False, 'reason': 'changed'}
    # Exact UIA patterns operate on the bound HWND. Never activate it or
    # send coordinate/keyboard input into another application's foreground.
    current = _read_stable(window)
    if current is None or not current.screen.identified or current.identity != identity or current.thread_hint != hint:
        return {'ok': False, 'reason': 'changed'}
    if action == 'focus':
        # An explicit Focus action activates only the already verified HWND.
        # It types nothing and never uses a coordinate fallback.
        try:
            user = app.ctypes.windll.user32
            user.GetForegroundWindow.restype = app.wintypes.HWND
            # UIA SetFocus on Electron's WindowControl only changes UIA focus;
            # it does not activate the top-level native window.
            hwnd = app.wintypes.HWND(window.hwnd)
            if user.GetForegroundWindow() != window.hwnd:
                if not user.SetForegroundWindow(hwnd):
                    return {'ok': False, 'reason': 'focus_failed'}
                user.BringWindowToTop(hwnd)
                # Cross-thread activation is asynchronous. Wait read-only for
                # the one accepted request; do not attach input queues, alter
                # foreground policy or inject keys to override a refusal.
                until = time.monotonic() + 1
                while user.GetForegroundWindow() != window.hwnd:
                    if not user.IsWindow(hwnd) or time.monotonic() >= until:
                        return {'ok': False, 'reason': 'focus_failed'}
                    time.sleep(.02)
            updated = _read_stable(window)
            if updated is None or not updated.screen.identified or updated.identity != identity or updated.thread_hint != hint:
                return {'ok': False, 'reason': 'changed'}
            return {'ok': True}
        except Exception:
            return {'ok': False, 'reason': 'focus_failed'}
    if action == 'interrupt':
        buttons = [c for c in document(window) if c.ControlTypeName == 'ButtonControl' and c.Name in app._STOP and c.IsEnabled and not c.IsOffscreen]
        if not current.screen.running or len(buttons) != 1:
            return {'ok': False, 'reason': 'no_turn'}
        if not _focus_stop_and_invoke(window, identity, hint, buttons[0]):
            return {'ok': False, 'reason': 'unknown'}
    else:
        value = prompt(window)
        if value is None or value['sig'] != prompt_sig:
            return {'ok': False, 'reason': 'no_prompt'}
        if action in {'approve_once', 'deny'}:
            if value['kind'] != 'perm':
                return {'ok': False, 'reason': 'asking'}
            n = 1 if action == 'approve_once' else 2
        if nav == 'esc':
            if value['kind'] == 'perm':
                n = 2
            else:
                return {'ok': False, 'reason': 'unsupported_navigation'}
        if nav in {'next','prev'}:
            control=value['submit'] if nav=='next' else value['back']
            if value['kind']!='ask' or control is None or not app._act(control, patterns_only=True):
                return {'ok':False,'reason':'unsupported_navigation'}
        else:
            if nav not in {'','esc'} or not isinstance(n, int) or n < 1 or n > len(value['choices']):
                return {'ok': False, 'reason': 'bad_pick'}
            control, label, multi = value['choices'][n - 1]
            if multi is None:
                if not text.strip():
                    return {'ok':False,'reason':'no_voice'}
                pattern=control.GetValuePattern()
                if pattern is None or pattern.Value.strip() not in {'', control.Name.strip()}:
                    return {'ok':False,'reason':'draft'}
                user = app.ctypes.windll.user32
                previous = user.GetForegroundWindow()
                try:
                    control.SetFocus()
                    focused = _prompt_after_focus(window, identity, hint, prompt_sig)
                    if focused is None or focused['sig'] != prompt_sig:
                        return {'ok': False, 'reason': 'changed'}
                    pattern.SetValue(text)
                finally:
                    if previous and previous != window.hwnd and user.GetForegroundWindow() == window.hwnd:
                        user.SetForegroundWindow(app.wintypes.HWND(previous))
                if pattern.Value.strip() != text.strip():
                    return {'ok':False,'reason':'unknown'}
            else:
                before=bool(control.GetTogglePattern().ToggleState) if multi else None
                if not app._act(control, patterns_only=True):
                    return {'ok': False, 'reason': 'unknown'}
                if multi:
                    fresh=prompt(window)
                    return {'ok':True} if fresh and fresh['sig']==prompt_sig and bool(control.GetTogglePattern().ToggleState)!=before else {'ok':False,'reason':'unknown'}
            # Recheck the exact question after selection/text entry. Never
            # submit a new card that appeared while choosing an answer.
            if (multi is False and value['submit'] is None and
                    getattr(current, 'question_card', None) is not None):
                fresh = prompt(window)
                if fresh is not None and fresh['sig'] == prompt_sig:
                    if not _commit_native_choice(window, identity, hint, prompt_sig, n, label):
                        return {'ok': False, 'reason': 'unknown'}
                # Earlier steps of a multi-question card can advance on the
                # selection itself. Do not send Enter to that next question.
                # The common confirmation below still verifies the binding.
            if value['submit'] is not None or (multi is None and getattr(current, 'question_card', None) is not None):
                fresh=prompt(window)
                if multi is None and getattr(current, 'question_card', None) is not None:
                    until = time.monotonic() + 5
                    while fresh and fresh['sig'] == prompt_sig and fresh['submit'] is None and time.monotonic() < until:
                        time.sleep(.1)
                        fresh = prompt(window)
                if fresh is None or fresh['sig']!=prompt_sig:
                    return {'ok':False,'reason':'unknown'}
                if fresh['submit'] is None or not app._act(fresh['submit'], patterns_only=True):
                    return {'ok': False, 'reason': 'unknown'}
    until = time.monotonic() + 10
    while time.monotonic() < until:
        updated = app.read_text(window)
        if updated is None or not updated.identity:
            # Answering can unmount the request card before the composer is
            # mounted. Wait read-only for the same binding; never repeat the
            # choice, input or Submit invocation during this redraw.
            time.sleep(.1)
            continue
        if updated.identity != identity or updated.thread_hint != hint:
            return {'ok': False, 'reason': 'unknown'}
        value = prompt(window) if action != 'interrupt' else None
        if (action == 'interrupt' and not updated.screen.running) or (action != 'interrupt' and (value is None or value['sig'] != prompt_sig)):
            return {'ok': True}
        time.sleep(.1)
    return {'ok': False, 'reason': 'unknown'}
