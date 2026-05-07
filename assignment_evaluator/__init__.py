"""
assignment_evaluator
====================
AI-powered assignment grading tool using Google Gemini.

Quick start
-----------
1. Set your API key::

       export GEMINI_API_KEY="your_key_here"          # Linux / macOS
       set  GEMINI_API_KEY=your_key_here              # Windows CMD
       $env:GEMINI_API_KEY = "your_key_here"          # Windows PowerShell

2. Launch the GUI::

       assignment-evaluator
       # or
       python -m assignment_evaluator

Public API
----------
The main programmatic entry-point is :func:`assignment_evaluator.app.main`.
"""

from importlib.metadata import version, PackageNotFoundError

try:
    __version__ = version("assignment-evaluator")
except PackageNotFoundError:
    __version__ = "0.0.0"

__all__ = ["__version__"]
