"""
SaveSync - Shared header-click behavior for a group of checkable widgets.

Used wherever several independently checkable rows (checkboxes or checkable
chip buttons) sit under one clickable group header — the search-merge
dialog's per-source chips, the add/edit game path list's sections, the
auto-scan confirmation panel's per-game path list. Any widget exposing
isChecked()/setChecked() (QCheckBox, a checkable QPushButton, ...) works.

Two independent actions, each keyed by a caller-chosen group id so one
instance can track several groups at once:

- toggle(): clears the group, remembering exactly which items were
  checked, or restores that SAME remembered selection — never a blanket
  "everything". Clicking a member individually while cleared is free to
  build a new selection; the next toggle() remembers THAT instead.
- bulk_set(): a plain, unconditional select-all/clear-all switch,
  independent of toggle()'s memory — for a hard reset to "everything" or
  "nothing" rather than whatever was last checked.
"""


class GroupToggle:
    def __init__(self):
        self._memory: dict = {}
        self._bulk_on: dict = {}

    def toggle(self, key, items: list) -> None:
        if not items:
            return
        if any(i.isChecked() for i in items):
            self._memory[key] = {i for i in items if i.isChecked()}
            for i in items:
                i.setChecked(False)
        else:
            remembered = self._memory.get(key)
            for i in items:
                i.setChecked(i in remembered if remembered else True)

    def bulk_set(self, key, items: list) -> None:
        if not items:
            return
        turn_on = not self._bulk_on.get(key, True)
        self._bulk_on[key] = turn_on
        for i in items:
            i.setChecked(turn_on)
