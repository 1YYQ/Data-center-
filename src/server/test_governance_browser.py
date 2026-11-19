"""Real Chrome governance flow over synthetic local data; no enterprise facts."""
from pathlib import Path
import json
import uuid
import os
import pytest
from sqlalchemy import select
from .test_worker import local_stack, synthetic_pdf, STARTUP, WARNING
from .test_services import fixture_db
from .models import Document, DocumentVersion, ParseRun, ContentBlock, ChunkSet, User, AuthorizationGrant, utc_now
from .services import save_upload
from .testing_governance import seed_confirmation, _authority, _proof
from .governance_models import BusinessConfirmation, GovernanceEvidence
from datetime import datetime, timedelta
from . import governance as g

OUTPUT = Path(os.environ.get('DATACENTER_TEST_EVIDENCE_ROOT', 'outputs/local-browser')) / 'static-governance' / uuid.uuid4().hex

def test_governance_workspace_roles_review_and_publish(local_stack):
    pw = pytest.importorskip('playwright.sync_api')
    chrome = Path(r'C:\Program Files\Google\Chrome\Application\chrome.exe')
    if not chrome.exists(): pytest.skip('Chrome executable unavailable')
    db, config, client, address, employee, administrator, kb, doc, old, upstream, _ = local_stack
    manager = next(x for x in db.scalars(select(User)).all() if 'knowledge_manager' in x.roles)
    file = save_upload([synthetic_pdf()], 'SYNTHETIC-DRAFT.pdf', 'application/pdf', manager, config)
    db.add(file); db.flush()
    draft = DocumentVersion(document_id=doc.id, version_no=old.version_no+1, source_file_id=file.id, created_by=manager.id,
        model_scope=['DEMO-1'], source_metadata={'channel':'synthetic','source_description':'SYNTHETIC Chrome fixture'})
    db.add(draft); db.flush()
    run = ParseRun(document_version_id=draft.id, origin='manual', status='needs_review')
    db.add(run); db.flush()
    for ordinal, text, kind in ((1, STARTUP, 'paragraph'), (2, WARNING, 'warning')):
        db.add(ContentBlock(parse_run_id=run.id, ordinal=ordinal, kind=kind, text_content=text, locator={'file_page':1,'section':'SYNTHETIC '+str(ordinal)}))
    chunk = ChunkSet(parse_run_id=run.id, status='ready'); db.add(chunk); db.commit()
    OUTPUT.mkdir(parents=True, exist_ok=True)
    errors=[]; checks=[]
    with pw.sync_playwright() as p:
        browser=p.chromium.launch(headless=True,executable_path=str(chrome),args=['--disable-background-networking'])
        context=browser.new_context(viewport={'width':1366,'height':900}); page=context.new_page()
        page.on('pageerror',lambda e:errors.append(str(e)));page.set_default_timeout(12000)
        def login(user,password):
            page.goto(address+'manage');page.wait_for_load_state('networkidle')
            page.get_by_label('账号').fill(user.username);page.get_by_label('口令').fill(password)
            page.get_by_role('button',name='登录',exact=True).click()
        def choose():
            page.locator('#doc').select_option(doc.id);page.locator('#version').select_option(draft.id)
            page.wait_for_selector(f'.block[data-block-id]');page.wait_for_function('(id)=>document.querySelector("#run").value===id',arg=run.id)
        login(manager,'manager password');page.wait_for_load_state('networkidle');page.screenshot(path=str(OUTPUT/'login-diagnostic.png'));assert page.locator('#workspace').is_visible(), (page.locator('#login-error').inner_text(),errors,page.locator('#status').inner_text());choose()
        response=context.request.get(address.rstrip('/')+page.locator('#source-file').get_attribute('href'))
        assert response.status==200 and response.body().startswith(b'%PDF-');checks.append('exact-version source PDF')
        page.get_by_role('button',name='发布检查',exact=True).click();page.locator('#preview').click()
        page.get_by_text('仍有发布阻断项',exact=True).wait_for();assert page.locator('#execute-publish').is_hidden();checks.append('missing evidence blocked')
        page.get_by_role('button',name='原文核验',exact=True).click()
        # Simulate a committed write whose response is lost, then retry the
        # same user action. The command key must remain stable without repeating the mutation.
        keys=[]
        def lose_response(route):
            keys.append(route.request.headers['idempotency-key']);route.fetch();route.abort('failed')
        page.route('**/blocks/*/review',lose_response,times=1)
        first=page.locator('.block').first;first.locator('select').select_option('approved');first.locator('input').fill('SYNTHETIC source checked')
        first.get_by_role('button',name='保存核验').click();page.get_by_text('操作结果未知',exact=False).wait_for()
        page.on('request',lambda r:keys.append(r.headers.get('idempotency-key')) if '/review' in r.url and r.method=='POST' else None)
        first.get_by_role('button',name='保存核验').click();page.get_by_text('核验已保存',exact=True).wait_for()
        assert len(keys)>=2 and keys[0]==keys[1];checks.append('lost response stable idempotency key')
        second=page.locator('.block').nth(1);second.locator('select').select_option('approved');second.locator('input').fill('SYNTHETIC warning checked')
        second.get_by_role('button',name='保存核验').click();page.wait_for_function('()=>Array.from(document.querySelectorAll(".block strong")).every(x=>x.textContent.includes("approved"))')
        db.expire_all();blocks=db.scalars(select(ContentBlock).where(ContentBlock.parse_run_id==run.id).order_by(ContentBlock.ordinal)).all()
        assert [x.revision for x in blocks]==[1,1]
        # Only controlled test initialization supplies external witness facts.
        # Browser UI has no attest/approved shortcut.
        binding=g.source_binding(db,draft)
        seed_confirmation(db,manager,draft,'applicability','applicable',binding)
        seed_confirmation(db,manager,draft,'classification','classified',{**binding,'access_level':doc.access_level,'category':doc.category,'need_class':doc.need_class,'rule_version':'synthetic-v1'})
        authority,_=_authority(db,purpose='key_content',scope={k:binding[k] for k in ('version_id','source_sha256','scope_hash')})
        db.commit()
        page.locator('#version').select_option(draft.id);page.get_by_role('button',name='业务依据',exact=True).click()
        page.locator('#kind').select_option('key_content');page.locator('#authority').select_option(authority.id)
        page.locator('#confirmed').fill((datetime.now()-timedelta(minutes=1)).strftime('%Y-%m-%dT%H:%M'))
        page.locator('#register-confirm').click();page.get_by_text('真实确认事实已登记',exact=False).wait_for()
        db.expire_all();professional=db.scalar(select(BusinessConfirmation).where(BusinessConfirmation.document_version_id==draft.id,BusinessConfirmation.kind=='key_content'))
        assert professional and professional.verification_state=='pending'
        page.get_by_text('补充本版本真实依据材料',exact=True).click();page.locator('#evidence-source').fill('SYNTHETIC ordinary uploaded attachment')
        page.locator('#evidence-file').set_input_files({'name':'SYNTHETIC-proof.pdf','mimeType':'application/pdf','buffer':synthetic_pdf()});page.locator('#upload-evidence').click();page.get_by_text('待核查材料已保存',exact=False).wait_for()
        db.expire_all();untrusted=db.scalar(select(GovernanceEvidence).where(GovernanceEvidence.owner_id==draft.id,GovernanceEvidence.attested.is_(False)))
        card=page.locator(f'[data-confirmation-id="{professional.id}"]');card.get_by_role('button',name='查看依据与核查').click();card.locator('select').select_option(untrusted.id)
        card.get_by_role('button',name='保存依据关联').click();page.get_by_text('依据已关联；上传与关联不会自行成为已核实事实',exact=True).wait_for()
        card=page.locator(f'[data-confirmation-id="{professional.id}"]');card.get_by_role('button',name='查看依据与核查').click();card.get_by_label('核查原因').fill('SYNTHETIC attachment alone must fail');card.get_by_role('button',name='记录核查通过').click()
        page.get_by_text('真实确认、职责、精确内容范围及受控依据必须一致',exact=True).wait_for();checks.append('ordinary uploaded attachment cannot self-attest')
        page.screenshot(path=str(OUTPUT/'manager-untrusted-proof-blocked.png'),full_page=True)
        db.expire_all();professional=db.get(BusinessConfirmation,professional.id)
        proof=_proof(db,manager,'version',draft.id,g._confirmation_assertion(professional));db.commit()
        page.locator('#version').select_option(draft.id)
        card=page.locator(f'[data-confirmation-id="{professional.id}"]');card.get_by_role('button',name='查看依据与核查').click()
        card.locator('select').select_option(proof.id);card.get_by_role('button',name='保存依据关联').click();page.get_by_text('依据已关联；上传与关联不会自行成为已核实事实',exact=True).wait_for()
        card=page.locator(f'[data-confirmation-id="{professional.id}"]');card.get_by_role('button',name='查看依据与核查').click()
        card.get_by_label('核查原因').fill('SYNTHETIC controlled witness and exact scope checked');card.get_by_role('button',name='记录核查通过').click();page.get_by_text('系统核查结果已读取',exact=True).wait_for()
        checks.append('actual structured confirmation registered, owned proof linked and verified in Chrome')
        page.screenshot(path=str(OUTPUT/'manager-confirmation-verified.png'),full_page=True)
        page.get_by_role('button',name='原文核验',exact=True).click();page.wait_for_selector(f'#professional option[value="{professional.id}"]',state='attached')
        page.locator('#professional').select_option(professional.id);page.locator('#create-review').click();page.get_by_text('完整核验集已创建',exact=False).wait_for()
        page.screenshot(path=str(OUTPUT/'manager-review.png'),full_page=True);checks.append('all blocks and current professional confirmation reviewed')
        page.get_by_role('button',name='发布检查',exact=True).click()
        page.locator('#reference').check();page.locator('#save-reference').click();page.get_by_text('参考状态已变更；正式资格不变',exact=True).wait_for()
        db.expire_all();assert db.get(DocumentVersion,draft.id).publish_status=='draft';checks.append('reference switch independent of publication')
        page.locator('#preview').click();page.get_by_text('预检允许发布',exact=True).wait_for()
        page.locator('#execute-publish').click();page.wait_for_function('()=>document.querySelector("#receipt").textContent.includes("requires_rebuild")')
        page.wait_for_function('()=>document.querySelector("#index-state").textContent.includes("stale")')
        page.screenshot(path=str(OUTPUT/'manager-published-stale.png'),full_page=True);checks.append('publish receipt and stale index')
        page.locator('#build-index').click();page.wait_for_function('()=>document.querySelector("#index-state").textContent.includes("ready") && document.querySelector("#index-state").textContent.includes("已纳入")')
        page.wait_for_function('()=>!document.querySelector("#build-index").disabled');page.screenshot(path=str(OUTPUT/'manager-index-ready.png'),full_page=True);checks.append('local keyword index ready and exact version included')
        assert client.get('/knowledge-bases/'+kb.id+'/index-status').json()['included_version_ids']==[]
        inaccessible=Document(knowledge_base_id=kb.id,title='SYNTHETIC separate scope',access_level=doc.access_level,category='ungranted-category',need_class=doc.need_class)
        db.add(inaccessible);db.commit()
        rejected=context.request.post(address+'knowledge-bases/'+kb.id+'/index-generations',data=json.dumps({'embedding_profile_id':'local-keyword'}),headers={'Content-Type':'application/json','X-CSRF-Token':page.evaluate('csrf'),'Idempotency-Key':str(uuid.uuid4())})
        assert rejected.status==403,rejected.text();inaccessible.deleted_at=utc_now();db.commit();checks.append('partial tuple manager cannot build whole-library index; employee sees no managed version IDs')

        page.locator('#withdraw-reason').fill('SYNTHETIC withdrawal verification');page.locator('#preview-withdraw').click();page.get_by_text('预检允许撤回',exact=True).wait_for()
        page.locator('#execute-withdraw').click();page.wait_for_function('()=>document.querySelector("#version-state").textContent.includes("withdrawn")')
        assert '撤回回执' in page.locator('#receipt').inner_text();checks.append('withdraw with actual receipt')
        page.screenshot(path=str(OUTPUT/'manager-withdrawn.png'),full_page=True)
        # Revocation is external to this browser and must clear retained quotes.
        grant=db.scalar(select(AuthorizationGrant).where(AuthorizationGrant.subject_user_id==manager.id,AuthorizationGrant.operation=='knowledge.manage'))
        grant.revoked_at=utc_now();db.commit();page.locator('#reload').click()
        page.wait_for_selector('#workspace.hidden',state='attached');assert not page.locator('#blocks').inner_text();assert not page.locator('#source-file').get_attribute('href');checks.append('revocation clears sensitive content and links')
        page.screenshot(path=str(OUTPUT/'manager-access-cleared.png'),full_page=True)
        context.clear_cookies();login(employee,'synthetic employee password');page.locator('#login-error').get_by_text('当前账号没有知识管理角色',exact=True).wait_for();assert page.locator('#workspace').is_hidden();checks.append('employee role refused')
        context.clear_cookies();login(administrator,'synthetic admin password');page.locator('#login-error').get_by_text('当前账号没有知识管理角色',exact=True).wait_for();assert page.locator('#workspace').is_hidden();checks.append('system administrator has no implicit knowledge access')
        assert not errors, errors
        browser.close()
    db.expire_all();final=db.get(DocumentVersion,draft.id);assert final.publish_status=='withdrawn' and final.applicability_status=='confirmed' and final.reference_access_state=='open'
    (OUTPUT/'governance-report.json').write_text(json.dumps({'status':'PASS','browser':'real installed Chrome','data':'synthetic controlled fixtures only','checks':checks,'page_errors':errors,'scope':'static HTML/JS, SQLite HTTP fixture; PostgreSQL/Celery integration verified separately'},ensure_ascii=False,indent=2),encoding='utf-8')


def test_governance_dynamic_controls_do_not_leave_tabs_disabled():
    """Pin the aggregate race without network latency or arbitrary sleeps.

    A version refresh replaces confirmation cards before its last reads finish.
    A new card can then start another operation while original tabs are disabled.
    Finish the outer read first to reproduce the stale disabled-state restore.
    """
    pw = pytest.importorskip('playwright.sync_api')
    chrome = Path(r'C:\Program Files\Google\Chrome\Application\chrome.exe')
    if not chrome.exists(): pytest.skip('Chrome executable unavailable')
    static = Path(__file__).parent/'static'
    with pw.sync_playwright() as p:
        browser=p.chromium.launch(headless=True,executable_path=str(chrome))
        page=browser.new_page()
        page.set_content((static/'governance.html').read_text(encoding='utf-8'))
        page.add_script_tag(path=str(static/'governance.js'))
        result=page.evaluate("""async () => {
            let finishOuter, finishInner, nestedRan=false;
            const outerGate=new Promise(resolve=>finishOuter=resolve);
            const innerGate=new Promise(resolve=>finishInner=resolve);
            const tab=document.querySelector('[data-tab="review"]');
            const outer=perform(async()=>{
                const dynamic=document.createElement('button');
                dynamic.id='synthetic-dynamic-confirmation';
                dynamic.textContent='Synthetic refreshed confirmation';
                document.querySelector('#confirmations').replaceChildren(dynamic);
                await outerGate;
            });
            const dynamic=document.querySelector('#synthetic-dynamic-confirmation');
            dynamic.focus();
            const newControlFocused=document.activeElement===dynamic;
            const inner=perform(async()=>{nestedRan=true;await innerGate;});
            finishOuter();await outer;
            finishInner();await inner;
            return {tabDisabled:tab.disabled,nestedRan,newControlFocused};
        }""")
        assert not result['tabDisabled'], result
        assert not result['nestedRan'], result
        assert not result['newControlFocused'], result
        browser.close()
