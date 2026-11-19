"""Compare QA candidate models on one labelled question set (stage 4-5).

Thin wrapper: the logic lives in `repopilot.qa.evaluation` and `repopilot.services.qa_eval_cli`.
Run `uv run python scripts/eval/run_qa_eval.py --help` for the dataset format and options.
"""

import sys

from repopilot.services.qa_eval_cli import main

if __name__ == "__main__":
    sys.exit(main())
