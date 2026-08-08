"""
Finestre modali con Qt.WindowModal (come AddGameDialog), non ApplicationModal:
l’overlay resta interattivo perché non blocca tutte le finestre dell’app.
Le API statiche QMessageBox / QInputDialog usano spesso ApplicationModal.
"""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QInputDialog, QMessageBox


def _default_button_texts() -> dict:
    """Return translated labels for standard dialog buttons.

    Called at runtime so the current locale is always used.
    """
    from i18n import t
    return {
        QMessageBox.StandardButton.Yes: t("common.yes"),
        QMessageBox.StandardButton.No: t("common.no"),
        QMessageBox.StandardButton.Cancel: t("common.cancel"),
        QMessageBox.StandardButton.Ok: t("common.ok"),
        QMessageBox.StandardButton.Close: t("common.close"),
    }


def message_box_window_modal(
    parent,
    icon: QMessageBox.Icon,
    title: str,
    text: str,
    buttons,
    default_button: QMessageBox.StandardButton | None = None,
    button_texts: dict | None = None,
):
    """QMessageBox with custom button texts, only parent window modal.

    Standard buttons (Yes/No/Cancel/Close) are automatically translated
    via the i18n dictionary unless explicitly overridden by *button_texts*.
    """
    msg = QMessageBox(parent)
    msg.setWindowModality(Qt.WindowModality.WindowModal)
    msg.setIcon(icon)
    msg.setWindowTitle(title)
    msg.setText(text)
    msg.setStandardButtons(buttons)
    if default_button is not None:
        msg.setDefaultButton(default_button)
    # Merge caller overrides on top of automatic translations
    merged = _default_button_texts()
    if button_texts:
        merged.update(button_texts)
    for btn_enum, txt in merged.items():
        actual_btn = msg.button(btn_enum)
        if actual_btn:
            actual_btn.setText(txt)
    return msg.exec()


def question_window_modal(
    parent,
    title: str,
    text: str,
    buttons=QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
    default_button: QMessageBox.StandardButton | None = None,
    button_texts: dict | None = None,
):
    """Equivalent to QMessageBox.question with translated button texts."""
    return message_box_window_modal(
        parent, QMessageBox.Icon.Question, title, text, buttons, default_button, button_texts
    )


def warning_window_modal(parent, title: str, text: str):
    """Equivalente a QMessageBox.warning in modalità finestra."""
    return message_box_window_modal(
        parent, QMessageBox.Icon.Warning, title, text, QMessageBox.StandardButton.Ok
    )


def information_window_modal(parent, title: str, text: str):
    """Equivalente a QMessageBox.information in modalità finestra."""
    return message_box_window_modal(
        parent, QMessageBox.Icon.Information, title, text, QMessageBox.StandardButton.Ok
    )


def _translate_input_buttons(dlg: QInputDialog):
    """Put the current locale on QInputDialog's own OK/Cancel buttons.

    Qt ships its own translations for these and loads none by default, so
    without this they stay English whatever the app language is.
    """
    from i18n import t
    dlg.setOkButtonText(t("common.ok"))
    dlg.setCancelButtonText(t("common.cancel"))


def input_text_window_modal(parent, title: str, label: str, text: str = "") -> tuple[str, bool]:
    """Equivalente a QInputDialog.getText con solo la finestra padre bloccata."""
    dlg = QInputDialog(parent)
    dlg.setWindowModality(Qt.WindowModality.WindowModal)
    dlg.setWindowTitle(title)
    dlg.setLabelText(label)
    dlg.setTextValue(text)
    _translate_input_buttons(dlg)
    ok = dlg.exec() == QDialog.DialogCode.Accepted
    return dlg.textValue(), ok


def input_int_window_modal(parent, title: str, label: str, value: int,
                           minimum: int, maximum: int,
                           step: int = 1) -> tuple[int, bool]:
    """Equivalente a QInputDialog.getInt, tradotto e window-modal.

    The label is set on a dialog instance rather than through the static
    helper so a multi-line explanation is laid out in full instead of being
    squeezed onto one line.
    """
    dlg = QInputDialog(parent)
    dlg.setWindowModality(Qt.WindowModality.WindowModal)
    dlg.setInputMode(QInputDialog.InputMode.IntInput)
    dlg.setWindowTitle(title)
    dlg.setIntRange(minimum, maximum)
    dlg.setIntStep(step)
    dlg.setIntValue(value)
    dlg.setLabelText(label)
    _translate_input_buttons(dlg)
    ok = dlg.exec() == QDialog.DialogCode.Accepted
    return dlg.intValue(), ok
