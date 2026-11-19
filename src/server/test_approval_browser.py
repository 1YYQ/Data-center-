"""Chrome workflow for local-only synthetic approval registration and review."""
from datetime import timedelta
from pathlib import Path
import os
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from .governance import _approval_assertion, approval_is_current
from .governance_models import ApprovalRevocation, AuthorityBasis, GovernanceEvidence
from .models import AuthorizationGrant, BusinessApproval, utc_now
from .test_worker import local_stack, synthetic_pdf
from .test_services import fixture_db  # exposes the synthetic DB fixture to pytest


def test_approval_review_browser_access_registration_verification_and_revocation(local_stack):
    pw = pytest.importorskip("playwright.sync_api")
    chrome = Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe")
    if not chrome.exists():
        pytest.skip("Chrome executable unavailable")

    db, config, _, address, employee, administrator, kb, document, _, _, _ = local_stack
    now = utc_now()
    scope = {"knowledge_base_id": kb.id, "document_id": None, "level": document.access_level,
             "category": document.category, "need_class": document.need_class}
    # Controlled test-only trust roots and attestations; no public route creates them.
    manager_basis = AuthorityBasis(identity={"user_ref": administrator.id, "identity_label": administrator.display_name,
        "responsibility": "SYNTHETIC system administrator"}, purposes=["access.manage"], scope={"knowledge_base_id": kb.id},
        source_reference="synthetic://test-only-manager-duty", valid_from=now - timedelta(days=1), valid_until=now + timedelta(days=10))
    approver_basis = AuthorityBasis(identity={"identity_label": "SYNTHETIC external approver",
        "responsibility": "SYNTHETIC approval responsibility"}, purposes=["access_approval"], scope=scope,
        source_reference="synthetic://test-only-external-approval-duty", valid_from=now - timedelta(days=1), valid_until=now + timedelta(days=10))
    db.add_all([manager_basis, approver_basis]); db.flush()

    untrusted = BusinessApproval(applicant=employee.id, recorded_by=administrator.id,
        actual_approver_snapshot={**approver_basis.identity, "authority_basis_ref": approver_basis.id},
        actual_decision="approved", approved_at=now - timedelta(minutes=10), scope=scope, operations=["document.read"],
        valid_from=now - timedelta(minutes=10), valid_until=now + timedelta(days=2), status="pending",
        verification_state="unverified", evidence_locator="SYNTHETIC approval record with untrusted upload")
    positive = BusinessApproval(applicant=employee.id, recorded_by=administrator.id,
        actual_approver_snapshot={**approver_basis.identity, "authority_basis_ref": approver_basis.id},
        actual_decision="approved", approved_at=now - timedelta(minutes=5), scope=scope, operations=["document.read"],
        valid_from=now - timedelta(minutes=5), valid_until=now + timedelta(days=2), status="pending",
        verification_state="unverified", evidence_locator="SYNTHETIC controlled approval evidence")
    db.add_all([untrusted, positive]); db.flush()
    untrusted_proof = GovernanceEvidence(owner_kind="approval", owner_id=untrusted.id,
        source_reference={"source_description": "SYNTHETIC uploaded approval evidence"}, assertion=_approval_assertion(untrusted),
        attested=False, created_by=administrator.id)
    trusted_proof = GovernanceEvidence(owner_kind="approval", owner_id=positive.id,
        source_reference={"source_description": "SYNTHETIC controlled imported approval evidence"}, assertion=_approval_assertion(positive),
        attested=True, created_by=administrator.id)
    revoke_basis = AuthorityBasis(identity={"identity_label": "SYNTHETIC external revocation authority",
        "responsibility": "SYNTHETIC revocation responsibility"}, purposes=["access_revocation"],
        scope={"approval_id": positive.id, **scope}, source_reference="synthetic://test-only-revocation-duty",
        valid_from=now - timedelta(days=1), valid_until=now + timedelta(days=10))
    db.add_all([untrusted_proof, trusted_proof, revoke_basis]); db.flush()
    untrusted.proof_refs = [untrusted_proof.id]
    revoke_assertion = {"kind": "access_revocation",
        "actual_identity": {**revoke_basis.identity, "authority_basis_ref": revoke_basis.id},
        "decision": "revoked", "decided_at": (now - timedelta(minutes=1)).isoformat(),
        "scope": {"approval_id": positive.id, **scope}}
    revoke_proof = GovernanceEvidence(owner_kind="approval", owner_id=positive.id,
        source_reference={"source_description": "SYNTHETIC controlled external revocation record"},
        assertion=revoke_assertion, attested=True, created_by=administrator.id)
    db.add(revoke_proof); db.commit()
    grant_count_before = db.scalar(select(func.count()).select_from(AuthorizationGrant))

    screenshot_dir = Path(os.environ.get("DATACENTER_TEST_EVIDENCE_ROOT", "outputs/local-browser")) / "static-approval" / uuid4().hex
    screenshot_dir.mkdir(parents=True, exist_ok=True)
    errors = []
    with pw.sync_playwright() as p:
        browser = p.chromium.launch(headless=True, executable_path=str(chrome), args=["--disable-background-networking"])
        context = browser.new_context(viewport={"width": 1440, "height": 1000})
        page = context.new_page(); page.set_default_timeout(10000); page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(address + "access-review"); page.wait_for_load_state("networkidle")
        assert page.get_by_role("heading", name="授权审批核查").is_visible()
        page.locator("#login-form [name=username]").fill(administrator.username)
        page.locator("#login-form [name=password]").fill("synthetic admin password")
        page.get_by_role("button", name="登录").click(); page.wait_for_selector("#workspace:not(.hidden)")

        # Recording an approval about oneself is permitted as a fact entry, but remains pending and grants nothing.
        page.get_by_role("button", name="登记外部审批事实").click()
        dialog = page.locator("#register-dialog")
        dialog.locator('[name="applicant"]').fill(administrator.id)
        dialog.locator('[name="decision"]').select_option("approved")
        dialog.locator('[name="identity_label"]').fill(approver_basis.identity["identity_label"])
        dialog.locator('[name="responsibility"]').fill(approver_basis.identity["responsibility"])
        dialog.locator('[name="authority_basis_ref"]').fill(approver_basis.id)
        dialog.locator('[name="approved_at"]').fill((now - timedelta(minutes=2)).strftime("%Y-%m-%dT%H:%M"))
        dialog.locator('[name="valid_from"]').fill((now - timedelta(minutes=2)).strftime("%Y-%m-%dT%H:%M"))
        dialog.locator('[name="valid_until"]').fill((now + timedelta(days=2)).strftime("%Y-%m-%dT%H:%M"))
        dialog.locator('[name="knowledge_base_id"]').fill(kb.id)
        dialog.locator('[name="level"]').fill(document.access_level)
        dialog.locator('[name="category"]').fill(document.category)
        dialog.locator('[name="need_class"]').fill(document.need_class)
        dialog.locator('[name="operations"]').select_option("document.read")
        dialog.locator('[name="evidence_locator"]').fill("SYNTHETIC only; registration is not verification")
        dialog.get_by_role("button", name="保存为待核查").click()
        page.get_by_text("事实已登记为待核查").wait_for()
        self_record_id = page.locator("#record-id").inner_text()
        db.expire_all(); self_record = db.get(BusinessApproval, self_record_id)
        assert self_record.applicant == administrator.id and self_record.status == "pending"
        assert self_record.verification_state == "unverified" and self_record.approver is None
        assert db.scalar(select(func.count()).select_from(AuthorizationGrant)) == grant_count_before

        # Ordinary uploaded evidence remains untrusted and cannot satisfy the positive verification gate.
        untrusted_item = page.locator(".approval-item").filter(has_text=untrusted.id); untrusted_item.click()
        page.locator("#verification-block:not(.hidden)").wait_for()
        assert "未受控" in page.locator("#verification-block").inner_text()
        assert page.locator("#authority-basis option").count() == 2
        assert page.locator("#authority-basis option").nth(1).get_attribute("value") == approver_basis.id
        assert page.locator("#verify-button").is_disabled()
        page.screenshot(path=str(screenshot_dir / "sa-unverified-evidence.png"), full_page=True)

        page.locator("#evidence-file").set_input_files({"name": "SYNTHETIC-approval.pdf", "mimeType": "application/pdf", "buffer": synthetic_pdf()})
        page.locator("#evidence-source").fill("SYNTHETIC browser upload")
        page.get_by_role("button", name="登记附件").click()
        page.wait_for_function("document.querySelector('#evidence-message').textContent.length > 0")
        assert "附件已关联，但仍未核实" in page.locator("#evidence-message").inner_text()
        assert page.locator("#verify-button").is_disabled()
        page.screenshot(path=str(screenshot_dir / "sa-attached-unverified.png"), full_page=True)

        # Positive path uses an already-controlled fixture proof; the UI links it, then records a server verification.
        page.locator(".approval-item").filter(has_text=positive.id).click()
        page.locator(f'#evidence-candidates input[value="{trusted_proof.id}"]').check()
        page.get_by_role("button", name="更新关联材料").click()
        page.get_by_text("材料关联已更新").wait_for()
        page.locator("#authority-basis").select_option(approver_basis.id)
        assert not page.locator("#verify-button").is_disabled()
        page.locator("#verification-reason").fill("SYNTHETIC: controlled external approval evidence matched")
        page.get_by_role("button", name="提交核查记录").click()
        page.get_by_text("服务端核查结果：已核实").wait_for()
        assert page.locator("#verify-button").is_disabled()
        db.expire_all(); positive = db.get(BusinessApproval, positive.id)
        assert positive.status == "approved" and positive.verification_state == "verified"
        assert approval_is_current(db, positive)
        assert db.scalar(select(func.count()).select_from(AuthorizationGrant)) == grant_count_before
        page.screenshot(path=str(screenshot_dir / "sa-verified.png"), full_page=True)

        # Register the exact controlled external revocation evidence and observe the derived receipt/status.
        page.locator("#revocation-ref").fill(revoke_proof.id)
        page.locator("#revocation-reason").fill("SYNTHETIC external revocation record matched")
        page.get_by_role("button", name="登记外部撤销").click()
        page.get_by_text("撤销登记回执").wait_for()
        page.locator("#record-state").filter(has_text="已撤销").wait_for()
        assert page.locator("#revocation-form").is_hidden()
        assert page.locator("#verify-button").is_disabled()
        db.expire_all(); positive = db.get(BusinessApproval, positive.id)
        revocation = db.scalar(select(ApprovalRevocation).where(ApprovalRevocation.approval_id == positive.id))
        assert revocation is not None
        assert not approval_is_current(db, positive)
        assert positive.status == "approved" and positive.verification_state == "verified"
        assert page.locator("#facts").inner_text().find(revocation.id) >= 0
        assert db.scalar(select(func.count()).select_from(AuthorizationGrant)) == grant_count_before
        page.screenshot(path=str(screenshot_dir / "sa-revoked.png"), full_page=True)

        # A non-admin receives no access to the workspace or its approval records.
        context.clear_cookies(); page.goto(address + "access-review"); page.wait_for_load_state("networkidle")
        page.locator("#login-form [name=username]").fill(employee.username)
        page.locator("#login-form [name=password]").fill("synthetic employee password")
        page.get_by_role("button", name="登录").click()
        page.get_by_text("当前账号不具备系统管理员授权管理能力").wait_for()
        assert page.locator("#workspace").is_hidden()
        assert not errors, errors
        browser.close()

    db.expire_all(); db.refresh(untrusted); db.refresh(untrusted_proof)
    assert untrusted.verification_state == "unverified" and untrusted.status == "pending"
    assert untrusted_proof.attested is False
