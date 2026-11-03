"""Tests never read or write the real list of AI models skipped for a week (data/jobs.db)."""
import pytest


@pytest.fixture(autouse=True)
def _no_gone_models(monkeypatch):
    from jobfinder import llm
    gone = {}
    monkeypatch.setattr(llm, "_gone", lambda: dict(gone))
    monkeypatch.setattr(llm, "mark_gone", lambda step, why: gone.update({f"{step[2]}|{step[1]}": 1e12}))
