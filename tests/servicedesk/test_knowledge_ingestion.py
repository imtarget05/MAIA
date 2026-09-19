"""T3 knowledge-ingestion tests (plan T3 RED list).

Covers: versioned ingest, idempotent retry, cross-tenant doc_id isolation,
server-side ACL rejection, scanned-PDF rejection, revoke taking effect
immediately, and source reads re-checking permissions.
"""
from __future__ import annotations

import io
from uuid import uuid4

import pytest

from maia.servicedesk.knowledge.citations import read_source
from maia.servicedesk.knowledge.ingest import (
    UnsupportedDocument,
    ingest_document,
    point_id,
    revoke_document,
)
from maia.servicedesk.models import SDChunk, SDDocument
from maia.servicedesk.policies import NotAuthorized, OutOfScope
from maia.servicedesk.schemas import Principal
from tests.servicedesk.conftest import TENANT_A, TENANT_B

RUNBOOK = """# VPN cannot connect (error 809)

1. Confirm the user is on the corporate network or home internet.
2. Re-import the VPN profile from the IT portal.
3. If error 809 persists, check the firewall profile version.
"""


def _make_user(session, tenant: str, role: str, groups=("network",), tag: str = "kb"):
    """Create a real user + membership so FKs (owner_id) are satisfied."""
    from maia.auth import get_password_hash
    from maia.servicedesk.models import SDMembership, SDUser

    email = f"{role}-{tag}-{uuid4().hex[:8]}@{tenant}.test"
    user = SDUser(email=email, password_hash=get_password_hash("pw"))
    session.add(user)
    session.flush()
    group = groups[0] if groups else None
    session.add(
        SDMembership(user_id=user.id, tenant_id=tenant, role=role, group_id=group)
    )
    session.flush()
    return Principal(
        user_id=user.id,
        tenant_id=tenant,
        role=role,
        group_ids=frozenset(groups),
    )


def admin_principal(session, tenant: str = TENANT_A, groups=("network",)) -> Principal:
    return _make_user(session, tenant, "admin", groups, tag="kbadmin")


def agent_principal(session, tenant: str = TENANT_A, groups=("network",)) -> Principal:
    return _make_user(session, tenant, "agent", groups, tag="kbagent")


def requester_principal(session, tenant: str = TENANT_A) -> Principal:
    return _make_user(session, tenant, "requester", (), tag="kbreq")


def ingest(
    session,
    *,
    principal=None,
    doc_id: str = "vpn-runbook",
    content: str = RUNBOOK,
    filename: str = "vpn-runbook.md",
    allowed_groups=("network",),
    visibility: str = "agent",
):
    return ingest_document(
        session,
        principal or admin_principal(session),
        doc_id=doc_id,
        title="VPN runbook",
        content=content,
        filename=filename,
        allowed_groups=list(allowed_groups),
        visibility=visibility,
    )


def test_ingest_creates_active_versioned_document_with_chunks(db_session):
    document_id = ingest(db_session)
    document = db_session.get(SDDocument, document_id)

    assert document.active is True
    assert document.version == 1
    assert document.tenant_id == TENANT_A
    assert document.allowed_groups == ["network"]
    assert document.deleted_at is None

    chunks = db_session.query(SDChunk).filter(SDChunk.document_id == document_id).all()
    assert chunks, "chunks must be persisted for the active version"
    for chunk in chunks:
        assert chunk.tenant_id == TENANT_A
        assert chunk.document_version == 1
        assert chunk.chunk_id.startswith("vpn-runbook_v1_")
        assert chunk.chunk_metadata["point_id"] == point_id(
            TENANT_A, "vpn-runbook", 1, chunk.chunk_id
        )


def test_ingest_retry_same_content_is_idempotent(db_session):
    first = ingest(db_session)
    second = ingest(db_session)

    assert first == second, "a retried upload must not create a new version"
    versions = (
        db_session.query(SDDocument)
        .filter(SDDocument.doc_id == "vpn-runbook", SDDocument.tenant_id == TENANT_A)
        .count()
    )
    assert versions == 1


def test_ingest_changed_content_creates_new_version_and_deactivates_old(db_session):
    first = ingest(db_session)
    second = ingest(db_session, content=RUNBOOK + "\n4. Escalate to the network team.\n")

    assert first != second
    old = db_session.get(SDDocument, first)
    new = db_session.get(SDDocument, second)
    assert new.version == 2
    assert new.active is True
    assert old.active is False, "superseded versions must stop being served"


def test_same_doc_id_in_two_tenants_does_not_collide(db_session):
    tenant_a_doc = ingest(db_session, principal=admin_principal(db_session, TENANT_A))
    tenant_b_doc = ingest(
        db_session,
        principal=admin_principal(db_session, TENANT_B, groups=("network-b",)),
        allowed_groups=("network-b",),
    )

    assert tenant_a_doc != tenant_b_doc
    doc_a = db_session.get(SDDocument, tenant_a_doc)
    doc_b = db_session.get(SDDocument, tenant_b_doc)
    assert doc_a.tenant_id == TENANT_A
    assert doc_b.tenant_id == TENANT_B
    assert doc_a.version == doc_b.version == 1

    point_a = point_id(TENANT_A, "vpn-runbook", 1, "vpn-runbook_v1_0")
    point_b = point_id(TENANT_B, "vpn-runbook", 1, "vpn-runbook_v1_0")
    assert point_a != point_b, "vector point IDs must be tenant-scoped"


def test_empty_content_rejected(db_session):
    with pytest.raises(UnsupportedDocument):
        ingest(db_session, content="   \n  ")


def test_empty_acl_rejected(db_session):
    with pytest.raises(ValueError, match="allowed_groups"):
        ingest(db_session, allowed_groups=[])


def test_unsupported_file_type_rejected(db_session):
    with pytest.raises(UnsupportedDocument, match="unsupported file type"):
        ingest(db_session, content=b"binary", filename="runbook.docx")


def test_scanned_pdf_without_text_layer_rejected(db_session):
    pdf_bytes = _blank_pdf()
    with pytest.raises(UnsupportedDocument, match="no text layer"):
        ingest(db_session, content=pdf_bytes, filename="scan.pdf")


def test_text_pdf_with_text_layer_is_ingested(db_session):
    """A PDF that does carry a text layer must ingest normally."""
    pdf_bytes = _text_pdf("VPN runbook step 1: re-import the profile")
    document_id = ingest(db_session, content=pdf_bytes, filename="vpn.pdf")

    document = db_session.get(SDDocument, document_id)
    assert document.active is True
    chunks = db_session.query(SDChunk).filter(SDChunk.document_id == document_id).all()
    assert any("re-import the profile" in chunk.text for chunk in chunks)


def test_non_admin_cannot_ingest(db_session):
    with pytest.raises(NotAuthorized):
        ingest(db_session, principal=agent_principal(db_session))


def _blank_pdf() -> bytes:
    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def _text_pdf(text: str) -> bytes:
    """Minimal PDF carrying a real text layer (no extra dependency).

    A Type1 Helvetica resource must be present or pypdf's extract_text()
    returns an empty string (verified empirically), which would make this
    fixture indistinguishable from a scanned document.
    """
    from pypdf import PdfWriter
    from pypdf.generic import (
        DecodedStreamObject,
        DictionaryObject,
        NameObject,
    )

    writer = PdfWriter()
    page = writer.add_blank_page(width=400, height=200)

    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    fonts = DictionaryObject({NameObject("/F1"): writer._add_object(font)})  # noqa: SLF001
    page[NameObject("/Resources")] = DictionaryObject(
        {NameObject("/Font"): fonts}
    )

    stream = DecodedStreamObject()
    escaped = text.replace("(", r"\(").replace(")", r"\)")
    stream.set_data(f"BT /F1 12 Tf 20 100 Td ({escaped}) Tj ET".encode("latin-1"))
    page[NameObject("/Contents")] = writer._add_object(stream)  # noqa: SLF001

    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


# --- part 3: citation source reads re-check ACL/version/tenant ---


def _first_chunk_id(session, document_id) -> str:
    chunk = (
        session.query(SDChunk)
        .filter(SDChunk.document_id == document_id)
        .order_by(SDChunk.ordinal)
        .first()
    )
    return chunk.chunk_id


def test_authorized_agent_can_read_source(db_session):
    document_id = ingest(db_session)
    chunk_id = _first_chunk_id(db_session, document_id)

    source = read_source(db_session, agent_principal(db_session, groups=("network",)), chunk_id)
    assert source["title"] == "VPN runbook"
    assert source["document_version"] == 1
    assert "error 809" in source["text"].lower()


def test_agent_outside_group_cannot_read_source(db_session):
    document_id = ingest(db_session)
    chunk_id = _first_chunk_id(db_session, document_id)

    with pytest.raises(OutOfScope):
        read_source(db_session, agent_principal(db_session, groups=("app",)), chunk_id)


def test_requester_cannot_read_agent_only_source(db_session):
    """A restricted runbook must never reach a requester-visible comment."""
    document_id = ingest(db_session, visibility="agent")
    chunk_id = _first_chunk_id(db_session, document_id)

    with pytest.raises(OutOfScope):
        read_source(db_session, requester_principal(db_session), chunk_id)


def test_requester_can_read_requester_visible_source(db_session):
    document_id = ingest(db_session, visibility="requester")
    chunk_id = _first_chunk_id(db_session, document_id)

    source = read_source(db_session, requester_principal(db_session), chunk_id)
    assert source["visibility"] == "requester"


def test_cross_tenant_source_read_is_denied(db_session):
    document_id = ingest(db_session, principal=admin_principal(db_session, TENANT_A))
    chunk_id = _first_chunk_id(db_session, document_id)

    with pytest.raises(OutOfScope):
        read_source(db_session, agent_principal(db_session, TENANT_B, ("network-b",)), chunk_id)


def test_revoke_takes_effect_immediately_for_source_reads(db_session):
    admin = admin_principal(db_session)
    document_id = ingest(db_session, principal=admin)
    chunk_id = _first_chunk_id(db_session, document_id)

    # Readable before revoke...
    read_source(db_session, agent_principal(db_session), chunk_id)

    assert revoke_document(db_session, admin, document_id) is True

    # ...and immediately denied after, with no index rebuild needed.
    with pytest.raises(OutOfScope):
        read_source(db_session, agent_principal(db_session), chunk_id)


def test_revoke_only_by_admin_and_only_own_tenant(db_session):
    document_id = ingest(db_session, principal=admin_principal(db_session, TENANT_A))

    with pytest.raises(NotAuthorized):
        revoke_document(db_session, agent_principal(db_session), document_id)

    other_tenant_admin = admin_principal(db_session, TENANT_B, ("network-b",))
    assert revoke_document(db_session, other_tenant_admin, document_id) is False


def test_superseded_version_source_is_denied(db_session):
    first = ingest(db_session)
    chunk_id = _first_chunk_id(db_session, first)
    ingest(db_session, content=RUNBOOK + "\n4. Escalate.\n")

    with pytest.raises(OutOfScope):
        read_source(db_session, agent_principal(db_session), chunk_id)