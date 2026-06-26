from __future__ import annotations


def test_full_smoke_pipeline(cfg, pipeline_summary):
    assert pipeline_summary["pipeline_status"] == "PASS"
    memo_path = cfg.artifacts_dir / "reports" / "executive_decision_memo.md"
    card_path = cfg.artifacts_dir / "reports" / "model_and_optimization_card.md"
    assert memo_path.exists()
    assert card_path.exists()
    assert (cfg.artifacts_dir / "stochastic_rcce.json").exists()
    memo = memo_path.read_text(encoding="utf-8")
    model_card = card_path.read_text(encoding="utf-8")
    assert "Recommended solver termination" in memo
    assert "Recommended max linear-constraint violation" in memo
    assert "## Solver release evidence" in model_card
    assert pipeline_summary["solver_diagnostics"]["recommended"]["termination_class"] in {
        "OPTIMAL",
        "LIMIT_REACHED",
    }
