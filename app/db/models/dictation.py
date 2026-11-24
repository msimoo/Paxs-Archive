"""Audio dictations owned by a study and its current patient."""
from datetime import datetime
from uuid import UUID

from sqlalchemy import DateTime, ForeignKey, LargeBinary, String, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.models.base import Base


class StudyDictation(Base):
    __tablename__ = "study_dictations"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    study_id: Mapped[int] = mapped_column(
        ForeignKey("studies.id", ondelete="CASCADE"), index=True, nullable=False
    )
    recorded_by: Mapped[str] = mapped_column(String(64), nullable=False)
    content_type: Mapped[str] = mapped_column(String(32), nullable=False)
    audio: Mapped[bytes] = mapped_column(LargeBinary, deferred=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
