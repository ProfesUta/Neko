"""Optional local OCR verification. Boxes come from actual OCR pixels, never an LLM."""

import csv
import io
import os
import shutil
import subprocess
import time
from collections import defaultdict

from core.cancellation import check_cancelled
from core.spatial import query_words
from core.spatial_geometry import Rect


def locate_text_pixels(image, label, cancel_event=None):
    executable = os.getenv("CAPRI_TESSERACT") or shutil.which("tesseract")
    if not executable:
        return []
    words = set(query_words(label))
    if not words:
        return []
    data = io.BytesIO()
    image.save(data, format="PNG")
    with subprocess.Popen(
        [executable, "stdin", "stdout", "tsv"], stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    ) as process:
        deadline = time.monotonic() + 8
        first = True
        try:
            while True:
                check_cancelled(cancel_event)
                if time.monotonic() > deadline:
                    return []
                try:
                    output, _error = process.communicate(data.getvalue() if first else None, timeout=0.05)
                    break
                except subprocess.TimeoutExpired:
                    first = False
            if process.returncode:
                return []
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()
    lines = defaultdict(list)
    for row in csv.DictReader(io.StringIO(output.decode("utf-8", errors="replace")), delimiter="\t"):
        try:
            if float(row["conf"]) >= 85 and row.get("text", "").strip():
                lines[(row["block_num"], row["par_num"], row["line_num"])].append(row)
        except (KeyError, ValueError):
            continue
    found = []
    for line in lines.values():
        for start in range(len(line)):
            for end in range(start + 1, min(len(line), start + len(words) + 2) + 1):
                group = line[start:end]
                if not words <= set(query_words(" ".join(row["text"] for row in group))):
                    continue
                x = min(int(row["left"]) for row in group)
                y = min(int(row["top"]) for row in group)
                right = max(int(row["left"]) + int(row["width"]) for row in group)
                bottom = max(int(row["top"]) + int(row["height"]) for row in group)
                rect = Rect(x, y, right - x, bottom - y)
                if not any(old[0].intersection(rect).area >= min(old[0].area, rect.area) * 0.8 for old in found):
                    found.append((rect, min(float(row["conf"]) for row in group) / 100))
                break
    return found
