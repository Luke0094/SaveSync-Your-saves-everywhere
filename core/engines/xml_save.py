"""Plain XML saves — .NET serializer output, Unity/Godot XML, and similar.

Read with the standard library's parser, which does not resolve external
entities, so a save cannot talk this into fetching anything. Values live in
element text and attributes; structure is carried through untouched.
"""
import re
import xml.etree.ElementTree as ET


class XmlSaveError(ValueError):
    pass


def kind_of_text(text: str) -> str:
    """What a piece of text in a save is: a number, a flag, or words."""
    low = text.strip().lower()
    if low in ("true", "false"):
        return "bool"
    try:
        int(text)
        return "int"
    except ValueError:
        pass
    try:
        float(text)
        return "float"
    except ValueError:
        return "str"


def value_of_text(text: str):
    kind = kind_of_text(text)
    if kind == "bool":
        return text.strip().lower() == "true"
    if kind == "int":
        return int(text)
    if kind == "float":
        return float(text)
    return text


class XmlDoc:
    """One XML document opened for editing."""

    def __init__(self):
        self._tree = None
        self._spots = []          # (element, attribute name or None, where)
        self._declaration = b""
        self._encoding = "utf-8"

    def load(self, data: bytes) -> None:
        head = data[:200].lstrip()
        if head.startswith(b"<?xml"):
            end = data.find(b"?>")
            if end > 0:
                self._declaration = data[:end + 2]
                match = re.search(rb'encoding=["\']([\w-]+)["\']',
                                  self._declaration)
                if match:
                    self._encoding = match.group(1).decode("ascii", "ignore")
        try:
            self._tree = ET.fromstring(data.decode(self._encoding, "replace"))
        except ET.ParseError as e:
            raise XmlSaveError(f"this XML will not parse: {e}") from e
        self._spots = []
        self._gather(self._tree, "")
        if not self._spots:
            raise XmlSaveError("this XML holds no values to edit")

    def _gather(self, node, prefix: str) -> None:
        where = f"{prefix}/{node.tag}" if prefix else str(node.tag)
        for name in node.attrib:
            self._spots.append((node, name, where))
        children = list(node)
        text = (node.text or "").strip()
        # An element with children has no value of its own: whatever sits
        # between its tags is the layout of the file, not a value anybody set.
        if text and not children:
            self._spots.append((node, None, where))
        for child in children:
            self._gather(child, where)

    def dump(self) -> bytes:
        body = ET.tostring(self._tree, encoding="unicode")
        raw = body.encode(self._encoding, "xmlcharrefreplace")
        if self._declaration:
            return self._declaration + b"\n" + raw
        return raw

    def values(self) -> list:
        """(index, label, kind, value, group) for every editable spot."""
        out = []
        for i, (node, attr, where) in enumerate(self._spots):
            text = node.attrib[attr] if attr else (node.text or "").strip()
            label = f"{node.tag}@{attr}" if attr else str(node.tag)
            group = where.rsplit("/", 1)[0] if "/" in where else "(root)"
            out.append((i, label, kind_of_text(text), value_of_text(text),
                        group))
        return out

    def set_value(self, index: int, value) -> None:
        node, attr, _where = self._spots[index]
        text = "true" if value is True else "false" if value is False \
            else str(value)
        if attr:
            node.attrib[attr] = text
        else:
            node.text = text


def loads(data: bytes) -> XmlDoc:
    doc = XmlDoc()
    doc.load(data)
    return doc
