"""FruLink — one edge of the IBM/Lenovo FRU crosswalk (FRU ↔ related part number).

Each row says "FRU <fru_raw> relates to <related_raw> as <rel_kind>" (11S PN, mfg
model, option, tray, bracket, ...), with sourcing context (manufacturer, qual status,
machine/series) carried alongside. Populated by the FRU matrix ingest command; read
by fru_matrix_service for the materials detail "FRU matrix" / "Used in FRUs" panels.

Called by: app/services/fru_matrix_service.py, app/management/ingest_fru_matrix.py
Depends on: app/constants.FruLinkKind (rel_kind vocabulary), Base
"""

from datetime import UTC, datetime

from sqlalchemy import Column, Date, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import validates

from ..database import UTCDateTime
from .base import Base


class FruLink(Base):
    __tablename__ = "fru_links"

    id = Column(Integer, primary_key=True)

    # norms are normalize_mpn_key of the CANONICALIZED part number, not of the raw
    # column verbatim: Lenovo FRU-PN rows keep the SAP-padded raw ("0000000NV340_E00")
    # with the norm of the de-padded FRU ("00nv340") — see ingest_fru_matrix._lenovo_fru.
    fru_raw = Column(String(64), nullable=False)  # FRU as it appears in the source
    fru_norm = Column(String(64), nullable=False)  # normalize_mpn_key of the canonical (de-padded) FRU
    related_raw = Column(String(64), nullable=False)  # related PN as it appears
    related_norm = Column(String(64), nullable=False)  # normalize_mpn_key of the canonical related PN
    rel_kind = Column(String(24), nullable=False)  # FruLinkKind value

    manufacturer = Column(String(128))  # maker of the related part (mfg_model/drive_pn)
    description = Column(Text)  # human description of the part/FRU
    series = Column(String(64))  # xSeries, pSeries, ...
    machine = Column(String(128))  # machine/platform context (Storwize, POWER 8, ...)
    qual_status = Column(String(64))  # "qlot approved", "qlot approved - Only EMEA", "cdc_pending"
    qual_date = Column(Date)  # qualification date when known
    note = Column(Text)  # free-text context (feature codes, comments, FW)
    source_sheet = Column(String(64), nullable=False)  # workbook sheet the row came from

    created_at = Column(UTCDateTime, default=lambda: datetime.now(UTC))
    updated_at = Column(
        UTCDateTime,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    @validates("rel_kind")
    def _validate_rel_kind(self, _key, value):
        from ..constants import FruLinkKind

        return FruLinkKind(value).value  # raises ValueError on unknown

    __table_args__ = (
        Index("ix_fru_links_fru_norm", "fru_norm"),
        Index("ix_fru_links_related_norm", "related_norm"),
        UniqueConstraint("fru_norm", "related_norm", "rel_kind", "source_sheet", name="uq_fru_links_edge"),
    )
