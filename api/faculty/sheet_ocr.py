"""
attendance_system/api/faculty/sheet_ocr.py
OCR  : OpenRouter API — free vision models (e.g. Gemini Flash, Llama Vision)
Match: local rapidfuzz (free, no API cost)
"""

import os
import re
import base64
import logging

from dotenv import load_dotenv
from openai import OpenAI
from rapidfuzz import fuzz
from core.db import get_cursor

load_dotenv()
logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# OpenRouter client — lazily initialised
# ─────────────────────────────────────────────

_client = None

def _get_client() -> OpenAI:
    global _client
    if _client is None:
        api_key = os.getenv("OPENROUTER_API_KEY")
        if not api_key:
            raise RuntimeError("OPENROUTER_API_KEY not set in .env")
        _client = OpenAI(
            base_url="https://openrouter.ai/api/v1",
            api_key=api_key,
        )
    return _client


# ─────────────────────────────────────────────
# OCR — OpenRouter Vision
# ─────────────────────────────────────────────

# Free models on OpenRouter that support vision (as of 2025):
# "google/gemini-2.0-flash-exp:free"   ← best quality, recommended
# "meta-llama/llama-3.2-11b-vision-instruct:free"
# "qwen/qwen2-vl-7b-instruct:free"
#
# Change the model below if one stops being free — check:
# https://openrouter.ai/models?order=pricing-asc&supported_parameters=tools

OCR_MODEL = "openrouter/auto"


def extract_identifiers_from_image(image_path: str) -> list[str]:
    """
    Extract handwritten student names / USNs
    from attendance sheet images using OpenRouter vision model.
    """

    from PIL import Image
    import tempfile

    # -----------------------------
    # Resize large mobile images
    # -----------------------------
    img = Image.open(image_path)

    img.thumbnail((1800, 1800))

    temp_file = tempfile.NamedTemporaryFile(
        suffix=".jpg",
        delete=False
    )

    img.save(temp_file.name, format="JPEG", quality=85)

    # -----------------------------
    # Convert image to base64
    # -----------------------------
    with open(temp_file.name, "rb") as f:
        image_b64 = base64.b64encode(
            f.read()
        ).decode("utf-8")

    # -----------------------------
    # OCR Prompt
    # -----------------------------
    prompt = """
    This image contains a handwritten college attendance sheet.

    Extract ONLY student names or USNs.

    STRICT RULES:
    - One entry per line
    - No numbering
    - No bullets
    - No explanations
    - Ignore headings
    - Ignore dates
    - Ignore subject names
    - Ignore printed labels
    - Ignore symbols
    - Preserve names exactly as written
    - If uncertain, still attempt best guess

    Example output:

    1MS22CS001
    Rahul Kumar
    Altamash Sami
    """

    # -----------------------------
    # Send to OpenRouter
    # -----------------------------
    response = _get_client().chat.completions.create(

        model=OCR_MODEL,

        max_tokens=700,

        temperature=0,

        messages=[
            {
                "role": "user",
                "content": [

                    {
                        "type": "text",
                        "text": prompt
                    },

                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/jpeg;base64,{image_b64}"
                        }
                    }

                ]
            }
        ]
    )

    # -----------------------------
    # Safe response extraction
    # -----------------------------
    raw = response.choices[0].message.content

    if not raw:
        logger.warning("OCR model returned empty response")
        return []

    raw = raw.strip()

    logger.info("OpenRouter OCR raw output:\n%s", raw)

    print("\n========== OCR OUTPUT ==========")
    print(raw)
    print("================================\n")

    # -----------------------------
    # Clean extracted lines
    # -----------------------------
    lines = []

    for line in raw.splitlines():

        cleaned = line.strip()

        # Remove markdown bullets/numbers
        cleaned = re.sub(
            r"^[\-\*\•]+",
            "",
            cleaned
        ).strip()

        # Remove unwanted symbols
        cleaned = re.sub(
            r"[^A-Za-z0-9 .]",
            "",
            cleaned
        ).strip()

        # Normalize spaces
        cleaned = re.sub(
            r"\s+",
            " ",
            cleaned
        )

        # Ignore empty/small garbage
        if len(cleaned) < 2:
            continue

        # Ignore known unwanted words
        lower = cleaned.lower()

        skip_words = [
            "attendance",
            "subject",
            "date",
            "signature",
            "faculty",
            "present",
            "absent",
            "semester",
            "section"
        ]

        if any(word in lower for word in skip_words):
            continue

        lines.append(cleaned)

    # -----------------------------
    # Remove duplicates
    # -----------------------------
    unique_lines = []

    seen = set()

    for item in lines:

        key = item.upper()

        if key not in seen:
            seen.add(key)
            unique_lines.append(item)

    logger.info(
        "Final identifiers (%d): %s",
        len(unique_lines),
        unique_lines
    )

    return unique_lines

# ─────────────────────────────────────────────
# USN HELPERS
# ─────────────────────────────────────────────

def looks_like_usn(text: str) -> bool:
    """
    True if text matches your college USN format.
    Pattern: 1XX22CS001 / 4SF22IS045
    Adjust regex if your format differs.
    """
    return bool(
        re.match(r"^[1-9][A-Z0-9]{2}\d{2}[A-Z]{2,3}\d{3}$", text.upper())
    )


def _fuzzy_usn_match(ocr_usn: str, usn_map: dict):
    """
    Handle common OCR character confusions for USNs.
    e.g. O↔0, I↔1, S↔5, B↔8, Z↔2
    """
    confusion = {
        "O": "0", "0": "O",
        "I": "1", "1": "I",
        "S": "5", "5": "S",
        "B": "8", "8": "B",
        "Z": "2",
    }
    for i, ch in enumerate(ocr_usn.upper()):
        if ch in confusion:
            candidate = ocr_usn[:i] + confusion[ch] + ocr_usn[i + 1:]
            if candidate.upper() in usn_map:
                logger.info("Fuzzy USN: %s → %s", ocr_usn, candidate)
                return usn_map[candidate.upper()]
    return None


# ─────────────────────────────────────────────
# STUDENT MATCHING — fully local, no API cost
# ─────────────────────────────────────────────

def match_students(identifiers: list[str], section_id: int) -> dict:
    """
    Match OCR'd identifiers against students enrolled in the section.

    Priority:
      1. Exact USN match
      2. Exact full-name match (case-insensitive)
      3. Fuzzy USN match (OCR character confusion)
      4. Substring name match
      5. Fuzzy token name match via rapidfuzz (threshold >= 72)

    Returns:
        {
          "matched": [{"student_id", "usn", "full_name"}, ...],
          "unmatched": ["raw_string", ...]
        }
    """
    cursor = get_cursor()
    cursor.execute(
        """
        SELECT st.id AS student_id, st.usn, u.full_name
        FROM students st
        JOIN users u ON u.id = st.user_id
        WHERE st.section_id = %s
        """,
        (section_id,),
    )
    all_students = cursor.fetchall()

    usn_map  = {s["usn"].strip().upper(): s for s in all_students}
    name_map = {s["full_name"].strip().upper(): s for s in all_students}

    matched, unmatched = [], []

    for raw in identifiers:
        key = raw.strip().upper()

        # 1. Exact USN
        if key in usn_map:
            matched.append(usn_map[key])
            continue

        # 2. Exact full name
        if key in name_map:
            matched.append(name_map[key])
            continue

        # 3. Fuzzy USN (character confusion)
        if looks_like_usn(key):
            best = _fuzzy_usn_match(key, usn_map)
            if best:
                matched.append(best)
                continue

        # 4. Substring name (e.g. OCR read "Rahul", DB has "Rahul Kumar")
        found = next(
            (s for s in all_students if key in s["full_name"].upper()),
            None
        )
        if found:
            matched.append(found)
            continue

        # 5. Fuzzy token name — handles "Altamash sami" vs "Altamash Sami",
        #    word-order differences, minor spelling errors
        best_match, best_score = None, 0
        for s in all_students:
            score = fuzz.token_sort_ratio(key, s["full_name"].upper())
            if score > best_score:
                best_score = score
                best_match = s

        if best_score >= 72:
            logger.info(
                "Fuzzy name: '%s' → '%s' (score %d)",
                key, best_match["full_name"], best_score
            )
            matched.append(best_match)
            continue

        unmatched.append(raw)

    # Deduplicate — same student matched by both name and USN on sheet
    seen, deduped = set(), []
    for s in matched:
        if s["student_id"] not in seen:
            seen.add(s["student_id"])
            deduped.append(s)

    return {"matched": deduped, "unmatched": unmatched}