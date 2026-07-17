"""Tests for the evaluation harness.

This file is not ceremony. It makes these tests ``eval.tests.*`` rather than
top-level modules, which does two things: pytest walks up to the repository root
looking for the first directory without an ``__init__.py`` and puts *that* on
``sys.path``, so ``from eval.metrics import ...`` resolves without a conftest
hack; and the package is named ``eval.tests``, not ``tests``, so it cannot collide
with the real top-level ``tests`` package that ``pyannote.pipeline`` ships in
site-packages — the collision that ``backend/tests`` has to import around. See
``backend/tests/test_speaker_mapping.py`` for what that looks like when it bites.
"""
