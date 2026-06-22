from __future__ import annotations

from droid_deploy.infra.cli import pipeline_control


def _job(job_id: str, committed: int, *, expected: int = 0, status: str = "running"):
    return {
        "job_id": job_id,
        "committed_rows": committed,
        "expected_rows": expected,
        "status": status,
    }


def test_throughput_report_baseline_has_unknown_rates() -> None:
    current = [_job("job-1", 100), _job("job-2", 50)]

    report = pipeline_control._build_throughput_report(
        current=current,
        prev_by_job=None,
        dt_seconds=60.0,
    )

    assert report["committed_rows"] == 150
    assert report["delta_rows"] is None
    assert report["rows_per_s"] is None
    assert report["stalled"] is False
    assert report["nonterminal_jobs"] == 2
    assert all(j["rows_per_s"] is None for j in report["jobs"])


def test_throughput_report_computes_aggregate_and_per_job_rate() -> None:
    current = [_job("job-1", 700, expected=1000), _job("job-2", 300)]
    prev = {"job-1": 100, "job-2": 300}

    report = pipeline_control._build_throughput_report(
        current=current,
        prev_by_job=prev,
        dt_seconds=60.0,
    )

    assert report["committed_rows"] == 1000
    assert report["delta_rows"] == 600
    assert report["rows_per_s"] == 10.0
    assert report["stalled"] is False

    job1 = next(j for j in report["jobs"] if j["job_id"] == "job-1")
    assert job1["delta_rows"] == 600
    assert job1["rows_per_s"] == 10.0
    assert job1["remaining_rows"] == 300
    # 300 remaining / 10 rows-per-s = 30s ETA
    assert job1["eta_seconds"] == 30

    job2 = next(j for j in report["jobs"] if j["job_id"] == "job-2")
    assert job2["delta_rows"] == 0
    assert job2["eta_seconds"] is None


def test_throughput_report_flags_stall_when_running_but_no_movement() -> None:
    current = [_job("job-1", 500, status="running")]
    prev = {"job-1": 500}

    report = pipeline_control._build_throughput_report(
        current=current,
        prev_by_job=prev,
        dt_seconds=60.0,
    )

    assert report["delta_rows"] == 0
    assert report["stalled"] is True
    assert report["all_terminal"] is False


def test_throughput_report_no_stall_when_all_terminal() -> None:
    current = [_job("job-1", 500, status="success")]
    prev = {"job-1": 500}

    report = pipeline_control._build_throughput_report(
        current=current,
        prev_by_job=prev,
        dt_seconds=60.0,
    )

    assert report["stalled"] is False
    assert report["all_terminal"] is True
    assert report["nonterminal_jobs"] == 0
