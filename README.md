# Assignment Evaluator

Desktop tool to grade PDF submissions with Google Gemini.

It generates a rubric from the assignment PDF, evaluates student PDFs, and exports:
- graded PDFs with feedback
- an Excel summary

## Install

### Option 1: Windows standalone executable
Download `AssignmentEvaluator.exe` from Releases and run it.

### Option 2: From PyPI
```bash
pip install assignment-evaluator
```

### Option 3: From source
```bash
pip install -e .
```

## Requirements

- Google Gemini API key
- Poppler (required for PDF to image conversion)

Install Poppler:

- Windows: download from https://github.com/oschwartz10612/poppler-windows
- macOS:
```bash
brew install poppler
```
- Ubuntu/Debian:
```bash
sudo apt-get install -y poppler-utils
```

If Poppler is not on PATH, set:

```powershell
$env:POPPLER_BIN_PATH = "C:\path\to\poppler\Library\bin"
```

## Gemini API key

On first launch, the app asks for your Gemini API key and can save it locally for next use.

Get a key:
1. Open https://aistudio.google.com/app/apikey
2. Sign in
3. Click Create API key
4. Paste key into the app dialog

Optional: you can still use environment variable `GEMINI_API_KEY`.

## Run

```bash
assignment-evaluator
```

Or:

```bash
python -m assignment_evaluator
```

## Basic flow

1. Select assignment PDF and generate rubric.
2. Review and approve rubric.
3. Select submissions and output folder.
4. Start evaluation.

## Output

- `*_graded.pdf` per student
- `Assignment_Evaluation_Results.xlsx`
- logs in `evaluation_logs/`

## Publish (maintainers)

```bash
python -m build
python -m twine check dist/*
python -m twine upload -u __token__ dist/*
```

## License

MIT. See LICENSE.
