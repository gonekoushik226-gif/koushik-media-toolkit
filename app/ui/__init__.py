"""PySide6 user interface. Widgets never do heavy work themselves: they collect
options, then hand a function to the JobController, which runs it on a worker
thread."""
