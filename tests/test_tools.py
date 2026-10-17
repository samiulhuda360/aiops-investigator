from aiops.tools import READ_ONLY_TOOLS, TOOL_SPECS, Environment, dispatch


def test_only_read_only_tools_are_exposed() -> None:
    assert {t["name"] for t in TOOL_SPECS} == set(READ_ONLY_TOOLS)
    verbs = ("restart", "delete", "exec", "write", "apply", "run", "kill", "scale", "set", "update", "rollback")
    assert all(name.split("_")[0] in ("check", "query", "list", "get", "read") for name in READ_ONLY_TOOLS)
    assert not any(name.split("_")[0] in verbs for name in READ_ONLY_TOOLS)


def test_write_tool_is_refused_not_run(env12: Environment) -> None:
    for name in ("restart_service", "run_command", "kubectl_apply"):
        r = dispatch(env12, name, {"service": "api"})
        assert r.refused and not r.ok and "refused" in r.output


def test_query_logs_groups_and_filters(env12: Environment) -> None:
    out = dispatch(env12, "query_logs", {"service": "postgres", "level": "WARN", "since_minutes": 60}).output
    assert "distinct patterns" in out
    assert "LOG:  duration:" in out
    assert out.splitlines()[1].startswith("[x")  # most frequent pattern first
    none = dispatch(env12, "query_logs", {"service": "api", "pattern": "no-such-thing-xyz"}).output
    assert "no matching lines" in none


def test_bad_arguments_return_errors(env12: Environment) -> None:
    r = dispatch(env12, "query_logs", {"service": "mysql"})
    assert not r.ok and "unknown service" in r.output
    assert not dispatch(env12, "get_change", {"change_id": "chg-0000"}).ok
    assert dispatch(env12, "query_logs", {"service": "api", "pattern": "([unclosed"}).ok  # searched as plain text


def test_metrics_changes_cron_runbook(env12: Environment) -> None:
    m = dispatch(env12, "query_metric", {"name": "node_cpu_iowait_ratio", "service": "postgres"}).output
    assert "now=" in m and "baseline" in m
    assert "no metric" in dispatch(env12, "query_metric", {"name": "nope", "service": "api"}).output
    ch = dispatch(env12, "list_changes", {}).output
    assert "pg-backup" in ch
    cid = ch.splitlines()[1].split()[0]
    assert dispatch(env12, "get_change", {"change_id": cid}).output.startswith(cid)
    assert "RUNNING" in dispatch(env12, "list_cron_runs", {}).output
    assert "Runbook: HighLatency" in dispatch(env12, "read_runbook", {"name": "HighLatency"}).output
    assert "Available" in dispatch(env12, "read_runbook", {"name": "../../etc/passwd"}).output


def test_unknown_arguments_are_dropped(env12: Environment) -> None:
    r = dispatch(env12, "check_health", {"service": "api", "shell": "rm -rf /"})
    assert r.ok and r.output.startswith("api:")
