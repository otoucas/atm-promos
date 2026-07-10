import datetime

from sqlalchemy import Boolean, Column, DateTime, Date, ForeignKey, Integer, String, Text
from sqlalchemy.orm import relationship

from .database import Base

STATUS_PENDING = "pending"
STATUS_ACTIVE = "active"
STATUS_ARCHIVED = "archived"

SOURCE_EMAIL = "email"
SOURCE_MANUAL = "manual"

# Un point de vente "erpnext" garde le fonctionnement historique (mot de passe
# admin, relevé Gmail, synchro ERPNext) — un seul aujourd'hui : Artemare.
# Un point de vente "standalone" est le format de dépannage : saisie manuelle
# des promos, aucune authentification sur ses propres pages de réglages.
INTEGRATION_ERPNEXT = "erpnext"
INTEGRATION_STANDALONE = "standalone"


class Store(Base):
    __tablename__ = "stores"

    id = Column(Integer, primary_key=True)
    code = Column(String(3), unique=True, nullable=False, index=True)  # ex: "ART" — utilisé dans l'URL /{code}/
    name = Column(String(200), nullable=False)
    integration = Column(String(20), nullable=False, default=INTEGRATION_STANDALONE)
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    # Contact du point de vente (collecté via Google Form externe, saisi par
    # Olivier dans /superadmin/stores/new) — l'email doit se terminer par
    # @hellopharmacie.com, et n'est confirmé (email_verified_at renseigné,
    # is_active mis à True) qu'après avoir cliqué le lien envoyé à sa création.
    # Nullable : le magasin ATM (Artemare, integration=erpnext) n'a pas de
    # contact — il n'est pas passé par ce circuit de demande.
    contact_name = Column(String(200), nullable=True)
    contact_email = Column(String(255), nullable=True, index=True)
    verification_token = Column(String(64), nullable=True, unique=True, index=True)
    email_verified_at = Column(DateTime, nullable=True)

    # Compte de connexion du point de vente (magasins "standalone") : le mot
    # de passe est choisi au clic sur le lien de confirmation (voir
    # verification_token ci-dessus), pas avant. password_reset_token/_at
    # permettent un lien "mot de passe oublié" à usage unique et limité dans
    # le temps (voir config.PASSWORD_RESET_TOKEN_VALIDITY_MINUTES).
    password_hash = Column(String(255), nullable=True)
    password_reset_token = Column(String(64), nullable=True, unique=True, index=True)
    password_reset_requested_at = Column(DateTime, nullable=True)

    promotions = relationship("Promotion", back_populates="store")


class Promotion(Base):
    __tablename__ = "promotions"

    id = Column(Integer, primary_key=True)
    store_id = Column(Integer, ForeignKey("stores.id"), nullable=True)
    brand_name = Column(String(200), nullable=False, default="")
    operation_label = Column(String(200), nullable=True)  # e.g. product/operation name, to disambiguate multi-op brands
    highco_reference = Column(Text, nullable=False)  # URL or identifier extracted from the QR code
    concerned_products = Column(Text, nullable=True)  # products/variants covered by this one QR link, e.g. multiple emails merged into it
    product_codes = Column(Text, nullable=True)  # comma-separated Winpharma CodeProduit values, entered by hand — the join key for the future export to the LGO
    valid_from = Column(Date, nullable=True)
    valid_until = Column(Date, nullable=True)
    status = Column(String(20), nullable=False, default=STATUS_PENDING)
    source = Column(String(20), nullable=False, default=SOURCE_MANUAL)
    logo_url = Column(Text, nullable=True)  # externally fetched logo (hotlinked)
    logo_path = Column(Text, nullable=True)  # locally uploaded/overridden logo, takes priority over logo_url
    raw_email_subject = Column(Text, nullable=True)
    source_message_id = Column(String(255), nullable=True)  # Gmail Message-ID, for SOURCE_EMAIL promotions only
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    validated_at = Column(DateTime, nullable=True)
    archived_at = Column(DateTime, nullable=True)

    store = relationship("Store", back_populates="promotions")
    generated_codes = relationship("GeneratedCode", back_populates="promotion")

    @property
    def display_name(self) -> str:
        if self.operation_label:
            return f"{self.brand_name} — {self.operation_label}"
        return self.brand_name

    @property
    def product_codes_list(self) -> list[str]:
        if not self.product_codes:
            return []
        return [c.strip() for c in self.product_codes.split(",") if c.strip()]

    @property
    def is_complete(self) -> bool:
        """Best-effort check that this promotion's data is usable as-is —
        drives the pre-sorted green/red grouping on the validation screen."""
        if not self.brand_name or self.brand_name.strip() in ("", "Promotion à nommer"):
            return False
        if len(self.brand_name) > 60:  # likely the whole subject line, unparsed
            return False
        if not self.valid_from or not self.valid_until:
            return False
        if self.valid_from > self.valid_until:
            return False
        return True


class GeneratedCode(Base):
    __tablename__ = "generated_codes"

    id = Column(Integer, primary_key=True)
    promotion_id = Column(Integer, ForeignKey("promotions.id"), nullable=False)
    code = Column(Text, nullable=False)
    generated_at = Column(DateTime, default=datetime.datetime.utcnow)

    promotion = relationship("Promotion", back_populates="generated_codes")


class ProcessedEmail(Base):
    """Tracks Gmail message IDs already ingested, to avoid re-processing on each poll."""

    __tablename__ = "processed_emails"

    id = Column(Integer, primary_key=True)
    message_id = Column(String(255), unique=True, nullable=False)
    processed_at = Column(DateTime, default=datetime.datetime.utcnow)
