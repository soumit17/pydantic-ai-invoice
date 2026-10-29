"""Turn an invoice file into the user prompt for the extractor (the graph's `load_document` step).

PDFs and scans go to Claude as `BinaryContent`: Claude reads them natively, so there is no OCR step.
Emails are plain text, so their body goes in as a string.
"""

from __future__ import annotations

import io
from email import message_from_bytes, policy
from email.message import EmailMessage
from pathlib import Path

from PIL import Image
from pydantic_ai import BinaryContent
from pydantic_ai.messages import UserContent

MAX_IMAGE_EDGE = 1600  # spec §21: scans downscaled to ≤ 1600 px on the long edge, to cap image tokens

PROMPT = "Extract the invoice data from this document."


def _png_within_limit(data: bytes) -> bytes:
    img = Image.open(io.BytesIO(data))
    if max(img.size) <= MAX_IMAGE_EDGE:
        return data
    img.thumbnail((MAX_IMAGE_EDGE, MAX_IMAGE_EDGE))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _email_text(data: bytes) -> str:
    msg = message_from_bytes(data, policy=policy.default)
    assert isinstance(msg, EmailMessage)
    body = msg.get_body(preferencelist=("plain",))
    text = body.get_content() if body is not None else ""
    return f"From: {msg['From']}\nDate: {msg['Date']}\nSubject: {msg['Subject']}\n\n{text}"


def load_document(path: Path) -> list[UserContent]:
    data = path.read_bytes()
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return [PROMPT, BinaryContent(data=data, media_type="application/pdf")]
    if suffix == ".png":
        return [PROMPT, BinaryContent(data=_png_within_limit(data), media_type="image/png")]
    if suffix in (".jpg", ".jpeg"):
        return [PROMPT, BinaryContent(data=data, media_type="image/jpeg")]
    if suffix == ".eml":
        return [f"{PROMPT} It arrived as an email:\n\n{_email_text(data)}"]
    raise ValueError(f"unsupported document type: {path.name}")
