"""Transfer referenced native images, with bounded reads and image validation."""

from __future__ import annotations

import base64
import binascii
import hashlib
import io
import logging
import os
import re
from dataclasses import dataclass
from urllib.parse import unquote, urlsplit

import httpx
from PIL import Image, UnidentifiedImageError

from .config import resolve_path
from .store import NodeStore

log = logging.getLogger(__name__)
MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_IMAGE_PIXELS = 25_000_000
MAX_OUTPUT_IMAGES = 16
MAX_OUTPUT_BYTES = 40 * 1024 * 1024


@dataclass
class ImageSource:
    data: bytes
    name: str
    mime: str
    source_path: str | None = None


def markdown_local_images(text: str) -> list[str]:
    """Conservative inline Markdown image destinations, excluding code spans/fences.

    Angle destinations support spaces, and a single nested parenthesis pair is
    accepted in ordinary destinations. Reference-style images are deliberately
    left as links rather than guessed. URLs are never downloaded here.
    """
    visible = []
    fence_char, fence_length = None, 0
    for line in text.splitlines():
        marker = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line)
        if marker:
            sequence, tail = marker.groups()
            if fence_char is None:
                fence_char, fence_length = sequence[0], len(sequence)
                continue
            if sequence[0] == fence_char and len(sequence) >= fence_length and not tail.strip():
                fence_char, fence_length = None, 0
                continue
        if fence_char is None:
            visible.append(line)
    text = "\n".join(visible)
    text = re.sub(r"(`+)(?!`)(.*?)\1(?!`)", "", text, flags=re.DOTALL)
    pattern = r"!\[[^\]\n]*\]\(\s*(?:<([^>\n]+)>|((?:\\[()\[\]]|[^\s()]|\([^()\n]*\))+))(?:\s+[\"'][^\n]*?[\"'])?\s*\)"
    result = []
    for match in re.finditer(pattern, text):
        value = match.group(1) or match.group(2)
        value = re.sub(r"\\([()\[\]])", r"\1", value)
        drive_path = bool(re.match(r"^[a-zA-Z]:[\\/]", value))
        parsed = urlsplit(value) if not drive_path else None
        if parsed and parsed.scheme == "file":
            if parsed.netloc not in {"", "localhost"}:
                continue
            value = unquote(parsed.path)
            if os.name == "nt" and re.match(r"^/[a-zA-Z]:", value):
                value = value[1:]
        elif parsed and (parsed.scheme or parsed.netloc):
            continue
        else:
            value = unquote(value)
        if value and value not in result:
            result.append(value)
    return result


def verified_image(data: bytes, name: str, source_path: str | None = None) -> ImageSource:
    if not data or len(data) > MAX_IMAGE_BYTES:
        raise ValueError("Image exceeds the 20 MiB limit or is empty")
    try:
        with Image.open(io.BytesIO(data)) as image:
            if image.width * image.height > MAX_IMAGE_PIXELS:
                raise ValueError("Image exceeds the 25 megapixel limit")
            image_format = image.format
            image.verify()
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise ValueError("Artifact is not a valid supported image") from exc
    mime = {"PNG": "image/png", "JPEG": "image/jpeg", "GIF": "image/gif", "WEBP": "image/webp"}.get(
        image_format
    )
    if mime is None:
        raise ValueError("Only PNG, JPEG, GIF, and WebP images are supported")
    return ImageSource(data, name, mime, source_path)


def _base64_image(value: str, name: str) -> ImageSource:
    if value.startswith("data:"):
        header, value = value.split(",", 1)
        if not header.startswith("data:image/") or ";base64" not in header:
            raise ValueError("Only base64 image data is accepted")
    if len(value) > MAX_IMAGE_BYTES * 4 // 3 + 8:
        raise ValueError("Encoded image exceeds size limit")
    try:
        decoded = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("Invalid base64 image") from exc
    return verified_image(decoded, name)


def extract_images(item: dict, cwd: str | None = None) -> list[ImageSource]:
    result: list[ImageSource] = []
    seen_paths: set[str] = set()
    seen_images: set[tuple[str, str | None]] = set()
    total_bytes = 0
    warned = False

    def warn_limit() -> None:
        nonlocal warned
        if not warned:
            log.warning("Output image count or total size limit reached; excess images skipped")
            warned = True

    def append(source: ImageSource) -> None:
        nonlocal total_bytes
        key = (hashlib.sha256(source.data).hexdigest(), source.source_path)
        if key in seen_images:
            return
        if len(result) >= MAX_OUTPUT_IMAGES or total_bytes + len(source.data) > MAX_OUTPUT_BYTES:
            warn_limit()
            return
        seen_images.add(key)
        total_bytes += len(source.data)
        result.append(source)

    def local(value: str) -> None:
        remaining = min(MAX_IMAGE_BYTES, MAX_OUTPUT_BYTES - total_bytes)
        if len(result) >= MAX_OUTPUT_IMAGES or remaining <= 0:
            warn_limit()
            return
        try:
            path = resolve_path(value, base=cwd)
            if str(path) in seen_paths:
                return
            seen_paths.add(str(path))
            if path.stat().st_size > remaining:
                warn_limit()
                return
            # A file can grow after stat(). Bound the read itself as well.
            with path.open("rb") as stream:
                data = stream.read(remaining + 1)
            if len(data) > remaining:
                warn_limit()
                return
            append(verified_image(data, path.name, str(path)))
        except (ValueError, OSError):
            # Missing files and OS permission failures remain unavailable.
            return

    if item.get("type") == "imageView" and isinstance(item.get("path"), str):
        local(item["path"])
    if item.get("type") == "imageGeneration":
        if isinstance(item.get("savedPath"), str):
            local(item["savedPath"])
        if not result and isinstance(item.get("result"), str):
            try:
                append(_base64_image(item["result"], "generated.png"))
            except ValueError:
                pass

    def visit(value) -> None:
        if len(result) >= MAX_OUTPUT_IMAGES:
            warn_limit()
            return
        if isinstance(value, dict):
            if value.get("type") == "image" and isinstance(value.get("data"), str):
                try:
                    append(_base64_image(value["data"], "tool-image.png"))
                except ValueError:
                    pass
            if value.get("type") == "localImage" and isinstance(value.get("path"), str):
                local(value["path"])
            image_key = {"inputImage": "imageUrl", "input_image": "image_url", "image": "url"}.get(
                value.get("type")
            )
            if image_key and isinstance(value.get(image_key), str):
                encoded = value[image_key]
                # Native tools can also return remote URLs or file IDs. The
                # bridge only accepts embedded bytes and never fetches a URL.
                if encoded.startswith("data:image/"):
                    try:
                        append(_base64_image(encoded, "tool-image.png"))
                    except ValueError:
                        pass
                elif encoded.startswith("file://"):
                    paths = markdown_local_images(f"![image](<{encoded}>)")
                    for path in paths:
                        local(path)
            # Only output text fields, never arguments/code/paths, are parsed.
            if value.get("type") in {
                "agentMessage",
                "text",
                "inputText",
                "input_text",
            } and isinstance(value.get("text"), str):
                for path in markdown_local_images(value["text"]):
                    local(path)
            for key in ("content", "result", "structuredContent"):
                if key in value:
                    visit(value[key])
            if value.get("type") == "dynamicToolCall":
                visit(value.get("contentItems"))
            elif value.get("type") == "functionCallOutput":
                output = value.get("output")
                if isinstance(output, str):
                    for path in markdown_local_images(output):
                        local(path)
                else:
                    visit(output)
        elif isinstance(value, list):
            for block in value:
                visit(block)

    visit(item)
    return result


class AssetBridge:
    def __init__(self, store: NodeStore, client: httpx.AsyncClient, server: str):
        self.store, self.client, self.server = store, client, server

    async def process(self, event: dict) -> list[dict]:
        params = event["params"]
        thread_id, item = params.get("threadId"), params.get("item", {})
        item_id = item.get("id")
        if not thread_id or not item_id:
            return []
        cwd = self.store.thread_cwd(thread_id)
        images = extract_images(item, cwd)
        assets = []
        for image in images:
            digest = hashlib.sha256(image.data).hexdigest()
            key = f"{thread_id}:{item_id}:{digest}"
            asset = self.store.cached_asset(key)
            if not asset:
                response = await self.client.post(
                    self.server + "/api/node/assets",
                    data={"thread_id": thread_id, "item_id": item_id},
                    files={"file": (image.name, image.data, image.mime)},
                )
                response.raise_for_status()
                asset = response.json()["asset"]
                self.store.save_asset(key, asset)
            asset = {**asset, "name": image.name}
            if image.source_path:
                asset["source_path"] = image.source_path
            assets.append(asset)
        return assets
