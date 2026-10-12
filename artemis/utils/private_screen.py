import base64
from copy import deepcopy
from io import BytesIO
import re
from xml.etree import ElementTree

from PIL import Image, ImageDraw


def mask_password_observation(screen):
    elements = deepcopy(screen.ui_elements)
    passwords = [element for element in elements if element.get("password") in (True, "true")]
    xml = None
    malformed = False
    if screen.ui_hierarchy_xml:
        try:
            xml = ElementTree.fromstring(screen.ui_hierarchy_xml)
            passwords.extend(node.attrib for node in xml.iter() if node.get("password") == "true")
        except ElementTree.ParseError:
            malformed = True
    if malformed:
        passwords.extend(elements)
    if not passwords and not malformed:
        return screen
    sensitive_bounds = {
        element["bounds"] for element in passwords if isinstance(element.get("bounds"), str)
    }
    passwords.extend(
        element
        for element in elements
        if element.get("bounds") in sensitive_bounds and element not in passwords
    )
    for element in passwords:
        for key in ("text", "content-desc", "content_description", "hint", "value"):
            if key in element:
                element[key] = "••••"
    screen.ui_elements = elements
    screen.ui_hierarchy_xml = (
        ElementTree.tostring(xml, encoding="unicode") if xml is not None else None
    )
    try:
        image = Image.open(BytesIO(screen.screenshot_bytes)).convert("RGB")
    except (OSError, ValueError):
        image = Image.new(
            "RGB", (max(1, min(screen.width, 4096)), max(1, min(screen.height, 4096))), "black"
        )
    drawing = ImageDraw.Draw(image)
    if malformed:
        drawing.rectangle((0, 0, image.width, image.height), fill="black")
    for element in passwords:
        bounds = re.fullmatch(
            r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]", str(element.get("bounds", ""))
        )
        if not bounds:
            drawing.rectangle((0, 0, image.width, image.height), fill="black")
            break
        left, top, right, bottom = map(int, bounds.groups())
        if right <= left or bottom <= top:
            drawing.rectangle((0, 0, image.width, image.height), fill="black")
            break
        drawing.rectangle((left, top, right, bottom), fill="black")
    buffer = BytesIO()
    image.save(buffer, "PNG")
    screen.screenshot_bytes = buffer.getvalue()
    screen.screenshot_base64 = base64.b64encode(screen.screenshot_bytes).decode()
    return screen
