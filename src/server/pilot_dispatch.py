"""One in-process consumer for the pilot's already accepted work.

Queries have priority. Parse/chunk and embedding workers run one accepted job
at a time while the query queue is idle, sharing the API's endpoint instances.
No acceptance, service admission, publication or index activation happens here.
"""
from .worker import run_one
from .parse_worker import scan_once as scan_processing
from .embedding_worker import scan_once as scan_embedding


def advance_once(application, *, gateway_url: str, profile_revision: int, report=None) -> dict:
    report = report or (lambda phase: None)
    config = application.state.settings
    report("query")
    query = run_one(config, gateway_url=gateway_url, profile_id="local-pi", profile_revision=profile_revision)
    if query.get("state") != "idle":
        return {"kind": "query", "result": query}
    return advance_processing_once(application, report=report)


def advance_processing_once(application, *, report=None) -> dict:
    """Document preparation without starting a query or preloading BGE."""
    report = report or (lambda phase: None)
    config = application.state.settings
    # Borrow sessions from the existing API engine; never create another model
    # endpoint or a separate BGE process for document batches.
    for kind, scan in (("processing", scan_processing), ("embedding", scan_embedding)):
        report(kind)
        with application.state.session_factory() as db:
            result = scan(db, config, application.state.service_endpoints, limit=1)
        if result["count"]:
            return {"kind": kind, "result": result}
    report("idle")
    return {"kind": "idle", "result": {"count": 0, "jobs": []}}
