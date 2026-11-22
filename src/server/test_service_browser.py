"""Built service management UI against PostgreSQL and a real HTTP fixture."""
import json
import os
import pytest
from .synthetic_provider import run_synthetic_provider, BATCH_PATH, UPLOAD_PATH
from .test_workspace_browser import workspace, _login, _screenshot, _select

pytestmark = pytest.mark.skipif(not os.environ.get("DATACENTER_TEST_POSTGRES_URL"), reason="Requires explicit isolated local PostgreSQL")


@pytest.fixture
def service_browser(monkeypatch, request):
    name, secret = "DATACENTER_SYNTHETIC_BROWSER_KEY", "SYNTHETIC browser service credential"
    monkeypatch.setenv(name, secret)
    with run_synthetic_provider(credential=secret) as provider:
        monkeypatch.setenv("DATACENTER_SYNTHETIC_SERVICE_ENDPOINTS", json.dumps({"synthetic": {"port": provider.port, "credential_env": name}}))
        yield request.getfixturevalue("workspace"), provider


def test_react_services_protocol_tests_previews_activation_recovery_and_safe_stop(service_browser, monkeypatch):
    from playwright.sync_api import expect
    f, provider = service_browser
    _login(f, "services", f.administrator, "synthetic admin password")
    page = f.page
    expect(page.get_by_role("heading", name="服务配置", exact=True)).to_be_visible()
    expect(page.get_by_text("企业数据准入未配置。", exact=True)).to_be_visible()
    selections = page.get_by_role("region", name="按用途选用服务", exact=True)
    expect(selections.get_by_text("选用只适用于系统确认过的合成输入。", exact=False)).to_be_visible()
    expect(_select(selections, "用途").locator("option")).to_have_count(5)
    _screenshot(f, "service-empty")
    for index, kind in enumerate(("generation", "embedding", "parsing")):
        label = "SYNTHETIC browser " + kind
        if index:
            page.get_by_text("新建服务配置", exact=True).click()
        page.get_by_label("服务类型").select_option(kind)
        page.get_by_label("配置名称", exact=True).fill(label)
        page.get_by_role("button", name="创建配置", exact=True).click()
        expect(page.get_by_role("heading", name=label, exact=True)).to_be_visible()
        page.get_by_role("button", name="保存修订", exact=True).click()
        expect(page.get_by_label("当前查看修订")).to_be_visible()
        if kind == "parsing":
            provider.mode = "pending"
        page.get_by_role("button", name="运行合成自检", exact=True).click()
        receipt = page.get_by_role("region", name="自检回执", exact=True)
        expect(receipt).to_be_visible()
        if kind == "parsing":
            expect(receipt.get_by_role("heading", name="自检回执 · 等待远端结果")).to_be_visible()
            page.get_by_label("核对理由", exact=True).fill("SYNTHETIC existing remote batch only")
            page.get_by_role("button", name="核对远端结果", exact=True).click()
        expect(receipt.get_by_role("heading", name="自检回执 · 测试通过")).to_be_visible()
        page.get_by_label("变更理由", exact=True).fill("SYNTHETIC local self-test configuration")
        page.get_by_role("button", name="预检启用", exact=True).click()
        expect(page.get_by_role("button", name="确认启用", exact=True)).to_be_visible()
        rows = page.request.get(f.address + "admin/service-profiles").json()["items"]
        profile = next(x for x in rows if x["label"] == label)
        assert profile["enabled"] is False
        page.get_by_role("button", name="确认启用", exact=True).click()
        expect(page.get_by_text("本地已启用修订 1", exact=True)).to_be_visible()
        current = page.request.get(f.address + "admin/service-profiles/" + profile["id"]).json()
        assert current["enabled"] and current["enterprise_admission_state"] == "not_configured"
        assert current["business_dispatch_enabled"] is False
        assert current["business_dispatch_scope"] == "exact_attested_synthetic_only"
        _screenshot(f, "service-" + kind + "-enabled")
        if kind in {"generation", "parsing"}:
            purpose = "query.answer" if kind == "generation" else "document.parse"
            listing_url = f.address + "admin/service-selections"

            def read_selection():
                return next(row for row in page.request.get(listing_url).json()["items"] if row["purpose"] == purpose)

            _select(selections, "用途").select_option(purpose)
            assert read_selection()["enabled"] is False
            _select(selections, "目标配置修订").select_option(current["active_revision_id"])
            selections.get_by_label("选用变更理由", exact=True).fill("SYNTHETIC purpose choice")
            selections.get_by_role("button", name="预检用途变更", exact=True).click()
            preview = selections.get_by_role("button", name="确认选用", exact=True)
            expect(preview).to_be_visible()
            assert read_selection()["enabled"] is False
            _screenshot(f, "service-use-" + kind + "-preview")
            if kind == "generation":
                apply_url = listing_url + "/" + purpose + "/select"
                receipts = []

                def lose_first_reply(route):
                    request = route.request
                    receipts.append((request.headers["idempotency-key"], request.headers["if-match"], request.post_data))
                    if len(receipts) == 1:
                        assert route.fetch().status == 200
                        route.abort()
                    else:
                        route.continue_()

                page.route(apply_url, lose_first_reply)
                preview.click()
                recover = page.get_by_role("button", name="恢复原操作回执", exact=True)
                expect(recover).to_be_visible()
                assert read_selection()["enabled"] is True
                page.reload(wait_until="networkidle")
                recover.click()
                expect(recover).to_have_count(0)
                assert len(receipts) == 1, "GET receipt recovery must not repeat the original selection POST"
                page.unroute(apply_url)
                _select(selections, "用途").select_option(purpose)
                # The service configuration detail selection is local to a page load.
                page.get_by_role("button", name=label, exact=False).click()
            else:
                preview.click()
            expect(selections.get_by_text("合成用途已选用", exact=True)).to_be_visible()
            selected = read_selection()
            assert selected["revision_id"] == current["active_revision_id"]
            expect(selections.get_by_text(selected["test_run_id"], exact=True)).to_be_visible()
            assert selected["input_admission"] == "exact_private_synthetic_attestation_required"
            assert selected["enterprise_admission_state"] == "not_configured"
            # A real selected, current self-test is shown as admission, not live health.
            request_count = len(provider.requests)
            with page.expect_popup() as opened:
                page.evaluate("window.open('/workspace/operations', '_blank')")
            dashboard = opened.value
            try:
                tile = dashboard.locator(".admission-tile").filter(has=dashboard.get_by_role("heading", name="查询答复" if kind == "generation" else "文档解析", exact=True))
                expect(tile.get_by_text("准入有效", exact=True)).to_be_visible()
                expect(tile).to_contain_text(label)
                assert len(provider.requests) == request_count, "Dashboard must not dispatch provider calls"
                dashboard.screenshot(path=str(f.out / ("operations-" + kind + "-admitted.png")))
            finally:
                dashboard.close()
            if kind == "parsing":
                # Revocation of the purpose must work even while the provider binding is unavailable.
                monkeypatch.delenv("DATACENTER_SYNTHETIC_BROWSER_KEY")
            _select(selections, "选用变更").select_option("disable")
            selections.get_by_label("选用变更理由", exact=True).fill("SYNTHETIC stop purpose")
            selections.get_by_role("button", name="预检用途变更", exact=True).click()
            expect(selections.get_by_text("重新选用后，原绑定仍然失效。", exact=False)).to_be_visible()
            assert read_selection()["enabled"] is True
            selections.get_by_role("button", name="确认停用用途", exact=True).click()
            expect(selections.locator("dd").get_by_text("未选用", exact=True)).to_be_visible()
            stopped = read_selection()
            assert stopped["enabled"] is False and stopped["epoch"] == selected["epoch"] + 1
            f.checks.append({"purpose": purpose, "preview_did_not_select": True, "attestation_required": True,
                             "unknown_reply_recovered": kind == "generation", "disabled": True})
        page.get_by_label("变更操作").select_option("disable")
        page.get_by_label("变更理由", exact=True).fill("SYNTHETIC safety stop")
        page.get_by_role("button", name="预检停用", exact=True).click()
        page.get_by_role("button", name="确认停用", exact=True).click()
        expect(page.locator("dd").get_by_text("未启用", exact=True)).to_be_visible()
        assert page.request.get(f.address + "admin/service-profiles/" + profile["id"]).json()["enabled"] is False
        f.checks.append({"kind": kind, "profile": profile["id"], "protocol_self_test": "passed", "preview_did_not_activate": True,
                         "synthetic_activation_only": True, "disabled": True})
    assert len([x for x in provider.requests if x.method == "POST" and x.target == BATCH_PATH]) == 1
    assert len([x for x in provider.requests if x.method == "PUT" and x.target.startswith(UPLOAD_PATH)]) == 1
    _screenshot(f, "service-safe-stop")

    page.goto(f.address + "workspace/operations")
    expect(page.locator(".admission-tile")).to_have_count(3)
    expect(page.locator(".admission-tile").get_by_text("准入有效", exact=True)).to_have_count(0)
    expect(page.get_by_text("当前仅获准合成试点。", exact=False)).to_be_visible()
    f.checks.append("Actual current purpose admission appears in dashboard; disabled purposes never appear admitted; reads dispatch no provider call")
