"""
Core application logic for the Assignment Evaluator.

Environment variables
---------------------
GEMINI_API_KEY
    Your Google Gemini API key (required at runtime).
POPPLER_BIN_PATH
    Full path to the folder that contains ``pdfinfo`` / ``pdfinfo.exe``.
    When unset the tool looks for a ``Release-25.12.0-0`` bundle next to
    this file, then falls back to whatever is already on PATH.
"""

import json
import hashlib
import logging
import os
import re
import shutil
import tempfile
import threading
import time
import tkinter as tk
import webbrowser
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk

import pymupdf as fitz  # PyMuPDF
import PIL.Image
import pandas as pd
import google.generativeai as genai
from pdf2image import convert_from_path


# -----------------------------
# MODEL SETUP
# -----------------------------

# Local file where the user's API key is persisted (never committed to source control)
_KEY_FILE = Path.home() / ".assignment_evaluator" / "api_key.json"


def _load_saved_api_key() -> str:
    """Return the key saved by the user on a previous run, or empty string."""
    try:
        data = json.loads(_KEY_FILE.read_text(encoding="utf-8"))
        return data.get("gemini_api_key", "").strip()
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return ""


def _save_api_key(key: str) -> None:
    """Persist the key to the local config file (mode 600)."""
    _KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
    _KEY_FILE.write_text(json.dumps({"gemini_api_key": key}), encoding="utf-8")
    try:
        _KEY_FILE.chmod(0o600)  # owner read/write only (no-op on Windows)
    except OSError:
        pass


def _delete_saved_api_key() -> None:
    """Remove a previously saved key."""
    try:
        _KEY_FILE.unlink(missing_ok=True)
    except OSError:
        pass


def _get_api_key() -> str:
    """Return the Gemini API key: environment variable takes priority, then saved file."""
    return os.environ.get("GEMINI_API_KEY", "").strip() or _load_saved_api_key()


def _configure_genai(key: str | None = None) -> None:
    """Configure the google-generativeai library with the given or discovered key."""
    resolved = (key or "").strip() or _get_api_key()
    if not resolved:
        raise EnvironmentError("No Gemini API key provided.")
    genai.configure(api_key=resolved)


def ask_for_api_key() -> str:
    """
    Show a Tkinter dialog that asks the user for their Gemini API key.
    Returns the entered key, or raises KeyboardInterrupt if cancelled.
    """
    saved_key = _load_saved_api_key()

    try:
        root = tk.Tk()
    except tk.TclError as exc:
        raise RuntimeError(f"Could not start the GUI: {exc}") from exc

    root.title("Gemini API Key")
    root.resizable(False, False)

    result = {"key": "", "submitted": False}

    content = ttk.Frame(root, padding=20)
    content.grid(row=0, column=0, sticky="nsew")
    content.columnconfigure(0, weight=1)

    # ── Title ──────────────────────────────────────────────────────────────
    ttk.Label(
        content,
        text="Enter your Google Gemini API Key",
        font=("TkDefaultFont", 11, "bold"),
    ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 4))

    # ── How-to link ────────────────────────────────────────────────────────
    how_to_frame = ttk.Frame(content)
    how_to_frame.grid(row=1, column=0, columnspan=2, sticky="w", pady=(0, 12))

    ttk.Label(how_to_frame, text="Don't have a key? ").pack(side="left")
    link = tk.Label(
        how_to_frame,
        text="Get one free at Google AI Studio →",
        fg="#1a73e8",
        cursor="hand2",
    )
    link.pack(side="left")
    link.bind("<Button-1>", lambda _e: webbrowser.open("https://aistudio.google.com/app/apikey"))

    steps_text = (
        "How to generate your key:\n"
        "  1. Visit https://aistudio.google.com/app/apikey\n"
        "  2. Sign in with your Google account\n"
        "  3. Click \"Create API key\"\n"
        "  4. Copy the key and paste it below"
    )
    ttk.Label(content, text=steps_text, justify="left", foreground="#555555").grid(
        row=2, column=0, columnspan=2, sticky="w", pady=(0, 12)
    )

    # ── Key entry ──────────────────────────────────────────────────────────
    ttk.Label(content, text="API Key:").grid(row=3, column=0, sticky="w", pady=4)
    key_var = tk.StringVar(value=saved_key)
    key_entry = ttk.Entry(content, textvariable=key_var, width=52, show="•")
    key_entry.grid(row=4, column=0, sticky="ew", pady=(0, 4))

    show_var = tk.BooleanVar(value=False)

    def _toggle_show():
        key_entry.config(show="" if show_var.get() else "•")

    ttk.Checkbutton(
        content, text="Show key", variable=show_var, command=_toggle_show
    ).grid(row=5, column=0, sticky="w", pady=(0, 8))

    # ── Save option ────────────────────────────────────────────────────────
    save_var = tk.BooleanVar(value=bool(saved_key))
    ttk.Checkbutton(
        content,
        text="Save key locally for next use  (stored in ~/.assignment_evaluator/api_key.json)",
        variable=save_var,
    ).grid(row=6, column=0, columnspan=2, sticky="w", pady=(0, 2))

    if saved_key:
        ttk.Label(
            content,
            text="A saved key was found and pre-filled above.",
            foreground="green",
        ).grid(row=7, column=0, columnspan=2, sticky="w", pady=(0, 8))
    else:
        ttk.Frame(content, height=8).grid(row=7)  # spacer

    # ── Buttons ────────────────────────────────────────────────────────────
    btn_frame = ttk.Frame(content)
    btn_frame.grid(row=8, column=0, columnspan=2, sticky="e", pady=(12, 0))

    def _submit():
        key = key_var.get().strip()
        if not key:
            messagebox.showerror("Missing key", "Please enter your Gemini API key.", parent=root)
            return
        if not key.startswith("AIza"):
            if not messagebox.askyesno(
                "Unusual key",
                "This doesn't look like a standard Gemini API key (expected to start with 'AIza').\nContinue anyway?",
                parent=root,
            ):
                return
        if save_var.get():
            _save_api_key(key)
        else:
            _delete_saved_api_key()
        result["key"] = key
        result["submitted"] = True
        root.destroy()

    def _cancel():
        root.destroy()

    ttk.Button(btn_frame, text="Cancel", command=_cancel).grid(row=0, column=0, padx=(0, 8))
    ttk.Button(btn_frame, text="Continue", command=_submit).grid(row=0, column=1)

    root.bind("<Return>", lambda _e: _submit())
    root.bind("<Escape>", lambda _e: _cancel())
    key_entry.focus_set()
    root.mainloop()

    if not result["submitted"]:
        raise KeyboardInterrupt("API key entry cancelled.")

    return result["key"]


MODEL_NAME = "gemini-2.5-flash-lite"  # Free tier default with unlimited daily requests
AVAILABLE_MODELS = [
    "gemini-3.1-flash-lite",  # Free tier
    "gemini-3-flash",         # Free tier
    "gemini-2.5-flash-lite",  # Free tier (unlimited daily requests)
    "gemini-2.0-flash-lite",  # Free tier alternative
    "gemma-3-27b-it",         # Text-only
    "gemma-3-12b-it",         # Text-only
    "gemma-3-4b-it",          # Text-only
    "gemma-3-1b-it",          # Text-only
    "gemini-2.5-flash",
    "gemini-2.0-flash",
    "gemini-3.1-pro",
    "gemini-2.5-pro",
    "gemini-1.5-pro",
]

# Map shorthand model names to full names for convenience
MODEL_SHORTHAND_MAP = {
    "gemini-lite": "gemini-3.1-flash-lite",
    "gemini-flash": "gemini-3-flash",
    "gemini-pro": "gemini-2.5-pro",
    "gemma": "gemma-3-27b-it",
}

def normalize_model_name(model_name):
    """Convert shorthand model names to full names. Returns the full name or the input if not a shorthand."""
    return MODEL_SHORTHAND_MAP.get(model_name.lower(), model_name)


def is_text_only_model(model_name):
    normalized = normalize_model_name((model_name or "").strip()).lower()
    return normalized.startswith("gemma-")


# -----------------------------
# DEFAULTS
# -----------------------------
PACKAGE_DIR = Path(__file__).resolve().parent

# Poppler: env-var > bundled release folder > system PATH
def _resolve_poppler_path() -> Path:
    env_path = os.environ.get("POPPLER_BIN_PATH", "").strip()
    if env_path:
        return Path(env_path)
    # Try the bundled release that ships alongside the package source
    bundled = PACKAGE_DIR.parent / "Release-25.12.0-0" / "poppler-25.12.0" / "Library" / "bin"
    if bundled.exists():
        return bundled
    # Fallback: assume poppler is on system PATH (return an empty Path sentinel)
    return Path("")


POPPLER_PATH = _resolve_poppler_path()

DEFAULT_PAGE_LIMIT = 40
DEFAULT_DPI = 150
DEFAULT_TIMEOUT_SECONDS = 180
DEFAULT_DELAY_SECONDS = 2
DEFAULT_RETRY_COUNT = 3
METADATA_PAGE_LIMIT = 8
METADATA_DPI = 120
METADATA_TIMEOUT_SECONDS = 45
METADATA_RETRY_COUNT = 1
DEFAULT_STRICTNESS = "moderate"
DEFAULT_FEEDBACK_STYLE = "balanced"
DEFAULT_EXPECT_HANDWRITTEN = True
MAX_QUESTION_COUNT = 200
DEFAULT_SOLUTION_MODEL = MODEL_NAME
DEFAULT_EVALUATION_MODEL = MODEL_NAME
DEFAULT_EXCEL_NAME = "Assignment_Evaluation_Results.xlsx"
LOGS_DIR = Path.cwd() / "evaluation_logs"
MATERIAL_CACHE_DIR = Path.cwd() / "assignment_material_cache"


# -----------------------------
# LOGGING
# -----------------------------
def setup_logging(session_name):
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    session_dir = LOGS_DIR / f"{session_name}_{timestamp}"
    session_dir.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger("assignment_evaluator")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    formatter = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")

    file_handler = logging.FileHandler(session_dir / "run.log", encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)

    return logger, session_dir


# -----------------------------
# HELPERS
# -----------------------------
def sanitize_filename(name):
    return re.sub(r'[\\/*?:"<>|]', "", str(name)).strip()


def normalize_output_name(name, max_length=50):
    cleaned_name = sanitize_filename(name)
    cleaned_name = re.sub(r"\s+", "_", cleaned_name)
    return cleaned_name[:max_length] or "Unknown"


def prompt_text(message, default=None, allow_empty=False):
    while True:
        suffix = f" [{default}]" if default not in (None, "") else ""
        value = input(f"{message}{suffix}: ").strip()
        if value:
            return value
        if default is not None:
            return str(default)
        if allow_empty:
            return ""
        print("Input is required.")


def prompt_choice(message, choices, default):
    normalized_choices = {choice.lower(): choice for choice in choices}
    while True:
        value = prompt_text(message, default=default).lower()
        if value in normalized_choices:
            return normalized_choices[value]
        print(f"Choose one of: {', '.join(choices)}")


def prompt_yes_no(message, default=True):
    default_text = "Y/n" if default else "y/N"
    while True:
        value = input(f"{message} [{default_text}]: ").strip().lower()
        if not value:
            return default
        if value in {"y", "yes"}:
            return True
        if value in {"n", "no"}:
            return False
        print("Please answer yes or no.")


def prompt_int(message, default, minimum=1, maximum=None):
    while True:
        raw_value = prompt_text(message, default=default)
        try:
            value = int(raw_value)
        except ValueError:
            print("Please enter a whole number.")
            continue

        if value < minimum:
            print(f"Value must be at least {minimum}.")
            continue
        if maximum is not None and value > maximum:
            print(f"Value must be at most {maximum}.")
            continue
        return value


def prompt_existing_file(message, default=None):
    while True:
        value = Path(prompt_text(message, default=default)).expanduser()
        if value.is_file():
            return value.resolve()
        print("File not found. Enter a valid file path.")


def prompt_existing_dir(message, default=None):
    while True:
        value = Path(prompt_text(message, default=default)).expanduser()
        if value.is_dir():
            return value.resolve()
        print("Folder not found. Enter a valid folder path.")


def prompt_output_dir(message, default=None):
    while True:
        value = Path(prompt_text(message, default=default)).expanduser()
        try:
            value.mkdir(parents=True, exist_ok=True)
            return value.resolve()
        except OSError as exc:
            print(f"Could not create folder: {exc}")


def run_task_with_progress(title, message, task_func):
    result = {"value": None, "error": None}
    done_event = threading.Event()

    def worker():
        try:
            result["value"] = task_func()
        except Exception as exc:
            result["error"] = exc
        finally:
            done_event.set()

    thread = threading.Thread(target=worker, daemon=True)

    try:
        root = tk.Tk()
    except tk.TclError:
        thread.start()
        thread.join()
        if result["error"]:
            raise result["error"]
        return result["value"]

    root.title(title)
    root.resizable(False, False)

    frame = ttk.Frame(root, padding=14)
    frame.pack(fill="both", expand=True)
    ttk.Label(frame, text=message, justify="left").pack(anchor="w", pady=(0, 8))

    progress = ttk.Progressbar(frame, mode="indeterminate", length=420)
    progress.pack(fill="x")
    progress.start(12)

    ttk.Label(frame, text="Please wait...").pack(anchor="w", pady=(8, 0))

    def poll_worker():
        if done_event.is_set():
            progress.stop()
            root.destroy()
            return
        root.after(120, poll_worker)

    root.protocol("WM_DELETE_WINDOW", lambda: None)
    thread.start()
    root.after(120, poll_worker)
    root.mainloop()

    if result["error"]:
        raise result["error"]
    return result["value"]


def cleanup_temp_folder(folder_path, logger, retries=5, delay=0.5):
    for attempt in range(retries):
        try:
            if folder_path.exists():
                shutil.rmtree(folder_path)
            return
        except PermissionError:
            if attempt < retries - 1:
                time.sleep(delay)
            else:
                logger.warning("Could not delete temp folder: %s", folder_path)


def build_submission_key(submissions_root, pdf_path):
    relative_path = pdf_path.resolve().relative_to(submissions_root.resolve())
    return str(relative_path).replace("\\", "/").lower()


def parse_max_marks(raw_text, question_count, default_marks):
    marks_map = {}
    tokens = [token.strip() for token in raw_text.split(",") if token.strip()]
    if tokens:
        for index, token in enumerate(tokens, start=1):
            if ":" in token:
                question_name, raw_mark = token.split(":", 1)
                question_name = question_name.strip().upper()
            else:
                question_name = f"Q{index}"
                raw_mark = token
            try:
                marks_map[question_name] = max(int(raw_mark.strip()), 0)
            except ValueError:
                continue

    if not marks_map:
        for index in range(1, question_count + 1):
            marks_map[f"Q{index}"] = default_marks

    return marks_map


def normalize_question_key(raw_key):
    key_text = str(raw_key).strip().upper()
    match = re.search(r"(\d{1,3})", key_text)
    if not match:
        return None
    return f"Q{int(match.group(1))}"


def parse_question_marks_map(raw_map, fallback_default_marks):
    parsed = {}
    if not isinstance(raw_map, dict):
        return parsed

    for raw_key, raw_value in raw_map.items():
        question_name = normalize_question_key(raw_key)
        if not question_name:
            continue

        mark_value = fallback_default_marks
        if isinstance(raw_value, dict):
            for field_name in ["max_marks", "marks", "total_marks", "max_mark"]:
                if field_name in raw_value:
                    raw_mark = raw_value.get(field_name)
                    try:
                        mark_value = max(int(raw_mark), 1)
                    except (TypeError, ValueError):
                        pass
                    break
        else:
            try:
                mark_value = max(int(raw_value), 1)
            except (TypeError, ValueError):
                number_match = re.search(r"(\d{1,3})", str(raw_value))
                if number_match:
                    mark_value = max(int(number_match.group(1)), 1)

        parsed[question_name] = mark_value

    return parsed


def extract_pdf_text(pdf_path, page_limit=DEFAULT_PAGE_LIMIT, logger=None, require_text=False):
    if logger:
        logger.info("Extracting text from PDF: %s", pdf_path)

    document = fitz.open(str(pdf_path))
    pages = []
    try:
        max_pages = min(len(document), max(1, int(page_limit)))
        for page_index in range(max_pages):
            page = document.load_page(page_index)
            text = page.get_text("text") or ""
            pages.append(f"--- Page {page_index + 1} ---\n{text.strip()}")
    finally:
        document.close()

    combined = "\n\n".join(chunk for chunk in pages if chunk.strip())
    if not combined.strip():
        if require_text:
            raise RuntimeError(
                "No extractable text found in PDF. For scanned or handwritten PDFs, use a Gemini Flash model instead of Gemma."
            )
        return ""

    max_chars = 120000
    if len(combined) > max_chars:
        if logger:
            logger.info("Truncating extracted text from %s to %s characters for model input.", len(combined), max_chars)
        combined = combined[:max_chars]

    return combined


def detect_questions_and_marks(text):
    heading_pattern = re.compile(r"(?im)^\s*(?:question|ques\.?|q)\s*(\d{1,2})\s*[:.)-]?")
    fallback_numbered_pattern = re.compile(r"(?im)^\s*(\d{1,2})\s*[).:-]\s+")

    question_matches = []
    seen_numbers = set()

    for match in heading_pattern.finditer(text):
        number = int(match.group(1))
        if number in seen_numbers:
            continue
        seen_numbers.add(number)
        question_matches.append((number, match.start()))

    if not question_matches:
        for match in fallback_numbered_pattern.finditer(text):
            number = int(match.group(1))
            if number in seen_numbers:
                continue
            seen_numbers.add(number)
            question_matches.append((number, match.start()))

    question_matches.sort(key=lambda item: item[1])

    if not question_matches:
        return 0, {}

    mark_patterns = [
        re.compile(r"(?i)max(?:imum)?\s*marks?\s*[:=-]?\s*(\d{1,3})"),
        re.compile(r"(?i)\((\d{1,3})\s*marks?\)"),
        re.compile(r"(?i)\[(\d{1,3})\s*marks?\]"),
        re.compile(r"(?i)(\d{1,3})\s*marks?\b"),
    ]

    marks_map = {}
    for index, (number, start_pos) in enumerate(question_matches):
        end_pos = question_matches[index + 1][1] if index + 1 < len(question_matches) else len(text)
        segment = text[start_pos:end_pos]
        mark_candidates = []
        for pattern in mark_patterns:
            mark_candidates.extend(int(item) for item in pattern.findall(segment))
        if mark_candidates:
            marks_map[f"Q{number}"] = max(mark_candidates)

    question_count = max(number for number, _ in question_matches)
    return question_count, marks_map


def auto_detect_assignment_metadata(assignment_pdf, fallback_question_count, fallback_default_marks):
    extracted_text = extract_pdf_text(assignment_pdf, page_limit=METADATA_PAGE_LIMIT)
    detected_count, detected_marks = detect_questions_and_marks(extracted_text)

    if detected_count < 1:
        return {
            "success": False,
            "question_count": fallback_question_count,
            "default_marks": fallback_default_marks,
            "marks_text": "",
            "message": "Could not confidently detect question structure from PDF text.",
        }

    detected_values = list(detected_marks.values())
    inferred_default = (
        max(1, int(round(sum(detected_values) / len(detected_values)))) if detected_values else fallback_default_marks
    )

    complete_marks_map = {}
    for question_index in range(1, detected_count + 1):
        question_name = f"Q{question_index}"
        complete_marks_map[question_name] = detected_marks.get(question_name, inferred_default)

    marks_text = ", ".join(f"{question}:{marks}" for question, marks in complete_marks_map.items())
    return {
        "success": True,
        "question_count": detected_count,
        "default_marks": inferred_default,
        "marks_text": marks_text,
        "message": f"Detected {detected_count} question(s) and inferred per-question marks.",
    }


def infer_question_setup_from_rubric(rubric, fallback_question_count, fallback_default_marks):
    rubric_marks = parse_question_marks_map(rubric, fallback_default_marks)

    if rubric_marks:
        question_count = max(int(question_name[1:]) for question_name in rubric_marks)
    else:
        question_count = max(int(fallback_question_count), 1)

    complete_marks_map = {}
    for index in range(1, question_count + 1):
        question_name = f"Q{index}"
        complete_marks_map[question_name] = rubric_marks.get(question_name, fallback_default_marks)

    default_marks = max(1, int(round(sum(complete_marks_map.values()) / len(complete_marks_map))))
    marks_text = ", ".join(f"{question}:{marks}" for question, marks in complete_marks_map.items())
    return question_count, default_marks, marks_text


def detect_question_metadata_with_gemini(assignment_pdf, logger, model_name=None):
    active_model = normalize_model_name(model_name or DEFAULT_SOLUTION_MODEL)
    tmp_dir = Path.cwd()
    temp_folder = Path(tempfile.mkdtemp(prefix="assignment_meta_pdf_", dir=tmp_dir))
    pil_images = []
    extracted_text = ""
    try:
        if is_text_only_model(active_model):
            extracted_text = extract_pdf_text(assignment_pdf, METADATA_PAGE_LIMIT, logger)
        else:
            image_paths = convert_pdf_to_images(
                assignment_pdf,
                temp_folder,
                METADATA_PAGE_LIMIT,
                METADATA_DPI,
                logger,
            )
            pil_images = open_pil_images(image_paths)
        prompt = """
Extract the complete list of main questions and each question's maximum marks from this assignment PDF.

Return strictly valid JSON in this exact shape:
{
  "questions": {
    "Q1": 3,
    "Q2": 5
  },
  "question_count": 2,
  "notes": "short detection note"
}

Rules:
- Include all main questions visible in the PDF.
- Use contiguous question keys Q1..Qn where possible.
- If a specific max mark is not visible for a question, estimate reasonably based on nearby formatting.
- The response must be valid JSON only.
""".strip()

        response = call_gemini_json(
            prompt,
            pil_images,
            logger,
            METADATA_TIMEOUT_SECONDS,
            METADATA_RETRY_COUNT,
            active_model,
            extracted_text=extracted_text,
        )

        questions_map = parse_question_marks_map(response.get("questions", {}), fallback_default_marks=3)
        reported_count = response.get("question_count", 0)
        try:
            reported_count = int(reported_count)
        except (TypeError, ValueError):
            reported_count = 0

        if questions_map:
            inferred_count = max(int(question_name[1:]) for question_name in questions_map)
        else:
            inferred_count = 0

        final_count = max(reported_count, inferred_count)
        if final_count < 1:
            return {
                "success": False,
                "question_count": 0,
                "marks_map": {},
                "notes": "Gemini metadata extraction did not return usable questions.",
            }

        if questions_map:
            default_mark = max(1, int(round(sum(questions_map.values()) / len(questions_map))))
        else:
            default_mark = 3

        complete_map = {}
        for index in range(1, final_count + 1):
            question_name = f"Q{index}"
            complete_map[question_name] = questions_map.get(question_name, default_mark)

        return {
            "success": True,
            "question_count": final_count,
            "marks_map": complete_map,
            "notes": str(response.get("notes", "")).strip(),
        }
    except Exception as exc:
        logger.warning("Gemini metadata extraction failed: %s", exc)
        return {
            "success": False,
            "question_count": 0,
            "marks_map": {},
            "notes": str(exc),
        }
    finally:
        close_pil_images(pil_images)
        cleanup_temp_folder(temp_folder, logger)


def convert_pdf_to_images(pdf_path, temp_folder, page_limit, dpi, logger):
    temp_folder.mkdir(parents=True, exist_ok=True)

    poppler_kwargs: dict = {}
    if POPPLER_PATH and str(POPPLER_PATH):
        if not POPPLER_PATH.exists():
            raise FileNotFoundError(
                f"Poppler path not found: {POPPLER_PATH}.\n"
                "Set the POPPLER_BIN_PATH environment variable to the folder "
                "that contains pdfinfo / pdfinfo.exe."
            )
        poppler_kwargs["poppler_path"] = str(POPPLER_PATH)

    logger.info("Converting PDF to images: %s", pdf_path)
    images = convert_from_path(
        str(pdf_path),
        dpi=dpi,
        first_page=1,
        last_page=page_limit,
        **poppler_kwargs,
    )

    image_paths = []
    for index, image in enumerate(images, start=1):
        image_path = temp_folder / f"page_{index}.jpg"
        image.save(image_path, "JPEG")
        image.close()
        image_paths.append(image_path)

    logger.info("Converted %s page(s) for %s", len(image_paths), pdf_path.name)
    return image_paths


def open_pil_images(image_paths):
    return [PIL.Image.open(image_path) for image_path in image_paths]


def close_pil_images(images):
    for image in images:
        try:
            image.close()
        except Exception:
            pass


def extract_json_object(text):
    if not text:
        raise ValueError("Empty model response.")

    stripped = text.strip()
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", stripped, flags=re.DOTALL)
        if not match:
            raise
        return json.loads(match.group(0))


def call_gemini_json(prompt, pil_images, logger, timeout_seconds, retry_count, model_name, extracted_text=""):
    generation_config = genai.GenerationConfig(response_mime_type="application/json")
    contents = [prompt]
    if extracted_text:
        contents.append(
            "Document text extracted from the PDF is provided below. "
            "This may be imperfect for scanned/handwritten pages:\n\n"
            f"{extracted_text}"
        )
    contents.extend(pil_images)
    last_error = None
    model = genai.GenerativeModel(model_name)

    for attempt in range(1, retry_count + 1):
        try:
            logger.info("Calling Gemini model %s attempt %s/%s", model_name, attempt, retry_count)
            response = model.generate_content(
                contents,
                generation_config=generation_config,
                request_options={"timeout": timeout_seconds},
            )
            return extract_json_object(getattr(response, "text", ""))
        except Exception as exc:
            last_error = exc
            logger.warning("Gemini call failed on attempt %s: %s", attempt, exc)
            if attempt < retry_count:
                time.sleep(min(2 * attempt, 6))

    raise RuntimeError(f"Gemini request failed after {retry_count} attempts: {last_error}")


def write_text_file(path, content):
    path.write_text(content, encoding="utf-8")


def build_material_cache_path(assignment_pdf, solution_model):
    MATERIAL_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    fingerprint = f"{assignment_pdf.resolve()}|{solution_model}".encode("utf-8")
    cache_name = hashlib.sha1(fingerprint).hexdigest()[:16]
    return MATERIAL_CACHE_DIR / f"{cache_name}.json"


def load_cached_assignment_materials(assignment_pdf, solution_model):
    cache_path = build_material_cache_path(assignment_pdf, solution_model)
    if not cache_path.exists():
        return None

    try:
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
    except Exception:
        return None

    if payload.get("assignment_pdf") != str(assignment_pdf.resolve()):
        return None

    return payload.get("assignment_materials")


def save_cached_assignment_materials(assignment_pdf, solution_model, assignment_materials):
    cache_path = build_material_cache_path(assignment_pdf, solution_model)
    payload = {
        "assignment_pdf": str(assignment_pdf.resolve()),
        "solution_model": solution_model,
        "saved_at": datetime.now().isoformat(timespec="seconds"),
        "assignment_materials": assignment_materials,
    }
    cache_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def load_cached_assignment_materials_with_meta(assignment_pdf, solution_model):
    cache_path = build_material_cache_path(assignment_pdf, solution_model)
    if not cache_path.exists():
        return None, None

    try:
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
    except Exception:
        return None, None

    if payload.get("assignment_pdf") != str(assignment_pdf.resolve()):
        return None, None

    meta = {
        "saved_at": payload.get("saved_at", "unknown"),
        "solution_model": payload.get("solution_model", solution_model),
    }
    return payload.get("assignment_materials"), meta


def append_jsonl(path, payload):
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")


def discover_submission_pdfs(submissions_root, assignment_pdf, output_root):
    excluded_files = {assignment_pdf.resolve()}
    pdf_paths = []
    resolved_output_root = output_root.resolve()

    for pdf_path in submissions_root.rglob("*.pdf"):
        resolved = pdf_path.resolve()
        if resolved in excluded_files:
            continue
        if resolved_output_root in resolved.parents:
            continue
        pdf_paths.append(resolved)

    return sorted(pdf_paths)


def sanitize_grade_payload(grades, max_marks):
    sanitized = {}
    total_score = 0
    for question_name, max_mark in max_marks.items():
        grade_item = grades.get(question_name, {}) if isinstance(grades, dict) else {}
        try:
            score = int(grade_item.get("score", 0))
        except (TypeError, ValueError):
            score = 0
        score = min(max(score, 0), max_mark)
        feedback = str(grade_item.get("feedback", "No feedback provided.")).strip() or "No feedback provided."
        rationale = str(grade_item.get("rationale", "")).strip()
        sanitized[question_name] = {
            "score": score,
            "feedback": feedback,
            "rationale": rationale,
        }
        total_score += score
    return sanitized, total_score


def create_graded_pdf(original_pdf_path, output_folder, output_basename, result_data, max_marks):
    output_folder.mkdir(parents=True, exist_ok=True)
    output_pdf_path = output_folder / f"{output_basename}_graded.pdf"

    doc = fitz.open(str(original_pdf_path))

    total_max = sum(max_marks.values())
    overall_feedback = result_data.get("overall_feedback", "").strip()

    lines = [
        "GRADING REPORT",
        "=" * 60,
        f"Student Name       : {result_data['extracted_name']}",
        f"Student ID         : {result_data['extracted_identifier']}",
        f"Source PDF         : {original_pdf_path.name}",
        f"Total Score        : {result_data['total_score']} / {total_max}",
        f"Strictness         : {result_data['strictness']}",
        f"Feedback Style     : {result_data['feedback_style']}",
        "=" * 60,
        "",
    ]

    if overall_feedback:
        lines.append("OVERALL FEEDBACK")
        lines.append("-" * 40)
        lines.append(overall_feedback)
        lines.append("")

    lines.append("PER-QUESTION BREAKDOWN")
    lines.append("-" * 40)
    for question_name, max_mark in max_marks.items():
        question_result = result_data["grades"].get(question_name, {})
        score = question_result.get("score", 0)
        feedback = question_result.get("feedback", "No feedback provided.")
        rationale = question_result.get("rationale", "").strip()
        lines.append(f"{question_name}  [{score}/{max_mark}]")
        lines.append(f"  Feedback : {feedback}")
        if rationale:
            lines.append(f"  Rationale: {rationale}")
        lines.append("")

    if result_data["status"] != "SUCCESS":
        lines.append(f"Evaluation Status: {result_data['status']}")
        lines.append(result_data.get("status_message", ""))

    PAGE_W, PAGE_H = 595, 842
    MARGIN_X, MARGIN_TOP, MARGIN_BOTTOM = 40, 40, 30
    FONT_SIZE = 10
    LINE_HEIGHT = FONT_SIZE * 1.5
    MAX_Y = PAGE_H - MARGIN_BOTTOM

    cover_pages_inserted = 0

    def new_cover_page():
        nonlocal cover_pages_inserted
        page = doc.new_page(pno=cover_pages_inserted, width=PAGE_W, height=PAGE_H)
        cover_pages_inserted += 1
        return page, MARGIN_TOP

    current_page, y = new_cover_page()

    for line in lines:
        wrapped = []
        while len(line) > 90:
            split_at = line.rfind(" ", 0, 90)
            if split_at == -1:
                split_at = 90
            wrapped.append(line[:split_at])
            line = "  " + line[split_at:].lstrip()
        wrapped.append(line)

        for segment in wrapped:
            if y + LINE_HEIGHT > MAX_Y:
                current_page, y = new_cover_page()
            current_page.insert_text(
                (MARGIN_X, y),
                segment,
                fontsize=FONT_SIZE,
                fontname="helv",
            )
            y += LINE_HEIGHT

    doc.save(str(output_pdf_path))
    doc.close()
    return output_pdf_path


def build_assignment_prompt(config):
    max_marks = config.get("max_marks", {})
    question_list = ", ".join(max_marks.keys()) if max_marks else "Auto-detect from assignment"
    question_count_text = len(max_marks) if max_marks else "Auto-detect from assignment"
    max_marks_text = max_marks if max_marks else "Auto-detect from assignment"
    question_guidance = (
        f"Include all expected questions from {question_list}."
        if max_marks
        else "Infer all questions present in the assignment and include each question in solution_manual and rubric."
    )
    return f"""
You are an expert academic evaluator. Study the assignment document and produce a complete answer key and grading rubric.

Assignment context:
- Course or subject: {config['subject_name']}
- Evaluation strictness target: {config['strictness']}
- Feedback style target: {config['feedback_style']}
- Number of questions expected: {question_count_text}
- Questions: {question_list}
- Max marks: {max_marks_text}
- Student submissions are expected to be {'handwritten' if config['expect_handwritten'] else 'typed'}.
- Additional evaluator notes: {config['evaluator_notes'] or 'None'}

Return strictly valid JSON with this shape:
{{
  "assignment_title": "short title",
  "assignment_summary": "brief summary",
  "recommended_extraction_notes": "how to interpret the submissions",
  "solution_manual": {{
    "Q1": "ideal answer",
    "Q2": "ideal answer"
  }},
  "rubric": {{
    "Q1": {{
      "max_marks": 3,
      "grading_points": ["point 1", "point 2"],
      "common_mistakes": ["mistake 1"]
    }},
    "Q2": {{
      "max_marks": 3,
      "grading_points": ["point 1"],
      "common_mistakes": []
    }}
  }}
}}

Make the rubric practical for grading incomplete handwritten responses.
{question_guidance}
""".strip()


def generate_assignment_materials(config, assignment_pdf, session_dir, logger):
    tmp_dir = Path.cwd()
    temp_folder = Path(tempfile.mkdtemp(prefix="assignment_pdf_", dir=tmp_dir))
    pil_images = []
    extracted_text = ""
    try:
        if is_text_only_model(config["solution_model"]):
            extracted_text = extract_pdf_text(
                assignment_pdf,
                config["page_limit"],
                logger,
                require_text=True,
            )
        else:
            image_paths = convert_pdf_to_images(
                assignment_pdf,
                temp_folder,
                config["page_limit"],
                config["dpi"],
                logger,
            )
            pil_images = open_pil_images(image_paths)
        prompt = build_assignment_prompt(config)
        result = call_gemini_json(
            prompt,
            pil_images,
            logger,
            config["timeout_seconds"],
            config["retry_count"],
            config["solution_model"],
            extracted_text=extracted_text,
        )

        solution_manual = result.get("solution_manual", {})
        rubric = result.get("rubric", {})
        assignment_summary = str(result.get("assignment_summary", "")).strip()
        assignment_title = str(result.get("assignment_title", assignment_pdf.stem)).strip() or assignment_pdf.stem
        extraction_notes = str(result.get("recommended_extraction_notes", "")).strip()

        write_text_file(session_dir / "solution_manual.txt", json.dumps(solution_manual, indent=2, ensure_ascii=False))
        write_text_file(session_dir / "rubric.json", json.dumps(rubric, indent=2, ensure_ascii=False))
        write_text_file(session_dir / "assignment_summary.txt", assignment_summary)

        logger.info("Generated assignment materials for %s", assignment_pdf.name)
        return {
            "assignment_title": assignment_title,
            "assignment_summary": assignment_summary,
            "recommended_extraction_notes": extraction_notes,
            "solution_manual": solution_manual,
            "rubric": rubric,
        }
    finally:
        close_pil_images(pil_images)
        cleanup_temp_folder(temp_folder, logger)


def build_grading_prompt(config, assignment_materials, submission_pdf_name):
    return f"""
You are grading one student submission against the provided solution manual and rubric.

Evaluation instructions:
- Subject: {config['subject_name']}
- Strictness: {config['strictness']}
- Feedback style: {config['feedback_style']}
- Handwriting expected: {config['expect_handwritten']}
- Questions and max marks: {config['max_marks']}
- Additional evaluator notes: {config['evaluator_notes'] or 'None'}
- Extraction notes: {assignment_materials['recommended_extraction_notes'] or 'None'}
- Submission file name: {submission_pdf_name}

Solution manual:
{json.dumps(assignment_materials['solution_manual'], indent=2, ensure_ascii=False)}

Rubric:
{json.dumps(assignment_materials['rubric'], indent=2, ensure_ascii=False)}

Return strictly valid JSON with this shape:
{{
  "student_info": {{
    "extracted_name": "student name or Unknown",
    "extracted_identifier": "sap id, roll number, or Unknown"
  }},
  "grades": {{
    "Q1": {{"score": 0, "feedback": "brief feedback", "rationale": "why"}},
    "Q2": {{"score": 0, "feedback": "brief feedback", "rationale": "why"}}
  }},
  "overall_feedback": "short overall summary"
}}

Rules:
- Grade every question listed in {config['max_marks']}.
- If an answer is missing or unreadable, give 0 with a clear explanation.
- Do not exceed the specified max marks.
- Keep feedback concise but specific.
""".strip()


def evaluate_single_submission(pdf_path, submissions_root, output_root, config, assignment_materials, logger):
    submission_key = build_submission_key(submissions_root, pdf_path)
    tmp_dir = Path.cwd()
    temp_folder = Path(tempfile.mkdtemp(prefix="submission_pdf_", dir=tmp_dir))
    pil_images = []
    extracted_text = ""
    started_at = datetime.now().isoformat(timespec="seconds")

    result_data = {
        "submission_key": submission_key,
        "source_pdf": str(pdf_path),
        "relative_pdf": str(pdf_path.resolve().relative_to(submissions_root.resolve())).replace("\\", "/"),
        "started_at": started_at,
        "finished_at": None,
        "status": "FAILED",
        "status_message": "Evaluation did not complete.",
        "strictness": config["strictness"],
        "feedback_style": config["feedback_style"],
        "extracted_name": "Unknown",
        "extracted_identifier": "Unknown",
        "overall_feedback": "",
        "grades": {},
        "total_score": 0,
        "graded_pdf": "",
        "error": "",
    }

    try:
        if is_text_only_model(config["evaluation_model"]):
            extracted_text = extract_pdf_text(
                pdf_path,
                config["page_limit"],
                logger,
                require_text=True,
            )
        else:
            image_paths = convert_pdf_to_images(
                pdf_path,
                temp_folder,
                config["page_limit"],
                config["dpi"],
                logger,
            )
            pil_images = open_pil_images(image_paths)
        prompt = build_grading_prompt(config, assignment_materials, pdf_path.name)
        response_data = call_gemini_json(
            prompt,
            pil_images,
            logger,
            config["timeout_seconds"],
            config["retry_count"],
            config["evaluation_model"],
            extracted_text=extracted_text,
        )

        student_info = response_data.get("student_info", {}) if isinstance(response_data, dict) else {}
        result_data["extracted_name"] = str(student_info.get("extracted_name", "Unknown")).strip() or "Unknown"
        result_data["extracted_identifier"] = (
            str(student_info.get("extracted_identifier", "Unknown")).strip() or "Unknown"
        )
        result_data["overall_feedback"] = str(response_data.get("overall_feedback", "")).strip()

        sanitized_grades, total_score = sanitize_grade_payload(response_data.get("grades", {}), config["max_marks"])
        result_data["grades"] = sanitized_grades
        result_data["total_score"] = total_score
        result_data["status"] = "SUCCESS"
        result_data["status_message"] = "Submission evaluated successfully."

        relative_parent = pdf_path.resolve().parent.relative_to(submissions_root.resolve())
        output_folder = output_root / relative_parent
        output_basename = normalize_output_name(pdf_path.stem, max_length=80)
        graded_pdf_path = create_graded_pdf(
            pdf_path,
            output_folder,
            output_basename,
            result_data,
            config["max_marks"],
        )
        result_data["graded_pdf"] = str(graded_pdf_path)
        logger.info("Evaluated %s successfully", pdf_path.name)
    except Exception as exc:
        result_data["error"] = str(exc)
        result_data["status"] = "FAILED"
        result_data["status_message"] = f"Evaluation failed: {exc}"
        logger.exception("Failed to evaluate %s", pdf_path)
    finally:
        result_data["finished_at"] = datetime.now().isoformat(timespec="seconds")
        close_pil_images(pil_images)
        cleanup_temp_folder(temp_folder, logger)

    return result_data


def load_existing_results(output_excel, logger):
    records = []
    processed_keys = set()
    if output_excel.exists():
        logger.info("Found existing results file: %s", output_excel)
        try:
            existing_df = pd.read_excel(output_excel)
            records = existing_df.to_dict("records")
            if "Submission Key" in existing_df.columns:
                processed_keys = set(existing_df["Submission Key"].dropna().astype(str))
        except Exception as exc:
            logger.warning("Could not load existing results file. Starting fresh. Error: %s", exc)
    return records, processed_keys


def result_to_row(result_data, config):
    total_max = sum(config["max_marks"].values())
    row = {
        "Student Name": result_data["extracted_name"],
        "Student ID": result_data["extracted_identifier"],
        "Source PDF": result_data["relative_pdf"],
        "Status": result_data["status"],
        "Graded PDF": result_data["graded_pdf"],
    }
    for question_name, max_mark in config["max_marks"].items():
        question_result = result_data["grades"].get(question_name, {})
        score = question_result.get("score", 0)
        row[f"{question_name} ({max_mark})"] = score
    row["TOTAL"] = result_data["total_score"]
    row["MAX"] = total_max
    return row


def save_results(output_excel, records):
    dataframe = pd.DataFrame(records)
    dataframe.to_excel(output_excel, index=False)


def ask_use_cached_materials(saved_at, solution_model):
    result = {"choice": "cancel"}

    try:
        root = tk.Tk()
    except tk.TclError:
        return "use"

    root.title("Cached Rubric Found")
    root.resizable(False, False)

    frame = ttk.Frame(root, padding=16)
    frame.pack(fill="both", expand=True)

    ttk.Label(
        frame,
        text="A rubric and solution manual from a previous run were found.",
        justify="left",
    ).pack(anchor="w", pady=(0, 4))
    ttk.Label(frame, text=f"Saved:   {saved_at}", justify="left").pack(anchor="w")
    ttk.Label(frame, text=f"Model:   {solution_model}", justify="left").pack(anchor="w", pady=(0, 12))
    ttk.Label(
        frame,
        text="Use the cached version, or generate a new one?",
        justify="left",
    ).pack(anchor="w", pady=(0, 12))

    button_row = ttk.Frame(frame)
    button_row.pack(anchor="e")

    def on_cancel():
        result["choice"] = "cancel"
        root.destroy()

    def on_regenerate():
        result["choice"] = "regenerate"
        root.destroy()

    def on_use():
        result["choice"] = "use"
        root.destroy()

    ttk.Button(button_row, text="Cancel", command=on_cancel).grid(row=0, column=0, padx=(0, 8))
    ttk.Button(button_row, text="Regenerate", command=on_regenerate).grid(row=0, column=1, padx=(0, 8))
    ttk.Button(button_row, text="Use Cached", command=on_use).grid(row=0, column=2)

    root.bind("<Escape>", lambda e: on_cancel())
    root.mainloop()

    return result["choice"]


def ask_reevaluate_submissions(count):
    try:
        root = tk.Tk()
        root.withdraw()
        answer = messagebox.askyesno(
            "Previously Evaluated Submissions",
            f"{count} submission(s) were already evaluated in a previous run.\n\nDo you want to reevaluate them?",
            parent=root,
        )
        root.destroy()
        return answer
    except tk.TclError:
        return False


def collect_assignment_pdf_input():
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        raise RuntimeError(f"Could not start the GUI: {exc}") from exc

    root.title("Step 1 - Assignment PDF")
    root.resizable(False, False)
    root.columnconfigure(1, weight=1)

    assignment_pdf_var = tk.StringVar()
    solution_model_var = tk.StringVar(value=DEFAULT_SOLUTION_MODEL)
    result = {"submitted": False, "assignment_pdf": None, "solution_model": DEFAULT_SOLUTION_MODEL}

    def browse_assignment_pdf():
        selected = filedialog.askopenfilename(
            title="Select assignment PDF",
            filetypes=[("PDF files", "*.pdf")],
        )
        if selected:
            assignment_pdf_var.set(selected)

    def submit_step():
        assignment_pdf = Path(assignment_pdf_var.get().strip()).expanduser()
        if not assignment_pdf.is_file():
            messagebox.showerror("Invalid input", "Select a valid assignment PDF file.", parent=root)
            return
        selected_model = normalize_model_name(solution_model_var.get().strip() or DEFAULT_SOLUTION_MODEL)
        result["assignment_pdf"] = assignment_pdf.resolve()
        result["solution_model"] = selected_model
        result["submitted"] = True
        root.destroy()

    def cancel_step():
        root.destroy()

    content = ttk.Frame(root, padding=16)
    content.grid(row=0, column=0, sticky="nsew")
    content.columnconfigure(1, weight=1)

    ttk.Label(
        content,
        text="Step 1: Select assignment PDF. The app will generate the solution manual and rubric next.",
    ).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 12))

    ttk.Label(content, text="Assignment PDF").grid(row=1, column=0, sticky="w", padx=(0, 8), pady=4)
    ttk.Entry(content, textvariable=assignment_pdf_var, width=60).grid(row=1, column=1, sticky="ew", pady=4)
    ttk.Button(content, text="Browse", command=browse_assignment_pdf).grid(row=1, column=2, padx=(8, 0), pady=4)

    ttk.Label(content, text="Solution/Rubric model").grid(row=2, column=0, sticky="w", padx=(0, 8), pady=4)
    ttk.Combobox(
        content,
        textvariable=solution_model_var,
        values=AVAILABLE_MODELS,
        state="readonly",
        width=30,
    ).grid(row=2, column=1, columnspan=2, sticky="ew", pady=4)

    actions = ttk.Frame(content)
    actions.grid(row=3, column=0, columnspan=3, sticky="e", pady=(16, 0))
    ttk.Button(actions, text="Cancel", command=cancel_step).grid(row=0, column=0, padx=(0, 8))
    ttk.Button(actions, text="Generate Rubric", command=submit_step).grid(row=0, column=1)

    root.bind("<Return>", lambda event: submit_step())
    root.bind("<Escape>", lambda event: cancel_step())
    root.mainloop()

    if not result["submitted"]:
        raise KeyboardInterrupt("Configuration cancelled.")

    return {
        "assignment_pdf": result["assignment_pdf"],
        "solution_model": result["solution_model"],
    }


def review_assignment_materials(assignment_pdf, assignment_materials, current_solution_model):
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        raise RuntimeError(f"Could not start the review window: {exc}") from exc

    root.title("Review Generated Solution and Rubric")
    root.geometry("980x700")

    model_var = tk.StringVar(value=current_solution_model)
    result = {
        "action": "cancel",
        "solution_model": current_solution_model,
    }

    container = ttk.Frame(root, padding=12)
    container.pack(fill="both", expand=True)

    title_text = assignment_materials.get("assignment_title", assignment_pdf.stem) or assignment_pdf.stem
    summary_text = assignment_materials.get("assignment_summary", "")
    extraction_notes = assignment_materials.get("recommended_extraction_notes", "")

    header = (
        f"Assignment: {title_text}\n"
        f"PDF: {assignment_pdf.name}\n"
        f"Summary: {summary_text or 'N/A'}\n"
        f"Extraction Notes: {extraction_notes or 'N/A'}"
    )
    ttk.Label(container, text=header, justify="left").pack(anchor="w", pady=(0, 8))

    body = ttk.Panedwindow(container, orient="horizontal")
    body.pack(fill="both", expand=True)

    solution_frame = ttk.Labelframe(body, text="Generated Solution Manual", padding=8)
    rubric_frame = ttk.Labelframe(body, text="Generated Rubric", padding=8)
    body.add(solution_frame, weight=1)
    body.add(rubric_frame, weight=1)

    solution_text = scrolledtext.ScrolledText(solution_frame, wrap="word", height=30)
    solution_text.pack(fill="both", expand=True)
    solution_text.insert(
        "1.0",
        json.dumps(assignment_materials.get("solution_manual", {}), indent=2, ensure_ascii=False),
    )
    solution_text.configure(state="disabled")

    rubric_text = scrolledtext.ScrolledText(rubric_frame, wrap="word", height=30)
    rubric_text.pack(fill="both", expand=True)
    rubric_text.insert(
        "1.0",
        json.dumps(assignment_materials.get("rubric", {}), indent=2, ensure_ascii=False),
    )
    rubric_text.configure(state="disabled")

    action_row = ttk.Frame(container)
    action_row.pack(fill="x", pady=(10, 0))

    ttk.Label(action_row, text="Regenerate model:").pack(side="left")
    ttk.Combobox(
        action_row,
        textvariable=model_var,
        values=AVAILABLE_MODELS,
        state="normal",
        width=30,
    ).pack(side="left", padx=(8, 0))

    ttk.Label(
        action_row,
        text="Approve to continue, regenerate with selected model, or cancel.",
    ).pack(side="left", padx=(12, 0))

    button_row = ttk.Frame(container)
    button_row.pack(fill="x", pady=(8, 0))

    def cancel_review():
        result["action"] = "cancel"
        root.destroy()

    def regenerate_review():
        result["action"] = "regenerate"
        result["solution_model"] = model_var.get().strip() or DEFAULT_SOLUTION_MODEL
        root.destroy()

    def approve_review():
        result["action"] = "approve"
        result["solution_model"] = model_var.get().strip() or DEFAULT_SOLUTION_MODEL
        root.destroy()

    ttk.Button(button_row, text="Cancel", command=cancel_review).pack(side="right")
    ttk.Button(button_row, text="Regenerate", command=regenerate_review).pack(side="right", padx=(0, 8))
    ttk.Button(button_row, text="Approve and Continue", command=approve_review).pack(side="right", padx=(0, 8))

    root.bind("<Escape>", lambda event: cancel_review())
    root.mainloop()

    return result


def evaluate_submissions_with_progress(
    submission_pdfs,
    config,
    assignment_materials,
    logger,
    records,
    processed_keys,
    audit_log_path,
):
    skipped_count = 0
    evaluated_count = 0
    failed_count = 0

    progress_root = None
    status_var = None
    count_var = None
    progress_bar = None

    try:
        progress_root = tk.Tk()
        progress_root.title("Evaluation Progress")
        progress_root.resizable(False, False)

        frame = ttk.Frame(progress_root, padding=14)
        frame.pack(fill="both", expand=True)

        status_var = tk.StringVar(value="Preparing evaluation...")
        count_var = tk.StringVar(value=f"0 / {len(submission_pdfs)} processed")

        ttk.Label(frame, textvariable=status_var, justify="left").pack(anchor="w", pady=(0, 6))
        progress_bar = ttk.Progressbar(
            frame, mode="determinate", length=500, maximum=max(len(submission_pdfs), 1)
        )
        progress_bar.pack(fill="x")
        ttk.Label(frame, textvariable=count_var).pack(anchor="w", pady=(6, 0))

        progress_root.update_idletasks()
    except tk.TclError:
        progress_root = None

    for index, pdf_path in enumerate(submission_pdfs, start=1):
        submission_key = build_submission_key(config["submissions_root"], pdf_path)

        if progress_root is not None:
            status_var.set(f"Processing {index}/{len(submission_pdfs)}: {pdf_path.name}")
            count_var.set(f"{index - 1} / {len(submission_pdfs)} processed")
            progress_root.update_idletasks()
            progress_root.update()

        if submission_key in processed_keys:
            logger.info("Skipping already processed submission: %s", submission_key)
            skipped_count += 1
        else:
            print(f"\nEvaluating: {pdf_path.name}")
            result_data = evaluate_single_submission(
                pdf_path,
                config["submissions_root"],
                config["output_root"],
                config,
                assignment_materials,
                logger,
            )
            append_jsonl(audit_log_path, result_data)
            records.append(result_to_row(result_data, config))
            save_results(config["results_excel"], records)
            processed_keys.add(submission_key)

            if result_data["status"] == "SUCCESS":
                evaluated_count += 1
                q_parts = ", ".join(
                    f"{q}: {result_data['grades'].get(q, {}).get('score', 0)}/{m}"
                    for q, m in config["max_marks"].items()
                )
                total_max = sum(config["max_marks"].values())
                print(
                    f"  Name: {result_data['extracted_name']} | "
                    f"{q_parts} | Total: {result_data['total_score']}/{total_max}"
                )
            else:
                failed_count += 1

            time.sleep(config["delay_seconds"])

        if progress_root is not None:
            progress_bar["value"] = index
            count_var.set(f"{index} / {len(submission_pdfs)} processed")
            progress_root.update_idletasks()
            progress_root.update()

    if progress_root is not None:
        status_var.set("Evaluation run completed.")
        progress_root.update_idletasks()
        progress_root.after(600, progress_root.destroy)
        progress_root.mainloop()

    return evaluated_count, skipped_count, failed_count


def collect_evaluation_inputs(assignment_pdf, defaults):
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        raise RuntimeError(f"Could not start the GUI: {exc}") from exc

    root.title("Step 2 - Evaluation Setup")
    root.resizable(False, False)
    root.columnconfigure(1, weight=1)

    submissions_root_var = tk.StringVar(value=str(defaults["submissions_root"]))
    output_root_var = tk.StringVar(value=str(defaults["output_root"]))
    subject_name_var = tk.StringVar(value=defaults["subject_name"])
    question_count_var = tk.StringVar(value=str(defaults["question_count"]))
    default_marks_var = tk.StringVar(value=str(defaults["default_marks"]))
    marks_text_var = tk.StringVar(value=defaults["marks_text"])
    strictness_var = tk.StringVar(value=defaults["strictness"])
    feedback_style_var = tk.StringVar(value=defaults["feedback_style"])
    solution_model_var = tk.StringVar(value=defaults["solution_model"])
    evaluation_model_var = tk.StringVar(value=defaults["evaluation_model"])
    expect_handwritten_var = tk.BooleanVar(value=defaults["expect_handwritten"])

    result = {"submitted": False, "payload": None}

    def browse_submissions_root():
        selected = filedialog.askdirectory(title="Select submissions folder")
        if selected:
            submissions_root_var.set(selected)

    def browse_output_root():
        selected = filedialog.askdirectory(title="Select graded output folder")
        if selected:
            output_root_var.set(selected)

    def submit_step():
        submissions_root = Path(submissions_root_var.get().strip()).expanduser()
        output_root = Path(output_root_var.get().strip()).expanduser()

        if not submissions_root.is_dir():
            messagebox.showerror("Invalid input", "Select a valid submissions folder.", parent=root)
            return

        try:
            output_root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            messagebox.showerror("Invalid input", f"Could not create output folder: {exc}", parent=root)
            return

        try:
            question_count = int(question_count_var.get().strip())
            default_marks = int(default_marks_var.get().strip())
        except ValueError:
            messagebox.showerror(
                "Invalid input", "Question count and default marks must be whole numbers.", parent=root
            )
            return

        if not 1 <= question_count <= MAX_QUESTION_COUNT:
            messagebox.showerror(
                "Invalid input",
                f"Question count must be between 1 and {MAX_QUESTION_COUNT}.",
                parent=root,
            )
            return
        if not 1 <= default_marks <= 100:
            messagebox.showerror("Invalid input", "Default max marks must be between 1 and 100.", parent=root)
            return

        result["payload"] = {
            "assignment_pdf": assignment_pdf,
            "submissions_root": submissions_root.resolve(),
            "output_root": output_root.resolve(),
            "subject_name": subject_name_var.get().strip() or assignment_pdf.stem,
            "question_count": question_count,
            "default_marks": default_marks,
            "marks_text": marks_text_var.get().strip(),
            "strictness": strictness_var.get(),
            "feedback_style": feedback_style_var.get(),
            "solution_model": normalize_model_name(solution_model_var.get().strip() or DEFAULT_SOLUTION_MODEL),
            "evaluation_model": normalize_model_name(evaluation_model_var.get().strip() or DEFAULT_EVALUATION_MODEL),
            "expect_handwritten": expect_handwritten_var.get(),
        }
        result["submitted"] = True
        root.destroy()

    def cancel_step():
        root.destroy()

    content = ttk.Frame(root, padding=16)
    content.grid(row=0, column=0, sticky="nsew")
    content.columnconfigure(1, weight=1)

    ttk.Label(
        content,
        text="Step 2: Review detected questions and marks, then configure evaluation options.",
    ).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 12))

    ttk.Label(content, text=f"Assignment PDF: {assignment_pdf.name}").grid(
        row=1, column=0, columnspan=3, sticky="w", pady=(0, 8),
    )

    ttk.Label(content, text="Submissions folder").grid(row=2, column=0, sticky="w", padx=(0, 8), pady=4)
    ttk.Entry(content, textvariable=submissions_root_var, width=60).grid(row=2, column=1, sticky="ew", pady=4)
    ttk.Button(content, text="Browse", command=browse_submissions_root).grid(row=2, column=2, padx=(8, 0), pady=4)

    ttk.Label(content, text="Output folder").grid(row=3, column=0, sticky="w", padx=(0, 8), pady=4)
    ttk.Entry(content, textvariable=output_root_var, width=60).grid(row=3, column=1, sticky="ew", pady=4)
    ttk.Button(content, text="Browse", command=browse_output_root).grid(row=3, column=2, padx=(8, 0), pady=4)

    ttk.Label(content, text="Subject or course").grid(row=4, column=0, sticky="w", padx=(0, 8), pady=4)
    ttk.Entry(content, textvariable=subject_name_var, width=60).grid(row=4, column=1, columnspan=2, sticky="ew", pady=4)

    ttk.Label(content, text="Question count").grid(row=5, column=0, sticky="w", padx=(0, 8), pady=4)
    ttk.Entry(content, textvariable=question_count_var, width=12).grid(row=5, column=1, sticky="w", pady=4)

    ttk.Label(content, text="Default max marks").grid(row=6, column=0, sticky="w", padx=(0, 8), pady=4)
    ttk.Entry(content, textvariable=default_marks_var, width=12).grid(row=6, column=1, sticky="w", pady=4)

    ttk.Label(content, text="Marks override").grid(row=7, column=0, sticky="w", padx=(0, 8), pady=4)
    ttk.Entry(content, textvariable=marks_text_var, width=60).grid(row=7, column=1, columnspan=2, sticky="ew", pady=4)

    ttk.Label(content, text="Strictness").grid(row=8, column=0, sticky="w", padx=(0, 8), pady=4)
    ttk.Combobox(
        content, textvariable=strictness_var,
        values=["lenient", "moderate", "strict"], state="readonly", width=18,
    ).grid(row=8, column=1, sticky="w", pady=4)

    ttk.Label(content, text="Feedback style").grid(row=9, column=0, sticky="w", padx=(0, 8), pady=4)
    ttk.Combobox(
        content, textvariable=feedback_style_var,
        values=["brief", "balanced", "detailed"], state="readonly", width=18,
    ).grid(row=9, column=1, sticky="w", pady=4)

    ttk.Label(content, text="Solution model").grid(row=10, column=0, sticky="w", padx=(0, 8), pady=4)
    ttk.Combobox(
        content, textvariable=solution_model_var,
        values=AVAILABLE_MODELS, state="readonly", width=30,
    ).grid(row=10, column=1, columnspan=2, sticky="ew", pady=4)

    ttk.Label(content, text="Evaluation model").grid(row=11, column=0, sticky="w", padx=(0, 8), pady=4)
    ttk.Combobox(
        content, textvariable=evaluation_model_var,
        values=AVAILABLE_MODELS, state="readonly", width=30,
    ).grid(row=11, column=1, columnspan=2, sticky="ew", pady=4)

    ttk.Checkbutton(
        content, text="Most submissions are handwritten", variable=expect_handwritten_var,
    ).grid(row=12, column=0, columnspan=3, sticky="w", pady=(8, 0))

    actions = ttk.Frame(content)
    actions.grid(row=13, column=0, columnspan=3, sticky="e", pady=(16, 0))
    ttk.Button(actions, text="Cancel", command=cancel_step).grid(row=0, column=0, padx=(0, 8))
    ttk.Button(actions, text="Start Evaluation", command=submit_step).grid(row=0, column=1)

    root.bind("<Return>", lambda event: submit_step())
    root.bind("<Escape>", lambda event: cancel_step())
    root.mainloop()

    if not result["submitted"]:
        raise KeyboardInterrupt("Configuration cancelled.")

    return result["payload"]


def collect_user_config():
    print("=" * 70)
    print("Assignment Evaluator")
    print("=" * 70)

    step1_inputs = collect_assignment_pdf_input()
    assignment_pdf = step1_inputs["assignment_pdf"]
    selected_solution_model = step1_inputs["solution_model"]
    session_name = normalize_output_name(f"assignment_eval_{datetime.now().strftime('%Y%m%d_%H%M%S')}", max_length=60)
    logger, session_dir = setup_logging(session_name)

    detected = auto_detect_assignment_metadata(assignment_pdf, fallback_question_count=10, fallback_default_marks=3)
    detected_question_count = detected["question_count"] if detected["success"] else 10
    detected_default_marks = detected["default_marks"] if detected["success"] else 3
    detected_marks_text = detected["marks_text"] if detected["success"] else ""
    detected_max_marks = parse_max_marks(detected_marks_text, detected_question_count, detected_default_marks)

    gemini_meta = run_task_with_progress(
        "Detecting Questions",
        "Extracting question count and marks from assignment PDF...",
        lambda: detect_question_metadata_with_gemini(assignment_pdf, logger, selected_solution_model),
    )
    gemini_marks_map = gemini_meta.get("marks_map", {}) if gemini_meta.get("success") else {}
    if gemini_marks_map:
        base_marks_map = gemini_marks_map
    elif detected_max_marks:
        base_marks_map = detected_max_marks
    else:
        base_marks_map = {f"Q{index}": 3 for index in range(1, 11)}

    force_regenerate = False
    while True:
        if not force_regenerate:
            cached_materials, cache_meta = load_cached_assignment_materials_with_meta(
                assignment_pdf, selected_solution_model
            )
        else:
            cached_materials, cache_meta = None, None
        force_regenerate = False

        if cached_materials:
            cache_choice = ask_use_cached_materials(
                cache_meta["saved_at"],
                cache_meta["solution_model"],
            )
            if cache_choice == "cancel":
                raise KeyboardInterrupt("Configuration cancelled.")
            if cache_choice == "regenerate":
                force_regenerate = True
                continue
            review_result = review_assignment_materials(
                assignment_pdf,
                cached_materials,
                selected_solution_model,
            )
            if review_result["action"] == "approve":
                selected_solution_model = review_result["solution_model"]
                assignment_materials = cached_materials
                break
            if review_result["action"] == "regenerate":
                selected_solution_model = review_result["solution_model"]
                force_regenerate = True
                continue
            raise KeyboardInterrupt("Configuration cancelled during rubric/solution review.")

        pre_generation_config = {
            "subject_name": assignment_pdf.stem,
            "strictness": DEFAULT_STRICTNESS,
            "feedback_style": DEFAULT_FEEDBACK_STYLE,
            "max_marks": base_marks_map,
            "expect_handwritten": DEFAULT_EXPECT_HANDWRITTEN,
            "evaluator_notes": "",
            "page_limit": DEFAULT_PAGE_LIMIT,
            "dpi": DEFAULT_DPI,
            "timeout_seconds": DEFAULT_TIMEOUT_SECONDS,
            "retry_count": DEFAULT_RETRY_COUNT,
            "solution_model": selected_solution_model,
        }

        try:
            assignment_materials = run_task_with_progress(
                "Generating Materials",
                f"Generating solution manual and rubric using model: {selected_solution_model}",
                lambda: generate_assignment_materials(
                    pre_generation_config,
                    assignment_pdf,
                    session_dir,
                    logger,
                ),
            )
        except Exception as exc:
            logger.exception("Could not generate assignment materials.")
            raise RuntimeError(f"Could not generate assignment materials: {exc}") from exc

        save_cached_assignment_materials(
            assignment_pdf,
            selected_solution_model,
            assignment_materials,
        )

        review_result = review_assignment_materials(
            assignment_pdf,
            assignment_materials,
            selected_solution_model,
        )

        if review_result["action"] == "approve":
            selected_solution_model = review_result["solution_model"]
            break
        if review_result["action"] == "regenerate":
            selected_solution_model = review_result["solution_model"]
            force_regenerate = True
            continue
        raise KeyboardInterrupt("Configuration cancelled during rubric/solution review.")

    rubric_question_count, rubric_default_marks, rubric_marks_text = infer_question_setup_from_rubric(
        assignment_materials.get("rubric", {}),
        fallback_question_count=detected_question_count,
        fallback_default_marks=detected_default_marks,
    )

    rubric_marks_map = parse_question_marks_map(assignment_materials.get("rubric", {}), fallback_default_marks=3)
    merged_count = max(
        rubric_question_count,
        gemini_meta.get("question_count", 0) if gemini_meta.get("success") else 0,
        detected_question_count,
    )
    if merged_count < 1:
        merged_count = 10

    merged_default_marks = rubric_default_marks if rubric_default_marks >= 1 else detected_default_marks
    merged_marks_map = {}
    for index in range(1, merged_count + 1):
        question_name = f"Q{index}"
        merged_marks_map[question_name] = rubric_marks_map.get(
            question_name,
            base_marks_map.get(question_name, merged_default_marks),
        )

    merged_marks_text = ", ".join(f"{question}:{marks}" for question, marks in merged_marks_map.items())

    gui_inputs = collect_evaluation_inputs(
        assignment_pdf,
        {
            "submissions_root": Path.cwd(),
            "output_root": Path.cwd() / "Graded_Assignments",
            "subject_name": assignment_materials.get("assignment_title", assignment_pdf.stem) or assignment_pdf.stem,
            "question_count": merged_count,
            "default_marks": merged_default_marks,
            "marks_text": merged_marks_text,
            "strictness": DEFAULT_STRICTNESS,
            "feedback_style": DEFAULT_FEEDBACK_STYLE,
            "solution_model": selected_solution_model,
            "evaluation_model": DEFAULT_EVALUATION_MODEL,
            "expect_handwritten": DEFAULT_EXPECT_HANDWRITTEN,
        },
    )

    submissions_root = gui_inputs["submissions_root"]
    output_root = gui_inputs["output_root"]
    results_excel = output_root / DEFAULT_EXCEL_NAME

    subject_name = gui_inputs["subject_name"]
    question_count = gui_inputs["question_count"]
    default_marks = gui_inputs["default_marks"]
    marks_text = gui_inputs["marks_text"]
    strictness = gui_inputs["strictness"]
    feedback_style = gui_inputs["feedback_style"]
    expect_handwritten = gui_inputs["expect_handwritten"]
    evaluator_notes = ""
    page_limit = DEFAULT_PAGE_LIMIT
    dpi = DEFAULT_DPI
    timeout_seconds = DEFAULT_TIMEOUT_SECONDS
    retry_count = DEFAULT_RETRY_COUNT
    delay_seconds = DEFAULT_DELAY_SECONDS
    reuse_existing_materials = False

    max_marks = parse_max_marks(marks_text, question_count, default_marks)
    if len(max_marks) != question_count:
        max_marks = {f"Q{index}": max_marks.get(f"Q{index}", default_marks) for index in range(1, question_count + 1)}

    config = {
        "session_name": session_name,
        "session_dir": session_dir,
        "logger": logger,
        "assignment_materials": assignment_materials,
        "assignment_pdf": assignment_pdf,
        "submissions_root": submissions_root,
        "output_root": output_root,
        "results_excel": results_excel,
        "subject_name": subject_name,
        "question_count": question_count,
        "max_marks": max_marks,
        "strictness": strictness,
        "feedback_style": feedback_style,
        "expect_handwritten": expect_handwritten,
        "solution_model": gui_inputs["solution_model"],
        "evaluation_model": gui_inputs["evaluation_model"],
        "evaluator_notes": evaluator_notes,
        "page_limit": page_limit,
        "dpi": dpi,
        "timeout_seconds": timeout_seconds,
        "retry_count": retry_count,
        "delay_seconds": delay_seconds,
        "reuse_existing_materials": reuse_existing_materials,
    }

    write_text_file(
        session_dir / "session_config.json",
        json.dumps(
            {
                key: str(value) if isinstance(value, Path) else value
                for key, value in config.items()
                if key not in {"logger", "assignment_materials"}
            },
            indent=2,
            ensure_ascii=False,
        ),
    )

    logger.info("Session initialized in %s", session_dir)
    return config


def load_or_generate_assignment_materials(config):
    logger = config["logger"]
    session_dir = config["session_dir"]
    if config["reuse_existing_materials"]:
        solution_path = session_dir / "solution_manual.txt"
        rubric_path = session_dir / "rubric.json"
        summary_path = session_dir / "assignment_summary.txt"
        if solution_path.exists() and rubric_path.exists() and summary_path.exists():
            logger.info("Reusing existing assignment materials from %s", session_dir)
            return {
                "assignment_title": config["assignment_pdf"].stem,
                "assignment_summary": summary_path.read_text(encoding="utf-8"),
                "recommended_extraction_notes": "",
                "solution_manual": json.loads(solution_path.read_text(encoding="utf-8")),
                "rubric": json.loads(rubric_path.read_text(encoding="utf-8")),
            }

    return generate_assignment_materials(
        config,
        config["assignment_pdf"],
        session_dir,
        logger,
    )


def main():
    """Entry point — ask for the Gemini API key then launch the GUI."""
    # Check env var / saved key first; only show the dialog if neither is set.
    api_key = _get_api_key()
    if not api_key:
        try:
            api_key = ask_for_api_key()
        except KeyboardInterrupt:
            print("Cancelled.")
            return
        except RuntimeError as exc:
            print(f"[ERROR] {exc}")
            return

    try:
        _configure_genai(api_key)
    except EnvironmentError as exc:
        print(f"[ERROR] {exc}")
        return

    try:
        config = collect_user_config()
    except KeyboardInterrupt:
        print("Evaluation cancelled before grading started.")
        return
    except RuntimeError as exc:
        print(f"Could not start the setup window: {exc}")
        return

    logger = config["logger"]
    session_dir = config["session_dir"]
    audit_log_path = session_dir / "evaluation_audit.jsonl"

    try:
        assignment_materials = config.get("assignment_materials") or load_or_generate_assignment_materials(config)
    except Exception as exc:
        logger.exception("Could not generate assignment materials.")
        print(f"Failed before grading started: {exc}")
        return

    existing_records, processed_keys = load_existing_results(config["results_excel"], logger)
    if processed_keys:
        if ask_reevaluate_submissions(len(processed_keys)):
            processed_keys = set()
            logger.info("User chose to reevaluate all previously processed submissions.")
        else:
            logger.info("Skipping %s previously processed submission(s).", len(processed_keys))

    submission_pdfs = discover_submission_pdfs(
        config["submissions_root"],
        config["assignment_pdf"],
        config["output_root"],
    )

    logger.info("Discovered %s submission PDF(s)", len(submission_pdfs))
    print(f"Found {len(submission_pdfs)} submission PDF(s) to consider.")

    records = list(existing_records)
    evaluated_count, skipped_count, failed_count = evaluate_submissions_with_progress(
        submission_pdfs,
        config,
        assignment_materials,
        logger,
        records,
        processed_keys,
        audit_log_path,
    )

    summary = {
        "evaluated": evaluated_count,
        "skipped": skipped_count,
        "failed": failed_count,
        "results_excel": str(config["results_excel"]),
        "audit_log": str(audit_log_path),
        "session_dir": str(session_dir),
    }
    write_text_file(session_dir / "summary.json", json.dumps(summary, indent=2, ensure_ascii=False))

    print("\n" + "=" * 70)
    print("EVALUATION COMPLETE")
    print(f"Evaluated : {evaluated_count}")
    print(f"Skipped   : {skipped_count}")
    print(f"Failed    : {failed_count}")
    print(f"Results   : {config['results_excel']}")
    print(f"Audit log : {audit_log_path}")
    print(f"Session   : {session_dir}")
    print("=" * 70)


if __name__ == "__main__":
    main()
