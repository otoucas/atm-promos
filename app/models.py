import datetime
from itertools import zip_longest

from sqlalchemy import Boolean, Column, DateTime, Date, ForeignKey, Integer, String, Text
from sqlalchemy.orm import relationship

from .database import Base

STATUS_PENDING = "pending"
STATUS_ACTIVE = "active"
STATUS_ARCHIVED = "archived"

SOURCE_EMAIL = "email"
SOURCE_MANUAL = "manual"
SOURCE_MCP = "mcp"

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
    # le temps (voir config.PASSWORD_RESET_TOKEN_VALIDITY_DAYS).
    password_hash = Column(String(255), nullable=True)
    password_reset_token = Column(String(64), nullable=True, unique=True, index=True)
    password_reset_requested_at = Column(DateTime, nullable=True)

    # Accès MCP (serveur séparé, voir mcp_server/) : le point de vente connecte
    # son propre Claude avec ce jeton pour lire/soumettre ses promotions —
    # "leur IA, pas la nôtre", c'est une mise à disposition gracieuse, pas un
    # service consommant nos propres crédits API. mcp_auto_publish choisit,
    # par magasin, si une soumission part directement active ou doit être
    # validée manuellement (comme les mails Artemare aujourd'hui).
    mcp_token = Column(String(64), nullable=True, unique=True, index=True)
    mcp_auto_publish = Column(Boolean, nullable=False, default=False)

    # Rappels par email avant le début/la fin d'une campagne, paramétrables par
    # point de vente (/{code}/admin/notifications) — demande Olivier du
    # 2026-07-14. notification_email est distinct de contact_email (qui sert à
    # la connexion) : un magasin sans contact_email (Artemare) peut quand même
    # activer les rappels avec une adresse de son choix.
    notifications_enabled = Column(Boolean, nullable=False, default=False)
    notification_email = Column(String(255), nullable=True)
    notify_days_before_start = Column(Integer, nullable=False, default=3)
    notify_days_before_end = Column(Integer, nullable=False, default=3)

    promotions = relationship("Promotion", back_populates="store")
    mcp_activity_logs = relationship(
        "McpActivityLog", back_populates="store", cascade="all, delete-orphan"
    )


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

    # Marque le moment d'envoi d'un rappel (voir jobs.run_promo_notifications)
    # pour ne jamais en renvoyer deux fois pour la même échéance.
    start_reminder_sent_at = Column(DateTime, nullable=True)
    end_reminder_sent_at = Column(DateTime, nullable=True)

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
    def product_rows(self) -> list[tuple[str, str]]:
        """Une ligne (nom de produit, EAN) par produit couvert par cette
        promotion — pour la vue tableau opérateur, où chaque EAN doit pouvoir
        être vérifié individuellement plutôt que noyé dans une seule ligne
        par promotion (demande Olivier du 2026-07-13). `concerned_products`
        et `product_codes` sont deux champs texte saisis à la main,
        virgule-séparés, en principe dans le même ordre — on les aligne du
        mieux possible s'ils ne correspondent pas exactement en nombre."""
        names = [p.strip() for p in (self.concerned_products or "").split(",") if p.strip()]
        eans = self.product_codes_list
        if not names and not eans:
            return [("", "")]
        if not eans:
            return [(n, "") for n in names]
        if not names:
            return [("", e) for e in eans]
        if len(names) == len(eans):
            return list(zip(names, eans))
        if len(names) == 1:
            return [(names[0], e) for e in eans]
        if len(eans) == 1:
            return [(n, eans[0]) for n in names]
        return [(n, e) for n, e in zip_longest(names, eans, fillvalue="")]

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


class Brand(Base):
    """Registre coopératif des marques : un logo choisi une fois par un
    superadmin (page /superadmin/brands) et rattaché par nom à toutes les
    promotions correspondantes, dans tous les points de vente — évite que
    chaque pharmacie corrige le même logo mal détecté une à une (demande
    Olivier du 2026-07-14). Priorité d'affichage sur une promotion donnée :
    logo_path propre à la promo (upload manuel du point de vente) > logo de
    marque ci-dessous > logo_url auto-détecté (Clearbit, best-effort)."""

    __tablename__ = "brands"

    id = Column(Integer, primary_key=True)
    name = Column(String(200), unique=True, nullable=False, index=True)
    logo_path = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)


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


MCP_ACTION_LIST = "list_promotions"
MCP_ACTION_SUBMIT = "submit_promotion"


class McpActivityLog(Base):
    """Journal des appels MCP (lectures/soumissions) — visible et supprimable
    par le point de vente lui-même dans ses réglages (/{code}/admin/mcp),
    demande explicite : chaque magasin garde la main sur ce que son IA a fait."""

    __tablename__ = "mcp_activity_logs"

    id = Column(Integer, primary_key=True)
    store_id = Column(Integer, ForeignKey("stores.id"), nullable=False)
    action = Column(String(30), nullable=False)  # MCP_ACTION_LIST | MCP_ACTION_SUBMIT
    detail = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    store = relationship("Store", back_populates="mcp_activity_logs")


class StoreRequestLog(Base):
    """Trace de CHAQUE demande d'ouverture de point de vente (/hello public ou
    /superadmin/stores/new), quel que soit le résultat — créée, rejetée ou
    signalée comme étonnante. Jamais modifiée ni supprimée après coup, y
    compris si le point de vente correspondant est ensuite désactivé/supprimé
    en base : c'est la seule trace durable qui survit à la disparition d'un
    Store (incident du 2026-07-12 où des comptes créés puis effacés n'avaient
    laissé aucune trace du formulaire d'origine)."""

    __tablename__ = "store_request_logs"

    id = Column(Integer, primary_key=True)
    submitted_at = Column(DateTime, default=datetime.datetime.utcnow, nullable=False)
    source = Column(String(20), nullable=False)  # "public" (/hello) ou "superadmin"
    name = Column(String(200), nullable=False)
    code = Column(String(10), nullable=False)  # tel que soumis (pas forcément valide)
    contact_name = Column(String(200), nullable=True)
    contact_email = Column(String(255), nullable=True)
    outcome = Column(String(30), nullable=False)
    directory_flags = Column(Text, nullable=True)  # une raison "étonnante" par ligne, NULL si RAS
    store_id = Column(Integer, ForeignKey("stores.id"), nullable=True)


class PasswordResetLog(Base):
    """Trace discrète de CHAQUE tentative de réinitialisation de mot de passe
    (/{code}/admin/forgot-password) — y compris quand l'email saisi ne
    correspond pas au contact enregistré. Le message affiché au magasin reste
    volontairement générique dans tous les cas (anti-énumération, voir
    forgot_password_submit) ; cette table est la seule façon de diagnostiquer
    après coup un échec silencieux (incident du 2026-07-15 où une demande
    n'aboutissait à rien sans qu'on sache pourquoi)."""

    __tablename__ = "password_reset_logs"

    id = Column(Integer, primary_key=True)
    submitted_at = Column(DateTime, default=datetime.datetime.utcnow, nullable=False)
    store_id = Column(Integer, ForeignKey("stores.id"), nullable=False)
    submitted_email = Column(String(255), nullable=False)
    matched = Column(Boolean, nullable=False)

    store = relationship("Store")
