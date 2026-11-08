"""Optional terminal entrypoint; backend recovery never imports Textual."""
import sys


def run_workflow(title, steps, operation):
    """Import UI packages only after checking terminal and installation readiness."""
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print("The MAVEN TUI requires an interactive terminal. Use explicit noninteractive commands otherwise.", file=sys.stderr)
        return 2
    from .bootstrap import ui_available
    if not ui_available():
        print("The terminal interface needs MAVEN's [ui] dependencies. Activate the installed environment "
              "or install this checkout's [ui] extra there. Noninteractive commands remain available.", file=sys.stderr)
        return 2
    from .app import WorkflowApp
    return WorkflowApp(title, steps, operation).run() or 0
