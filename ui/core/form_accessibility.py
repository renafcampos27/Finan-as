"""Explicit field relationships for the application's editable forms."""

from __future__ import annotations

from PySide6.QtWidgets import QComboBox, QFormLayout, QLabel, QTextEdit, QWidget


def bind_field(label: QLabel, field: QWidget, name: str = "") -> None:
    label.setBuddy(field)
    caption = name or label.text().replace("&", "").rstrip(": ")
    if not field.accessibleName():
        field.setAccessibleName(caption)
    if isinstance(field, QComboBox) and field.isEditable():
        field.lineEdit().setAccessibleName(field.accessibleName())
    if isinstance(field, QTextEdit):
        field.setTabChangesFocus(True)


def prepare_form_fields(content: QWidget) -> None:
    for form in content.findChildren(QFormLayout):
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        for row in range(form.rowCount()):
            label_item = form.itemAt(row, QFormLayout.ItemRole.LabelRole)
            field_item = form.itemAt(row, QFormLayout.ItemRole.FieldRole)
            label = label_item.widget() if label_item else None
            field = field_item.widget() if field_item else None
            if isinstance(label, QLabel) and field is not None:
                label.setWordWrap(True)
                bind_field(label, field)
