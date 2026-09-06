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


def test_build_site_receives_collection_status():
    build = _step(_source(), "build", "Build static search")
    assert "LAW_FETCH: ${{ steps.fetch_law.outcome }}" in build
    assert "MFDS_FETCH: ${{ steps.fetch_mfds.outcome }}" in build
    assert 'COLLECTION_STATUS="$(printf \'{"run_at":"%s","law":"%s","mfds":"%s"}\'' in build
    assert "python build_site.py" in build


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
