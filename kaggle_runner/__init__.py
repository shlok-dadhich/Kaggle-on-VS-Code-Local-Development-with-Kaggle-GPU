"""kaggle_runner package."""

__version__ = "1.0.0"

from .sync import main
from .run import main as run_main
from .pull import main as pull_main

__all__ = ["main", "run_main", "pull_main", "__version__"]