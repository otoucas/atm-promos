"""Covers the multi-store rollout: each store only sees its own promotions,
"standalone" (dépannage) stores have no password on their own admin pages
while the "erpnext" store (Artemare) keeps the historical login, code
generation is rate-limited, and the superadmin space can create/list stores.
"""

from fastapi.testclient import TestClient

from app import config, highco, main
from app.auth import hash_password
from app.models import INTEGRATION_ERPNEXT, INTEGRATION_STANDALONE, STATUS_ACTIVE, GeneratedCode, Promotion, Store


def _client(db):
    def override_get_db():
        yield db

    main.app.dependency_overrides[main.get_db] = override_get_db
    return TestClient(main.app)


def _make_store(db, code, integration=INTEGRATION_STANDALONE, name=None):
    # commit (pas juste flush) : sinon la transaction reste ouverte et bloque
    # (SQLite "database is locked") la session séparée que le démarrage de
    # l'app utilise pour créer le magasin par défaut (voir init_db()).
    store = Store(code=code, name=name or f"Point de vente {code}", integration=integration)
    db.add(store)
    db.commit()
    return store


def test_default_store_created_on_startup(db):
    with _client(db):
        pass
    store = db.query(Store).filter(Store.code == config.DEFAULT_STORE_CODE).first()
    assert store is not None
    assert store.integration == INTEGRATION_ERPNEXT
    main.app.dependency_overrides.clear()


def test_each_store_grid_only_shows_its_own_promotions(db):
    store_a = _make_store(db, "LYO")
    store_b = _make_store(db, "GRE")
    db.add(Promotion(store_id=store_a.id, brand_name="Fixodent", highco_reference="ref-a", status=STATUS_ACTIVE))
    db.add(Promotion(store_id=store_b.id, brand_name="Compeed", highco_reference="ref-b", status=STATUS_ACTIVE))
    db.commit()

    client = _client(db)
    try:
        with client as c:
            resp_a = c.get("/LYO/")
            resp_b = c.get("/GRE/")
        assert "Fixodent" in resp_a.text
        assert "Compeed" not in resp_a.text
        assert "Compeed" in resp_b.text
        assert "Fixodent" not in resp_b.text
    finally:
        main.app.dependency_overrides.clear()


def test_grid_tiles_expose_product_and_ean_data_for_client_side_search(db):
    store = _make_store(db, "LYO")
    db.add(Promotion(
        store_id=store.id,
        brand_name="Fixodent",
        highco_reference="ref-a",
        status=STATUS_ACTIVE,
        concerned_products="Fixodent crème adhésive",
        product_codes="3401560123456, 3401560123457",
    ))
    db.commit()

    client = _client(db)
    try:
        with client as c:
            resp = c.get("/LYO/")
        assert 'id="promo-search"' in resp.text
        assert 'data-products="fixodent crème adhésive"' in resp.text
        assert 'data-eans="3401560123456,3401560123457"' in resp.text
    finally:
        main.app.dependency_overrides.clear()


def test_table_view_has_one_row_per_product_with_sortable_columns(db):
    """Retour Olivier du 2026-07-13 : la vue tableau doit permettre de
    vérifier chaque EAN individuellement (une ligne par produit, pas par
    promotion), avec la marque et l'offre dans des colonnes séparées, et
    des colonnes triables au clic (voir Promotion.product_rows)."""
    store = _make_store(db, "LYO")
    db.add(Promotion(
        store_id=store.id,
        brand_name="Fixodent",
        operation_label="-30%",
        highco_reference="ref-a",
        status=STATUS_ACTIVE,
        concerned_products="Crème adhésive, Poudre",
        product_codes="3401560123456, 3401560123457",
    ))
    db.commit()

    client = _client(db)
    try:
        with client as c:
            resp = c.get("/LYO/")
        assert resp.text.count('class="table-row') == 2
        assert "data-sortable" in resp.text
        assert "<td>Fixodent</td>" in resp.text
        assert "<td>-30%</td>" in resp.text
        assert "<td>Crème adhésive</td>" in resp.text
        assert "<td>Poudre</td>" in resp.text
        assert "<td>3401560123456</td>" in resp.text
        assert "<td>3401560123457</td>" in resp.text
    finally:
        main.app.dependency_overrides.clear()


def test_unknown_store_code_returns_404(db):
    client = _client(db)
    try:
        with client as c:
            resp = c.get("/ZZZ/")
        assert resp.status_code == 404
    finally:
        main.app.dependency_overrides.clear()


def test_standalone_store_admin_requires_login(db):
    """Depuis le 2026-07-10 (suite), les points de vente en dépannage ont eux
    aussi un compte (email + mot de passe) — décision d'Olivier qui remplace
    l'accès sans authentification prévu initialement."""
    store = _make_store(db, "LYO")
    client = _client(db)
    try:
        with client as c:
            resp = c.get("/LYO/admin/pending", follow_redirects=False)
        assert resp.status_code == 307
        assert resp.headers["location"] == "/LYO/admin/login"
    finally:
        main.app.dependency_overrides.clear()


def test_erpnext_store_admin_requires_login(db):
    client = _client(db)
    try:
        with client as c:
            resp = c.get(f"/{config.DEFAULT_STORE_CODE}/admin/pending", follow_redirects=False)
        assert resp.status_code == 307
        assert resp.headers["location"] == f"/{config.DEFAULT_STORE_CODE}/admin/login"
    finally:
        main.app.dependency_overrides.clear()


def test_standalone_store_new_promotion_form_requires_login(db):
    store = _make_store(db, "LYO")
    client = _client(db)
    try:
        with client as c:
            resp = c.get("/LYO/admin/promotions/new", follow_redirects=False)
        assert resp.status_code == 307
    finally:
        main.app.dependency_overrides.clear()


def test_manual_promotion_creation_saves_products_and_eans(db):
    """Retour Olivier du 2026-07-13 (suite) : avant, le formulaire de saisie
    manuelle ne proposait aucun champ produits/EAN, et rien ne permettait de
    les ajouter après coup pour une promotion créée à la main (le champ
    concerned_products n'était éditable que via la file d'attente, que ces
    promotions ne traversent jamais)."""
    store = _make_store(db, "LYO")
    store.contact_email = "contact@hellopharmacie.com"
    store.password_hash = hash_password("un-bon-mot-de-passe")
    db.commit()

    client = _client(db)
    try:
        with client as c:
            c.post("/LYO/admin/login", data={"email": store.contact_email, "password": "un-bon-mot-de-passe"})
            resp = c.post(
                "/LYO/admin/promotions/new",
                data={
                    "brand_name": "Fixodent",
                    "highco_reference": "https://example.com/ref",
                    "concerned_products": "Crème adhésive, Poudre",
                    "product_codes": "3401560123456, 3401560123457",
                },
                follow_redirects=False,
            )
        assert resp.status_code == 303
        promo = db.query(Promotion).filter(Promotion.store_id == store.id).one()
        assert promo.concerned_products == "Crème adhésive, Poudre"
        assert promo.product_codes == "3401560123456, 3401560123457"
    finally:
        main.app.dependency_overrides.clear()


def test_edit_products_form_prefills_and_saves(db):
    store = _make_store(db, "LYO")
    store.contact_email = "contact@hellopharmacie.com"
    store.password_hash = hash_password("un-bon-mot-de-passe")
    promo = Promotion(
        store_id=store.id, brand_name="Fixodent", highco_reference="ref-a", status=STATUS_ACTIVE,
        concerned_products="Crème adhésive", product_codes="3401560123456",
    )
    db.add(promo)
    db.commit()
    db.refresh(promo)

    client = _client(db)
    try:
        with client as c:
            c.post("/LYO/admin/login", data={"email": store.contact_email, "password": "un-bon-mot-de-passe"})
            form_resp = c.get(f"/LYO/admin/promotions/{promo.id}/products")
            assert form_resp.status_code == 200
            # Les valeurs initiales sont injectées côté client via JSON (le champ
            # est rempli par JS, pas rendu côté serveur) — vérifie la présence du
            # JSON encodé plutôt que le texte affiché.
            assert "Cr\\u00e8me adh\\u00e9sive" in form_resp.text
            assert "3401560123456" in form_resp.text

            save_resp = c.post(
                f"/LYO/admin/promotions/{promo.id}/products",
                data={"concerned_products": "Poudre", "product_codes": "3401560999999"},
                follow_redirects=False,
            )
        assert save_resp.status_code == 303
        db.refresh(promo)
        assert promo.concerned_products == "Poudre"
        assert promo.product_codes == "3401560999999"
    finally:
        main.app.dependency_overrides.clear()


def test_generate_code_rate_limited_after_threshold(db, monkeypatch):
    monkeypatch.setattr(config, "CODE_GENERATION_RATE_LIMIT_COUNT", 2)
    monkeypatch.setattr(highco, "generate_code", lambda ref: "FAKE-CODE")
    store = _make_store(db, "LYO")
    promo = Promotion(store_id=store.id, brand_name="Fixodent", highco_reference="fake-ref", status=STATUS_ACTIVE)
    db.add(promo)
    db.commit()
    db.refresh(promo)

    client = _client(db)
    try:
        with client as c:
            r1 = c.post(f"/LYO/generate/{promo.id}", headers={"X-Requested-With": "fetch"})
            r2 = c.post(f"/LYO/generate/{promo.id}", headers={"X-Requested-With": "fetch"})
            r3 = c.post(f"/LYO/generate/{promo.id}", headers={"X-Requested-With": "fetch"})
        assert "FAKE-CODE" in r1.text
        assert "FAKE-CODE" in r2.text
        assert "FAKE-CODE" not in r3.text
        assert "Trop de codes générés récemment" in r3.text
    finally:
        main.app.dependency_overrides.clear()


def test_ean_export_lists_one_row_per_ean(db):
    """Nouvel export dédié au LGO (retour Olivier du 2026-07-13) : une ligne
    par EAN, distinct de l'export Winpharma existant qui reste une ligne par
    promotion avec les codes regroupés."""
    store = _make_store(db, "LYO", name="Pharmacie de Lyon")
    store.contact_email = "contact@hellopharmacie.com"
    store.password_hash = hash_password("un-bon-mot-de-passe")
    db.add(Promotion(
        store_id=store.id,
        brand_name="Fixodent",
        highco_reference="ref-a",
        status=STATUS_ACTIVE,
        product_codes="3401560123456, 3401560123457",
    ))
    db.commit()

    client = _client(db)
    try:
        with client as c:
            c.post("/LYO/admin/login", data={"email": store.contact_email, "password": "un-bon-mot-de-passe"})
            resp = c.get("/LYO/admin/export/eans.csv")
        assert resp.status_code == 200
        rows = [r for r in resp.text.strip().splitlines()]
        assert rows[0] == "ean;marque;produit;valide_du;valide_au"
        assert len(rows) == 3  # en-tête + 2 EAN
        assert any(row.startswith("3401560123456;Fixodent") for row in rows)
        assert any(row.startswith("3401560123457;Fixodent") for row in rows)
    finally:
        main.app.dependency_overrides.clear()


def test_generate_code_scoped_to_its_own_store(db, monkeypatch):
    """A promotion belonging to one store must not be generatable through
    another store's URL, even by guessing the numeric id."""
    monkeypatch.setattr(highco, "generate_code", lambda ref: "FAKE-CODE")
    store_a = _make_store(db, "LYO")
    store_b = _make_store(db, "GRE")
    promo = Promotion(store_id=store_a.id, brand_name="Fixodent", highco_reference="fake-ref", status=STATUS_ACTIVE)
    db.add(promo)
    db.commit()
    db.refresh(promo)

    client = _client(db)
    try:
        with client as c:
            resp = c.post(f"/GRE/generate/{promo.id}", headers={"X-Requested-With": "fetch"})
        assert resp.status_code == 404
    finally:
        main.app.dependency_overrides.clear()


def test_superadmin_dashboard_requires_login(db):
    client = _client(db)
    try:
        with client as c:
            resp = c.get("/superadmin", follow_redirects=False)
        assert resp.status_code == 307
        assert resp.headers["location"] == "/superadmin/login"
    finally:
        main.app.dependency_overrides.clear()


def _new_store_form(name, code, contact_name="Jean Dupont", email_local_part="jdupont"):
    return {"name": name, "code": code, "contact_name": contact_name, "email_local_part": email_local_part}


def test_superadmin_can_create_store(db, monkeypatch):
    sent = []
    monkeypatch.setattr(main, "send_verification_email", lambda store: sent.append(store.code) or True)
    client = _client(db)
    try:
        with client as c:
            c.post("/superadmin/login", data={"password": config.ADMIN_PASSWORD})
            resp = c.post(
                "/superadmin/stores/new",
                data=_new_store_form("Pharmacie de Lyon", "lyo", email_local_part="jdupont"),
                follow_redirects=False,
            )
        assert resp.status_code == 303
        store = db.query(Store).filter(Store.code == "LYO").first()
        assert store is not None
        assert store.name == "Pharmacie de Lyon"
        assert store.integration == INTEGRATION_STANDALONE
        assert store.contact_email == f"jdupont@{config.STORE_CONTACT_EMAIL_DOMAIN}"
        assert store.contact_name == "Jean Dupont"
        # Inactif tant que le lien de confirmation n'a pas été cliqué.
        assert store.is_active is False
        assert store.verification_token
        assert sent == ["LYO"]
    finally:
        main.app.dependency_overrides.clear()


def test_store_request_is_logged_even_when_rejected(db, monkeypatch):
    from app.models import StoreRequestLog

    monkeypatch.setattr(main, "send_verification_email", lambda store: True)
    client = _client(db)
    try:
        with client as c:
            c.post("/superadmin/login", data={"password": config.ADMIN_PASSWORD})
            c.post("/superadmin/stores/new", data=_new_store_form("Pharmacie de Lyon", "LYO"))
            # Même sigle redemandé : rejeté, mais doit quand même laisser une trace.
            c.post("/superadmin/stores/new", data=_new_store_form("Autre nom", "LYO", email_local_part="autre"))
            # Soumission publique.
            c.post("/hello", data=_new_store_form("Pharmacie de Grenoble", "GRE", email_local_part="ggrenoble"))

        logs = db.query(StoreRequestLog).order_by(StoreRequestLog.id).all()
        assert [log.outcome for log in logs] == ["created", "rejected_duplicate_code", "created"]
        assert [log.source for log in logs] == ["superadmin", "superadmin", "public"]
        assert logs[0].store_id is not None
        # La trace du sigle LYO reste même si le point de vente est ensuite désactivé.
        store = db.query(Store).filter(Store.code == "LYO").first()
        store.is_active = False
        db.commit()
        still_there = db.query(StoreRequestLog).filter(StoreRequestLog.code == "LYO").all()
        assert len(still_there) == 2
    finally:
        main.app.dependency_overrides.clear()


def test_suspicious_request_flags_mismatch_with_directory_and_alerts(db, monkeypatch, tmp_path):
    from app.models import StoreRequestLog

    csv_path = tmp_path / "contacts_sigles.csv"
    csv_path.write_text(
        "Sigle;Nom;Mail Pharmacie (Clients / Laboratoires);Mail Titulaire 1;Mail Titulaire 2;Mail Titulaire 3;"
        "Mail Commandes;Mail Administratif\n"
        "LYO;Pharmacie de Lyon;lyon@hellopharmacie.com;;;;;administratif.lyon@hellopharmacie.com\n",
        encoding="iso-8859-1",
    )
    monkeypatch.setattr(config, "CONTACTS_DIRECTORY_CSV_PATH", str(csv_path))
    monkeypatch.setattr(main, "send_verification_email", lambda store: True)
    alerts = []
    monkeypatch.setattr(main, "send_suspicious_request_alert", lambda code, name, email, reasons: alerts.append((code, reasons)) or True)
    client = _client(db)
    try:
        with client as c:
            c.post("/superadmin/login", data={"password": config.ADMIN_PASSWORD})
            # Email de contact ne correspond à rien de connu pour LYO dans le fichier.
            c.post("/superadmin/stores/new", data=_new_store_form("Pharmacie de Lyon", "LYO", email_local_part="inconnu"))
            # Sigle absent du fichier — dépannage légitime, mais signalé quand même.
            c.post("/superadmin/stores/new", data=_new_store_form("Pharmacie de Grenoble", "GRE", email_local_part="ggrenoble"))

        assert len(alerts) == 2
        assert alerts[0][0] == "LYO"
        assert "ne correspond à aucune adresse connue" in alerts[0][1][0]
        assert alerts[1][0] == "GRE"
        assert "n'apparaît pas dans le fichier groupement" in alerts[1][1][0]

        logs = db.query(StoreRequestLog).order_by(StoreRequestLog.id).all()
        assert all(log.directory_flags for log in logs)
    finally:
        main.app.dependency_overrides.clear()


def test_matching_request_does_not_trigger_alert(db, monkeypatch, tmp_path):
    csv_path = tmp_path / "contacts_sigles.csv"
    csv_path.write_text(
        "Sigle;Nom;Mail Pharmacie (Clients / Laboratoires);Mail Titulaire 1;Mail Titulaire 2;Mail Titulaire 3;"
        "Mail Commandes;Mail Administratif\n"
        "LYO;Pharmacie de Lyon;lyon@hellopharmacie.com;;;;;administratif.lyon@hellopharmacie.com\n",
        encoding="iso-8859-1",
    )
    monkeypatch.setattr(config, "CONTACTS_DIRECTORY_CSV_PATH", str(csv_path))
    monkeypatch.setattr(main, "send_verification_email", lambda store: True)
    alerts = []
    monkeypatch.setattr(main, "send_suspicious_request_alert", lambda *a: alerts.append(a) or True)
    client = _client(db)
    try:
        with client as c:
            c.post("/superadmin/login", data={"password": config.ADMIN_PASSWORD})
            c.post("/superadmin/stores/new", data=_new_store_form("Pharmacie de Lyon", "LYO", email_local_part="administratif.lyon"))
        assert alerts == []
    finally:
        main.app.dependency_overrides.clear()


def test_superadmin_store_requests_page_lists_history(db, monkeypatch):
    monkeypatch.setattr(main, "send_verification_email", lambda store: True)
    client = _client(db)
    try:
        with client as c:
            c.post("/superadmin/login", data={"password": config.ADMIN_PASSWORD})
            c.post("/superadmin/stores/new", data=_new_store_form("Pharmacie de Lyon", "LYO"))
            resp = c.get("/superadmin/store-requests")
        assert resp.status_code == 200
        assert "LYO" in resp.text
    finally:
        main.app.dependency_overrides.clear()


def test_new_store_grid_unreachable_until_email_verified(db, monkeypatch):
    monkeypatch.setattr(main, "send_verification_email", lambda store: True)
    client = _client(db)
    try:
        with client as c:
            c.post("/superadmin/login", data={"password": config.ADMIN_PASSWORD})
            c.post("/superadmin/stores/new", data=_new_store_form("Pharmacie de Lyon", "LYO"))
            resp = c.get("/LYO/")
        assert resp.status_code == 404
    finally:
        main.app.dependency_overrides.clear()


def test_verify_link_shows_password_form_then_activates_and_logs_in(db, monkeypatch):
    monkeypatch.setattr(main, "send_verification_email", lambda store: True)
    client = _client(db)
    try:
        with client as c:
            c.post("/superadmin/login", data={"password": config.ADMIN_PASSWORD})
            c.post("/superadmin/stores/new", data=_new_store_form("Pharmacie de Lyon", "LYO"))
            store = db.query(Store).filter(Store.code == "LYO").first()

            form_resp = c.get(f"/verify/{store.verification_token}")
            assert form_resp.status_code == 200
            assert store.password_hash is None  # pas encore choisi tant que le formulaire n'est pas soumis

            submit_resp = c.post(
                f"/verify/{store.verification_token}",
                data={"password": "un-bon-mot-de-passe", "password_confirm": "un-bon-mot-de-passe"},
                follow_redirects=False,
            )
            assert submit_resp.status_code == 303

            # Auto-connecté après confirmation : la page admin est directement accessible.
            admin_resp = c.get("/LYO/admin/pending")
            grid_resp = c.get("/LYO/")
        db.refresh(store)
        assert store.is_active is True
        assert store.email_verified_at is not None
        assert store.password_hash is not None
        assert admin_resp.status_code == 200
        assert grid_resp.status_code == 200
    finally:
        main.app.dependency_overrides.clear()


def test_verify_rejects_mismatched_passwords(db, monkeypatch):
    monkeypatch.setattr(main, "send_verification_email", lambda store: True)
    client = _client(db)
    try:
        with client as c:
            c.post("/superadmin/login", data={"password": config.ADMIN_PASSWORD})
            c.post("/superadmin/stores/new", data=_new_store_form("Pharmacie de Lyon", "LYO"))
            store = db.query(Store).filter(Store.code == "LYO").first()
            resp = c.post(
                f"/verify/{store.verification_token}",
                data={"password": "abcdefgh", "password_confirm": "different"},
            )
        assert resp.status_code == 400
        assert "ne correspondent pas" in resp.text
    finally:
        main.app.dependency_overrides.clear()


def test_reset_password_link_rejects_expired_token(db, monkeypatch):
    import datetime

    monkeypatch.setattr(main, "send_verification_email", lambda store: True)
    monkeypatch.setattr(main, "send_password_reset_email", lambda store: True)
    client = _client(db)
    try:
        with client as c:
            c.post("/superadmin/login", data={"password": config.ADMIN_PASSWORD})
            c.post("/superadmin/stores/new", data=_new_store_form("Pharmacie de Lyon", "LYO"))
            store = db.query(Store).filter(Store.code == "LYO").first()
            c.post(f"/verify/{store.verification_token}", data={"password": "ancien-mdp-1234", "password_confirm": "ancien-mdp-1234"})
            c.post("/LYO/admin/logout")
            c.post("/LYO/admin/forgot-password", data={"email": store.contact_email})

        db.refresh(store)
        store.password_reset_requested_at = datetime.datetime.utcnow() - datetime.timedelta(
            days=config.PASSWORD_RESET_TOKEN_VALIDITY_DAYS, hours=1
        )
        db.commit()

        with client as c:
            resp = c.get(f"/LYO/admin/reset-password/{store.password_reset_token}")
        assert resp.status_code in (400, 404)
    finally:
        main.app.dependency_overrides.clear()


def test_verify_link_rejects_expired_token(db, monkeypatch):
    import datetime

    monkeypatch.setattr(main, "send_verification_email", lambda store: True)
    client = _client(db)
    try:
        with client as c:
            c.post("/superadmin/login", data={"password": config.ADMIN_PASSWORD})
            c.post("/superadmin/stores/new", data=_new_store_form("Pharmacie de Lyon", "LYO"))
            store = db.query(Store).filter(Store.code == "LYO").first()
            store.created_at = datetime.datetime.utcnow() - datetime.timedelta(
                days=config.STORE_VERIFICATION_TOKEN_VALIDITY_DAYS, hours=1
            )
            db.commit()

            resp = c.get(f"/verify/{store.verification_token}")
        assert resp.status_code == 404
        assert "expiré" in resp.text
    finally:
        main.app.dependency_overrides.clear()


def test_verify_link_rejects_unknown_or_reused_token(db, monkeypatch):
    monkeypatch.setattr(main, "send_verification_email", lambda store: True)
    client = _client(db)
    try:
        with client as c:
            resp = c.get("/verify/does-not-exist")
            assert resp.status_code == 404

            c.post("/superadmin/login", data={"password": config.ADMIN_PASSWORD})
            c.post("/superadmin/stores/new", data=_new_store_form("Pharmacie de Lyon", "LYO"))
            store = db.query(Store).filter(Store.code == "LYO").first()
            token = store.verification_token
            c.post(f"/verify/{token}", data={"password": "abcdefgh", "password_confirm": "abcdefgh"})
            reuse_resp = c.get(f"/verify/{token}")
        assert reuse_resp.status_code == 404
    finally:
        main.app.dependency_overrides.clear()


def test_login_with_email_and_password(db, monkeypatch):
    monkeypatch.setattr(main, "send_verification_email", lambda store: True)
    client = _client(db)
    try:
        with client as c:
            c.post("/superadmin/login", data={"password": config.ADMIN_PASSWORD})
            c.post("/superadmin/stores/new", data=_new_store_form("Pharmacie de Lyon", "LYO", email_local_part="jdupont"))
            store = db.query(Store).filter(Store.code == "LYO").first()
            c.post(f"/verify/{store.verification_token}", data={"password": "un-bon-mot-de-passe", "password_confirm": "un-bon-mot-de-passe"})
            c.post("/LYO/admin/logout")

            wrong_resp = c.post(
                "/LYO/admin/login",
                data={"email": store.contact_email, "password": "mauvais-mot-de-passe"},
                follow_redirects=False,
            )
            assert wrong_resp.status_code == 401

            still_locked_out = c.get("/LYO/admin/pending", follow_redirects=False)
            assert still_locked_out.status_code == 307

            ok_resp = c.post(
                "/LYO/admin/login",
                data={"email": store.contact_email, "password": "un-bon-mot-de-passe"},
                follow_redirects=False,
            )
            assert ok_resp.status_code == 303
            admin_resp = c.get("/LYO/admin/pending")
        assert admin_resp.status_code == 200
    finally:
        main.app.dependency_overrides.clear()


def test_forgot_password_resets_and_logs_in(db, monkeypatch):
    monkeypatch.setattr(main, "send_verification_email", lambda store: True)
    reset_emails_sent = []
    monkeypatch.setattr(main, "send_password_reset_email", lambda store: reset_emails_sent.append(store.code) or True)
    client = _client(db)
    try:
        with client as c:
            c.post("/superadmin/login", data={"password": config.ADMIN_PASSWORD})
            c.post("/superadmin/stores/new", data=_new_store_form("Pharmacie de Lyon", "LYO"))
            store = db.query(Store).filter(Store.code == "LYO").first()
            c.post(f"/verify/{store.verification_token}", data={"password": "ancien-mdp-1234", "password_confirm": "ancien-mdp-1234"})
            c.post("/LYO/admin/logout")

            # Une adresse qui ne correspond pas ne doit rien révéler et ne rien envoyer.
            c.post("/LYO/admin/forgot-password", data={"email": "quelquun-dautre@hellopharmacie.com"})
            assert reset_emails_sent == []

            forgot_resp = c.post("/LYO/admin/forgot-password", data={"email": store.contact_email})
            assert forgot_resp.status_code == 200
            assert reset_emails_sent == ["LYO"]

        db.refresh(store)
        reset_token = store.password_reset_token
        assert reset_token

        with client as c:
            reset_submit = c.post(
                f"/LYO/admin/reset-password/{reset_token}",
                data={"password": "nouveau-mdp-5678", "password_confirm": "nouveau-mdp-5678"},
                follow_redirects=False,
            )
            assert reset_submit.status_code == 303
            admin_resp = c.get("/LYO/admin/pending")
        assert admin_resp.status_code == 200

        with client as c:
            c.post("/LYO/admin/logout")
            old_pw_resp = c.post("/LYO/admin/login", data={"email": store.contact_email, "password": "ancien-mdp-1234"})
            assert old_pw_resp.status_code == 401
            new_pw_resp = c.post(
                "/LYO/admin/login", data={"email": store.contact_email, "password": "nouveau-mdp-5678"}, follow_redirects=False
            )
            assert new_pw_resp.status_code == 303
    finally:
        main.app.dependency_overrides.clear()


def test_superadmin_rejects_duplicate_code_and_sends_alert(db, monkeypatch):
    alerts = []
    monkeypatch.setattr(main, "send_verification_email", lambda store: True)
    monkeypatch.setattr(
        main, "send_duplicate_code_alert", lambda code, existing, email: alerts.append((code, email)) or True
    )
    _make_store(db, "LYO")
    client = _client(db)
    try:
        with client as c:
            c.post("/superadmin/login", data={"password": config.ADMIN_PASSWORD})
            resp = c.post("/superadmin/stores/new", data=_new_store_form("Doublon", "LYO", email_local_part="autre"))
        assert resp.status_code == 400
        assert "déjà utilisé" in resp.text
        assert alerts == [("LYO", f"autre@{config.STORE_CONTACT_EMAIL_DOMAIN}")]
    finally:
        main.app.dependency_overrides.clear()


def test_superadmin_rejects_code_not_three_letters(db):
    client = _client(db)
    try:
        with client as c:
            c.post("/superadmin/login", data={"password": config.ADMIN_PASSWORD})
            resp = c.post("/superadmin/stores/new", data=_new_store_form("Trop long", "LYON"))
        assert resp.status_code == 400
        assert "3 lettres" in resp.text
    finally:
        main.app.dependency_overrides.clear()


def test_superadmin_rejects_same_email_for_two_stores(db, monkeypatch):
    monkeypatch.setattr(main, "send_verification_email", lambda store: True)
    client = _client(db)
    try:
        with client as c:
            c.post("/superadmin/login", data={"password": config.ADMIN_PASSWORD})
            c.post("/superadmin/stores/new", data=_new_store_form("Pharmacie A", "AAA", email_local_part="meme"))
            resp = c.post("/superadmin/stores/new", data=_new_store_form("Pharmacie B", "BBB", email_local_part="meme"))
        assert resp.status_code == 400
        assert "un seul sigle" in resp.text
        assert db.query(Store).filter(Store.code == "BBB").first() is None
    finally:
        main.app.dependency_overrides.clear()


def test_public_gateway_header_allows_erpnext_store_grid(db):
    """Depuis le 2026-07-10 (suite), la grille d'Artemare est volontairement
    publique (sous le sigle ATM) pour rejoindre le format des autres points
    de vente du groupement."""
    client = _client(db)
    try:
        with client as c:
            resp = c.get(f"/{config.DEFAULT_STORE_CODE}/", headers={"X-Nifty-Public-Gateway": "1"})
        assert resp.status_code == 200
    finally:
        main.app.dependency_overrides.clear()


def test_public_gateway_header_requires_login_for_erpnext_store_admin(db):
    """Revirement du 2026-07-10 (soir) : le lien "Paramètres" affiché sur la
    grille publique d'Artemare renvoyait sur une page inexistante (404 brut),
    la passerelle publique bloquant tout /ART/admin/* sans distinction —
    repéré par Olivier en cliquant dessus depuis l'adresse publique. Décision
    ("même système pour tous garantit qu'une erreur observée soit corrigée
    chez tous") : Artemare est désormais protégée par son propre compte
    email + mot de passe, comme n'importe quel autre point de vente — plus
    de blocage spécifique "erpnext" au niveau de la passerelle publique,
    juste une exigence de connexion comme pour tous les magasins."""
    client = _client(db)
    try:
        with client as c:
            resp = c.get(
                f"/{config.DEFAULT_STORE_CODE}/admin/pending", headers={"X-Nifty-Public-Gateway": "1"}, follow_redirects=False
            )
        assert resp.status_code == 307
        assert resp.headers["location"] == f"/{config.DEFAULT_STORE_CODE}/admin/login"
    finally:
        main.app.dependency_overrides.clear()


def test_public_gateway_root_shows_store_index(db):
    """Depuis le 2026-07-10 (suite), la racine publique (atm.hellopharmacie.com/nifty/)
    affiche un index des points de vente actifs plutôt que d'être bloquée."""
    store = _make_store(db, "LYO", name="Pharmacie de Lyon")
    client = _client(db)
    try:
        with client as c:
            resp = c.get("/", headers={"X-Nifty-Public-Gateway": "1"})
        assert resp.status_code == 200
        assert ">LYO<" in resp.text
    finally:
        main.app.dependency_overrides.clear()


def test_public_gateway_store_index_sorted_case_insensitively(db):
    """Un nom commençant par une minuscule (ex. "de l'Éclair") doit se
    classer avec les autres, pas systématiquement en dernier — régression
    d'un tri SQL sensible à la casse repéré en corrigeant l'affichage de
    cette page (retour Olivier du 2026-07-13)."""
    _make_store(db, "ECL", name="Pharmacie de l'Éclair")
    _make_store(db, "BEL", name="Pharmacie Bellevue")
    client = _client(db)
    try:
        with client as c:
            resp = c.get("/", headers={"X-Nifty-Public-Gateway": "1"})
        assert resp.status_code == 200
        assert resp.text.index(">BEL<") < resp.text.index(">ECL<")
    finally:
        main.app.dependency_overrides.clear()


def test_direct_tailscale_root_still_shows_default_store_grid(db):
    """L'accès direct (sans passerelle) garde le comportement historique :
    "/" affiche directement la grille d'Artemare, pas l'index."""
    client = _client(db)
    try:
        with client as c:
            resp = c.get("/")
        assert resp.status_code == 200
        assert "store-index-grid" not in resp.text
    finally:
        main.app.dependency_overrides.clear()


def test_code_without_trailing_slash_redirects_with_mount_prefix(db):
    """Régression du 2026-07-10 : Starlette redirigeait automatiquement
    "/atm" -> "/atm/" sans tenir compte du préfixe /nifty retiré par le
    reverse proxy, renvoyant vers la racine du domaine (l'ERPNext)."""
    store = _make_store(db, "LYO")
    client = _client(db)
    try:
        with client as c:
            resp = c.get("/LYO", headers={"X-Forwarded-Prefix": "/nifty"}, follow_redirects=False)
        assert resp.status_code == 307
        assert resp.headers["location"] == "/nifty/LYO/"
    finally:
        main.app.dependency_overrides.clear()


def test_public_gateway_header_blocks_superadmin(db):
    client = _client(db)
    try:
        with client as c:
            resp = c.get("/superadmin", headers={"X-Nifty-Public-Gateway": "1"}, follow_redirects=False)
        assert resp.status_code == 404
    finally:
        main.app.dependency_overrides.clear()


def test_public_gateway_header_allows_standalone_store(db):
    store = _make_store(db, "LYO")
    client = _client(db)
    try:
        with client as c:
            resp = c.get("/LYO/", headers={"X-Nifty-Public-Gateway": "1"})
        assert resp.status_code == 200
    finally:
        main.app.dependency_overrides.clear()


def test_mount_prefix_from_forwarded_header_applied_to_static_and_logo_links(db):
    """X-Forwarded-Prefix (set by the nginx /nifty/ location) must be
    reflected in absolute /static and /media/logos links, since the reverse
    proxy strips that prefix before forwarding — otherwise the browser would
    request assets at the wrong path and get a 404."""
    store = _make_store(db, "LYO")
    db.add(Promotion(store_id=store.id, brand_name="Fixodent", highco_reference="ref", status=STATUS_ACTIVE, logo_path="fixodent.png"))
    db.commit()

    client = _client(db)
    try:
        with client as c:
            resp = c.get("/LYO/", headers={"X-Forwarded-Prefix": "/nifty"})
        assert "/nifty/static/style.css" in resp.text
        assert "/nifty/media/logos/fixodent.png" in resp.text
    finally:
        main.app.dependency_overrides.clear()


def test_mount_prefix_applied_to_generate_form_action_and_login_redirects(db):
    """Régression du 2026-07-10 : seuls /static et /media/logos étaient
    préfixés au départ, pas les liens propres au magasin (génération de code,
    redirection après connexion...) — servi sous atm.hellopharmacie.com/nifty/,
    ces liens pointaient par erreur vers la racine du domaine (donc vers
    l'ERPNext) au lieu de repasser par /nifty/."""
    store = _make_store(db, "LYO")
    db.add(Promotion(store_id=store.id, brand_name="Fixodent", highco_reference="ref", status=STATUS_ACTIVE))
    db.commit()

    client = _client(db)
    try:
        with client as c:
            grid_resp = c.get("/LYO/", headers={"X-Forwarded-Prefix": "/nifty"})
            assert 'action="/nifty/LYO/generate/' in grid_resp.text

            login_redirect = c.get("/LYO/admin/pending", headers={"X-Forwarded-Prefix": "/nifty"}, follow_redirects=False)
            assert login_redirect.headers["location"] == "/nifty/LYO/admin/login"
    finally:
        main.app.dependency_overrides.clear()


def test_erpnext_history_never_shows_other_store_codes(db, monkeypatch):
    """Regression guard for the join in admin_history: history must stay
    scoped per store even though GeneratedCode has no store_id of its own."""
    monkeypatch.setattr(highco, "generate_code", lambda ref: "SECRET-CODE-FOR-OTHER-STORE")
    store_a = _make_store(db, "LYO")
    store_b = _make_store(db, "GRE")
    promo_a = Promotion(store_id=store_a.id, brand_name="Fixodent", highco_reference="ref-a", status=STATUS_ACTIVE)
    db.add(promo_a)
    db.commit()
    db.refresh(promo_a)

    client = _client(db)
    try:
        with client as c:
            c.post(f"/LYO/generate/{promo_a.id}", headers={"X-Requested-With": "fetch"})
            resp = c.get("/GRE/admin/history")
        assert "SECRET-CODE-FOR-OTHER-STORE" not in resp.text
    finally:
        main.app.dependency_overrides.clear()


def test_page_title_matches_header_on_settings_pages(db):
    """Régression du 2026-07-10 (soir) : le <title> de l'onglet restait
    toujours "NIFTY by HighCo — {magasin} — Promotions en cours" même sur
    les pages de réglages (connexion, MCP...), qui affichent un tout autre
    titre en <h1> — repéré par Olivier ("la page des paramètres n'a pas
    l'air d'être celle attendue"). Le <title> doit reprendre le même texte
    que le <h1> affiché, page par page."""
    store = _make_store(db, "LYO")
    client = _client(db)
    try:
        with client as c:
            login_resp = c.get("/LYO/admin/login")
        assert "<title>Paramètres</title>" in login_resp.text
        assert "<h1>Paramètres</h1>" in login_resp.text
    finally:
        main.app.dependency_overrides.clear()
