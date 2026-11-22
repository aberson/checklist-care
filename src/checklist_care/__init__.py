"""checklist-care: turn life-advice sources into printable room checklists."""

from typing import Final

__version__ = "0.1.0"

# Set at build Step 1 from `git remote get-url origin`
# (https://github.com/aberson/checklist-care.git), normalized to the web form.
PROJECT_URL: Final = "https://github.com/aberson/checklist-care"

# Sent to Wikipedia and yt-dlp. Never put an email address here.
USER_AGENT: Final = f"checklist-care/{__version__} (+{PROJECT_URL})"

__all__ = ["PROJECT_URL", "USER_AGENT", "__version__"]
