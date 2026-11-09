"""Tests never read or write the real list of AI models skipped for a week (data/jobs.db)."""
import pytest


@pytest.fixture(autouse=True)
def _no_gone_models(monkeypatch):
    from jobfinder import llm
    gone = {}
    monkeypatch.setattr(llm, "_gone", lambda: dict(gone))
    monkeypatch.setattr(llm, "mark_gone", lambda step, why: gone.update({f"{step[2]}|{step[1]}": 1e12}))
    monkeypatch.setattr(llm, "note_quota", lambda *a, **k: None)  # never the real quota record
    from jobfinder import adapters
    monkeypatch.setattr(adapters, "save_jobspipe_credits", lambda meta: None)
