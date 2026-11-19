"""Document feedback is independent of publication, approvals and answers."""
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from .models import DocumentVersion, QueryTask, User, new_id, utc_now


class FeedbackBase(DeclarativeBase):
    """Feedback starts at migration 0010, outside the legacy 0001 metadata."""


class DocumentFeedback(FeedbackBase):
    __tablename__ = "document_feedback"
    __table_args__ = (
        CheckConstraint("(task_id IS NULL) <> (original_version_id IS NULL)", name="feedback_one_origin"),
        CheckConstraint("status IN ('submitted', 'processing', 'replied')", name="feedback_valid_status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    reporter_id: Mapped[str] = mapped_column(ForeignKey(User.id), index=True)
    task_id: Mapped[str | None] = mapped_column(ForeignKey(QueryTask.id), index=True)
    original_version_id: Mapped[str | None] = mapped_column(ForeignKey(DocumentVersion.id), index=True)
    handler_id: Mapped[str | None] = mapped_column(ForeignKey(User.id))
    text: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="submitted")
    reply: Mapped[str | None] = mapped_column(Text)
    responded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revision: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class FeedbackDependency(FeedbackBase):
    __tablename__ = "document_feedback_dependencies"

    feedback_id: Mapped[str] = mapped_column(ForeignKey("document_feedback.id"), primary_key=True)
    version_id: Mapped[str] = mapped_column(ForeignKey(DocumentVersion.id), primary_key=True)
