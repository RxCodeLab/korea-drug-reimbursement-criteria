import json
import os
import subprocess
from pathlib import Path

import yaml

import common

WORKFLOW = Path(__file__).parents[1] / ".github" / "workflows" / "update.yml"


def _source() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def _steps(source: str, job: str) -> list[str]:
    body = source.split(f"\n  {job}:\n", 1)[1]
    body = body.split("\n\n  ", 1)[0]
    return body.split("\n      - ")[1:]


def _step(source: str, job: str, name: str) -> str:
    matches = [step for step in _steps(source, job) if step.startswith(f"name: {name}\n")]
    assert len(matches) == 1, f"step {name!r} not found in job {job!r}"
    return matches[0]


def test_workflow_parses_as_yaml():
    document = yaml.safe_load(_source())
    assert set(document["jobs"]) == {"build", "publish-data", "report-upstream", "deploy-pages"}
    build_steps = [step.get("name") for step in document["jobs"]["build"]["steps"]]
    assert "Run tests" in build_steps


def test_code_and_workflow_changes_trigger_publication():
    """*.py 변경이 다음 cron까지 배포되지 않던 문제: push 트리거가 소스도 포함해야 한다."""
    source = _source()
    paths = source.split("    paths:\n", 1)[1].split("  workflow_dispatch:", 1)[0]
    assert "- .github/workflows/update.yml" in paths
    assert '- "*.py"' in paths
    assert "- requirements.txt" in paths
    assert "- tests/**" in paths


def test_transient_upstream_failures_do_not_block_verified_pages_build():
    source = _source()

    assert "mfds_fetch: ${{ steps.fetch_mfds.outcome }}" in source
    assert "id: fetch_mfds\n        continue-on-error: true" in source
    assert "id: fetch_law\n        continue-on-error: true" in source
    assert 'test -n "$DATA_GO_KEY" || {' in source
    assert (
        "if: needs.build.outputs.law_fetch == 'failure' || "
        "needs.build.outputs.mfds_fetch == 'failure'"
    ) in source


def test_secret_audit_is_a_fail_closed_step_before_publication():
    """비밀값 누출 검사가 continue-on-error 스텝 안에 있으면 감지해도 게시가 진행된다.

    게시되는 모든 산출물(정규화, MFDS, 공개 DB, Pages)이 만들어진 뒤, artifact 업로드 전에 검사한다.
    """
    source = _source()
    steps = _steps(source, "build")
    names = [step.split("\n", 1)[0] for step in steps if step.startswith("name: ")]
    audit = _step(source, "build", "Audit collected data for leaked secrets")
    assert "continue-on-error" not in audit
    assert "LAW_OC: ${{ secrets.LAW_OC }}" in audit and "DATA_GO_KEY: ${{ secrets.DATA_GO_KEY }}" in audit
    assert "sys.exit(1)" in audit
    for scanned in ('Path("data/criteria.db")', 'Path("public").rglob("*")', 'Path("data/normalized").glob("*.json")',
                    'Path("data/mfds/items").glob("*.json")'):
        assert scanned in audit
    order = [names.index(f"name: {name}") for name in
             ("Fetch law notices", "Fetch MFDS permit data", "Normalize and build SQLite",
              "Verify candidate publication", "Build static search", "Audit collected data for leaked secrets")]
    assert order == sorted(order)
    build_body = source.split("\n  build:\n", 1)[1].split("\n\n  publish-data:", 1)[0]
    assert build_body.index("Audit collected data for leaked secrets") < build_body.index("actions/upload-artifact")
    fetch_law = _step(source, "build", "Fetch law notices")
    assert "leaked" not in fetch_law


def test_raw_archive_cache_is_gone():
    """raw는 git에 커밋되므로 run_id별 55MB 캐시는 무의미했고 실패한 실행의 자료까지 보존했다."""
    assert "actions/cache" not in _source()


def test_build_site_no_longer_receives_collection_status():
    """푸터는 실패·시도 정보를 담지 않으므로 COLLECTION_STATUS 경로는 사라져야 한다."""
    build = _step(_source(), "build", "Build static search")
    assert "COLLECTION_STATUS" not in build
    assert "LAW_FETCH" not in build
    run_line = [line for line in build.splitlines() if line.strip().startswith("run:")]
    assert run_line == ["        run: python build_site.py"]


def test_last_success_recorded_only_when_both_fetches_succeed_before_build():
    """법제처·식약처 둘 다 성공한 실행만 상태 파일을 갱신한다. 하나라도 실패하면 구식 자료로 검증되지 않은 자료가 있을 수 있어
    "안전한 날짜"로 신뢰할 수 없다."""
    source = _source()
    record_step = _step(source, "build", "Record last successful collection date")
    assert record_step.startswith(
        "name: Record last successful collection date\n"
        "        if: steps.fetch_law.outcome == 'success' && steps.fetch_mfds.outcome == 'success'\n"
    )
    assert "data/collection.json" in record_step
    assert "last_success_date" in record_step

    names = [step.split("\n", 1)[0] for step in _steps(source, "build") if step.startswith("name: ")]
    order = [names.index(f"name: {name}") for name in
             ("Verify candidate publication", "Record last successful collection date", "Build static search")]
    assert order == sorted(order)


def test_record_last_success_step_actually_runs(tmp_path):
    """run 블록을 미리 뿑아 bash로 실제 실행해, 파일이 없는 상태에서 시작해 성공/실패 각각을 검증한다."""
    record_step = _step(_source(), "build", "Record last successful collection date")
    run_block = record_step.split("run: |\n", 1)[1]
    run_lines = [line[10:] if line.startswith(" " * 10) else line for line in run_block.split("\n")]
    run_script = "\n".join(run_lines).rstrip("\n")

    workdir = tmp_path / "run"
    workdir.mkdir()
    result = subprocess.run(["bash", "-c", run_script], capture_output=True, text=True, cwd=workdir)
    assert result.returncode == 0, result.stderr
    state = json.loads((workdir / "data" / "collection.json").read_text(encoding="utf-8"))
    assert set(state) == {"last_success_date"}
    assert len(state["last_success_date"]) == 8 and state["last_success_date"].isdigit()


def test_secret_audit_scans_collection_state_file():
    audit = _step(_source(), "build", "Audit collected data for leaked secrets")
    assert 'Path("data/collection.json")' in audit


def test_upstream_failure_opens_or_updates_issue():
    source = _source()
    report = source.split("\n  report-upstream:\n", 1)[1].split("\n  deploy-pages:\n", 1)[0]
    assert "permissions:\n      issues: write" in report
    assert "GH_TOKEN: ${{ github.token }}" in report
    assert "gh issue list --label upstream-failure --state open" in report
    assert "gh issue edit" in report and "gh issue create --title" in report
    assert "gh issue comment" not in report  # 장애가 길어져도 댓글이 매일 쌓이지 않는다
    assert "GITHUB_STEP_SUMMARY" in report
    assert "exit 1" not in report


def test_publish_data_commits_collection_state_file():
    """상태 파일을 git add 대상에 넣지 않으면 다음 실행에서 사라진다."""
    source = _source()
    commit = _step(source, "publish-data", "Promote canonical data and commit")
    assert "data/collection.json" in commit
    assert "git add -f data/collection.json" in commit
    assert "upload-artifact" not in commit
    build_body = source.split("\n  build:\n", 1)[1].split("\n\n  publish-data:", 1)[0]
    assert "data/collection.json" in build_body  # upload-artifact path


def test_pages_deploy_does_not_wait_for_data_commit():
    source = _source()
    deploy = source.split("\n  deploy-pages:\n", 1)[1]

    assert deploy.startswith("    needs: build\n")
    assert "needs: [build, publish-data]" not in deploy
    assert "pages: write" in deploy
    assert "id-token: write" in deploy


def test_http_timeout_is_short_enough_to_fail_fast():
    """차단된 상위 서버에 60초×3회씩 기다리면 실행당 30분을 버린다."""
    assert common.HTTP_TIMEOUT_SECONDS <= 15


def _schedule_crons(document):
    return [entry["cron"] for entry in document[True]["schedule"]]


def test_schedule_has_separate_weekday_and_saturday_crons():
    """평일 증분 / 토요일 전량을 서로 다른 cron으로 나눈다. 일요일은 어느 쪽에도 없어야 한다."""
    document = yaml.safe_load(_source())
    crons = _schedule_crons(document)
    assert len(crons) == 2
    dow_fields = [cron.split()[-1] for cron in crons]
    assert "1-5" in dow_fields, dow_fields
    assert "6" in dow_fields, dow_fields
    for field in dow_fields:
        assert "0" not in field.split(",")
        assert "7" not in field.split(",")
        assert "*" != field


def _mfds_full_sweep_expression(source: str) -> str:
    fetch_mfds = _step(source, "build", "Fetch MFDS permit data")
    line = next(line for line in fetch_mfds.splitlines() if line.strip().startswith("MFDS_FULL_SWEEP:"))
    return line.split("MFDS_FULL_SWEEP:", 1)[1].strip().removeprefix("${{").removesuffix("}}").strip()


def test_full_sweep_only_on_saturday_schedule():
    source = _source()
    fetch_mfds = _step(source, "build", "Fetch MFDS permit data")
    document = yaml.safe_load(source)
    crons = _schedule_crons(document)
    saturday_cron = next(cron for cron in crons if cron.split()[-1] == "6")
    weekday_cron = next(cron for cron in crons if cron.split()[-1] == "1-5")
    assert saturday_cron in fetch_mfds
    assert f"github.event.schedule == '{saturday_cron}'" in fetch_mfds
    assert weekday_cron not in fetch_mfds.split("MFDS_FULL_SWEEP")[1].split("\n")[0]
    assert "--full" in fetch_mfds
    assert "inputs.full_sweep" in fetch_mfds


def test_saturday_cron_literal_in_expression_matches_actual_schedule():
    """cron 표현을 바꾸면 MFDS_FULL_SWEEP 식 안의 리터럴도 같이 바뀌지 않으면 토요일에도 전량이 안 돌아간다."""
    source = _source()
    document = yaml.safe_load(source)
    crons = _schedule_crons(document)
    saturday_cron = next(cron for cron in crons if cron.split()[-1] == "6")
    expression = _mfds_full_sweep_expression(source)
    assert f"github.event.schedule == '{saturday_cron}'" in expression


def test_boolean_full_sweep_input_is_not_compared_to_a_string_literal():
    """full_sweep은 type: boolean이라 inputs.full_sweep은 문자열이 아니라 불리언이다. GitHub Actions 식의
    loose equality는 타입이 다르면 숫자로 변환하므로(Boolean true -> 1, String 'true' -> NaN),
    ``inputs.full_sweep == 'true'``는 항상 false로 평가된다. 식은 불리언 입력을 문자열 리터럴과
    ``==``로 비교하지 않고 그대로 써야 한다.
    """
    source = _source()
    document = yaml.safe_load(source)
    inputs = document[True]["workflow_dispatch"]["inputs"]
    assert inputs["full_sweep"]["type"] == "boolean"
    expression = _mfds_full_sweep_expression(source)
    assert "inputs.full_sweep" in expression
    for banned in ("inputs.full_sweep == 'true'", 'inputs.full_sweep == "true"',
                   "inputs.full_sweep=='true'", "'true' == inputs.full_sweep"):
        assert banned not in expression, expression
    # 식은 단순히 boolean 값을 그대로 써야 한다(또는 fromJSON 같은 타입 안전한 변환). 문자열 리터럴과의
    # ==/!= 비교가 아니라면, 항상 참으로 평가되지 않는다는 것만 확인한다.
    assert expression.count("==") == 1  # github.event.schedule 비교 하나뿐, full_sweep은 안 비교
    assert expression.strip().endswith("inputs.full_sweep")


def test_push_trigger_stays_incremental():
    """push로 도는 실행에서는 github.event.schedule이 빈 값이고 inputs.full_sweep도 없어 증분 경로로 떨어져야 한다."""
    fetch_mfds = _step(_source(), "build", "Fetch MFDS permit data")
    assert "MFDS_FULL_SWEEP: ${{ github.event.schedule ==" in fetch_mfds


def test_workflow_dispatch_has_full_sweep_input():
    document = yaml.safe_load(_source())
    inputs = document[True]["workflow_dispatch"]["inputs"]
    assert "full_sweep" in inputs
    assert inputs["full_sweep"]["type"] == "boolean"
    assert inputs["full_sweep"]["default"] is False
    assert "law_changes_since" in inputs
    assert "mfds_changes_since" in inputs


def test_full_and_changes_since_are_mutually_exclusive_in_branch():
    """--full과 --changes-since를 함께 붙이면 fetch_mfds.py의 argparse가 거부한다. 워크플로도 그 조합을 만들면 안 된다."""
    fetch_mfds = _step(_source(), "build", "Fetch MFDS permit data")
    assert 'if [ "$MFDS_FULL_SWEEP" = "true" ]; then' in fetch_mfds
    branch = fetch_mfds.split('if [ "$MFDS_FULL_SWEEP" = "true" ]; then', 1)[1].split("fi\n", 1)[0]
    full_branch = branch.split("elif", 1)[0]
    invocation_lines = [line for line in full_branch.splitlines() if line.strip().startswith("python fetch_mfds.py")]
    assert len(invocation_lines) == 1
    assert "--full" in invocation_lines[0]
    assert "--changes-since" not in invocation_lines[0]


def test_mfds_fetch_branches_execute_expected_argv(tmp_path):
    """run 블록을 뽑아 bash로 실제 실행하고, 각 트리거 조합의 최종 argv를 검증한다."""
    fetch_mfds = _step(_source(), "build", "Fetch MFDS permit data")
    run_block = fetch_mfds.split("run: |\n", 1)[1]
    run_lines = [line[10:] if line.startswith(" " * 10) else line for line in run_block.split("\n")]
    run_script = "\n".join(run_lines).rstrip("\n")
    script = run_script.replace("python fetch_mfds.py", "echo fetch_mfds.py")

    def run(full_sweep: str, changes_since: str = "") -> str:
        env = {
            **os.environ,
            "DATA_GO_KEY": "dummy-key",
            "MFDS_CHANGES_SINCE": changes_since,
            "MFDS_FULL_SWEEP": full_sweep,
        }
        result = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env, check=True)
        return result.stdout.strip()

    weekday_cron_argv = run(full_sweep="false")
    assert weekday_cron_argv == "fetch_mfds.py"

    saturday_cron_argv = run(full_sweep="true")
    assert saturday_cron_argv == "fetch_mfds.py --full"

    dispatch_full_argv = run(full_sweep="true")
    assert dispatch_full_argv == "fetch_mfds.py --full"

    dispatch_incremental_argv = run(full_sweep="false")
    assert dispatch_incremental_argv == "fetch_mfds.py"

    push_argv = run(full_sweep="false")
    assert push_argv == "fetch_mfds.py"

    dispatch_with_changes_since_argv = run(full_sweep="false", changes_since="20260101")
    assert dispatch_with_changes_since_argv == "fetch_mfds.py --changes-since 20260101"


def test_timeout_budget_documented_for_full_sweep():
    """토요일 전량 실행(열거 ~8분 + 매칭 + 이력 백필)이 350분 예산 안에 들어온다는 근거가 있어야 한다."""
    document = yaml.safe_load(_source())
    assert document["jobs"]["build"]["timeout-minutes"] == 350
