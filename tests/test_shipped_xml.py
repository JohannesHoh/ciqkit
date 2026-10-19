"""Every XML file we ship parses: examples as-is, templates after substitution.

Templates carry ``{{PLACEHOLDER}}`` text, so a raw parse proves nothing (a
placeholder inside an attribute is fine, one in a tag name is not). Rendering
through the real scaffold path is what a user's ``ciqkit new`` actually writes.
"""
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from ciqkit import scaffold

REPO = Path(__file__).resolve().parent.parent
CASES = [("blank", "app"), ("blank", "watchface"), ("blank", "datafield"),
         ("tictactoe", "app")]


@pytest.mark.parametrize("template,type_", CASES)
def test_rendered_template_xml_parses(template, type_):
    params = scaffold.params_for(
        "xml-check", type_=type_, devices=["venu3", "fr255"],
        app_uuid="12345678-1234-4234-8234-123456789abc", template=template)
    files = scaffold.template_files(template, type_, params)
    xml = {k: v for k, v in files.items() if k.endswith(".xml")}
    assert "manifest.xml" in xml
    for name, data in xml.items():
        try:
            ET.fromstring(data)
        except ET.ParseError as e:  # pragma: no cover - failure path
            pytest.fail(f"{template}/{type_}: {name}: {e}")


def test_example_xml_parses():
    files = list((REPO / "examples").rglob("*.xml"))
    assert files
    for f in files:
        ET.parse(f)
