"""Velocity tracking and backlog projection for the GreenEarth GitHub Project.

Pure-logic modules (``points``, ``velocity``, ``burndown``) operate on lists of
``ProjectItem`` and are unit-tested without network access. ``github_project``
handles all I/O (fetching project data via the ``gh`` CLI).
"""
