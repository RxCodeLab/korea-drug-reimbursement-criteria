from pathlib import Path


WORKFLOW = Path(__file__).parents[1] / ".github" / "workflows" / "update.yml"


def test_workflow_changes_run_on_main():
    source = WORKFLOW.read_text(encoding="utf-8")

    assert "push:\n    branches:\n      - main" in source
    assert "paths:\n      - .github/workflows/update.yml" in source


def test_transient_upstream_failures_do_not_block_verified_pages_build():
    source = WORKFLOW.read_text(encoding="utf-8")

    assert "mfds_fetch: ${{ steps.fetch_mfds.outcome }}" in source
    assert "id: fetch_mfds\n        continue-on-error: true" in source
    assert 'test -n "$DATA_GO_KEY" || {' in source
    assert (
        "if: needs.build.outputs.law_fetch == 'failure' || "
        "needs.build.outputs.mfds_fetch == 'failure'"
    ) in source
    assert "::warning::상위 데이터 수집이 실패해" in source
    report = source.split("\n  report-upstream:\n", 1)[1].split("\n  deploy-pages:\n", 1)[0]
    assert "exit 1" not in report


def test_pages_deploy_does_not_wait_for_data_commit():
    source = WORKFLOW.read_text(encoding="utf-8")
    deploy = source.split("\n  deploy-pages:\n", 1)[1]

    assert deploy.startswith("    needs: build\n")
    assert "needs: [build, publish-data]" not in deploy
    assert "pages: write" in deploy
    assert "id-token: write" in deploy
