# tests/test_stripe_billing.py
# Suite pytest couvrant la logique metier de l'integration Stripe (voir billing.py,
# routes_billing.py, models_billing.py) sur la branche feature/stripe-integration.
#
# Portee : ces tests utilisent une base SQLite temporaire et ne font AUCUN appel reseau
# reel a Stripe (stripe.Webhook.construct_event est mocke pour le scenario webhook ;
# les autres scenarios n'appellent jamais l'API Stripe, ils testent uniquement la
# logique d'acces/quota interne a l'application). Ils ne remplacent donc pas un test
# reel de bout en bout en mode Stripe test (voir le protocole manuel fourni a part pour
# la verification du webhook reel sur le déploiement Render).
#
# Execution locale : pip install pytest && pytest tests/ -v
# Execution automatique : voir .github/workflows/tests.yml (declenche a chaque push).

import sys
import uuid
from types import SimpleNamespace
from unittest.mock import patch

import pytest


TEST_ENV = {
    "ADMIN_PASSWORD": "test-admin-password",
    "FLASK_SECRET_KEY": "test-secret-key",
    "STRIPE_SECRET_KEY": "sk_test_dummy",
    "STRIPE_WEBHOOK_SECRET": "whsec_test_dummy",
    "STRIPE_PRICE_ID_ONE_TIME": "price_test_onetime",
    "STRIPE_PRICE_ID_STARTER10": "price_test_starter10",
    "STRIPE_PRICE_ID_UNLIMITED": "price_test_unlimited",
}

APP_MODULES = ["app", "billing", "routes_billing", "models_billing", "email_verification"]


@pytest.fixture()
def app_module(tmp_path, monkeypatch):
    """Importe l'application Flask avec un environnement isole : base SQLite
    temporaire et cles Stripe factices, pour ne jamais toucher a une vraie base
    ni a un vrai compte Stripe pendant les tests."""
    db_path = tmp_path / f"test_{uuid.uuid4().hex}.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    for key, value in TEST_ENV.items():
        monkeypatch.setenv(key, value)

    for mod in APP_MODULES:
        sys.modules.pop(mod, None)

    import app as imported_app

    monkeypatch.setattr(imported_app, "ORDERS_FILE", str(tmp_path / "orders.json"))
    imported_app.app.config["TESTING"] = True
    yield imported_app

    for mod in APP_MODULES:
        sys.modules.pop(mod, None)


@pytest.fixture()
def client(app_module):
    return app_module.app.test_client()


def _make_active_subscriber(app_module, email, plan="starter_10"):
    """Insere directement un client + abonnement actif en base : represente l'etat
    obtenu apres un paiement Stripe reussi, sans dependre d'un appel reseau reel."""
    from models_billing import Customer, Subscription

    with app_module.app.app_context():
        customer = Customer(
            email=email,
            stripe_customer_id=f"cus_test_{uuid.uuid4().hex[:8]}",
        )
        app_module.db.session.add(customer)
        app_module.db.session.commit()

        subscription = Subscription(
            customer_id=customer.id,
            stripe_subscription_id=f"sub_test_{uuid.uuid4().hex[:8]}",
            plan=plan,
            status="active",
        )
        app_module.db.session.add(subscription)
        app_module.db.session.commit()


def _checkout_payload(email):
    return {
        "client": "Client Test",
        "evenement": "Evenement Test",
        "email": email,
        "label1": "Etape 1",
        "date1": "2026-08-01",
        "date_event": "2026-08-15",
    }


class TestWebhookCreatesSubscription:
    """Scenario 1 : un evenement webhook Stripe checkout.session.completed
    (mode=subscription, plan=starter_10) doit creer un abonnement actif en base,
    sans qu'aucun appel reseau reel a Stripe ne soit necessaire (construct_event
    est mocke, seule la logique de traitement applicative est testee ici)."""

    def test_webhook_creates_active_starter10_subscription(self, app_module, client):
        from models_billing import Customer

        email = "abonne.test@example.com"
        fake_event = {
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "customer_email": email,
                    "customer": "cus_test_webhook123",
                    "mode": "subscription",
                    "metadata": {"plan": "starter_10"},
                    "subscription": "sub_test_webhook123",
                }
            },
        }

        with patch("billing.stripe.Webhook.construct_event", return_value=fake_event):
            response = client.post(
                "/webhook/stripe",
                data=b"{}",
                headers={"Stripe-Signature": "t=1,v1=fake_signature_for_test"},
                content_type="application/json",
            )

        assert response.status_code == 200

        with app_module.app.app_context():
            customer = Customer.query.filter_by(email=email).first()
            assert customer is not None
            assert customer.subscription is not None
            assert customer.subscription.plan == "starter_10"
            assert customer.subscription.status == "active"


class TestCheckoutBypassForActiveSubscriber:
    """Scenario 2 : un email actif sur /checkout n'obtient plus le bypass paiement
    immediatement (incident de securite : n'importe qui connaissant l'email d'un
    abonne pouvait consommer gratuitement ses credits) -- il est renvoye vers
    /verifier pour prouver, par un code envoye a cet email, qu'il en est bien
    proprietaire. Ni PayPal ni Stripe a l'acte ne sont impliques dans ce cas."""

    def test_active_subscriber_sent_to_verification_instead_of_direct_bypass(self, app_module, client):
        email = "abonne.actif@example.com"
        _make_active_subscriber(app_module, email, plan="starter_10")

        response = client.post(
            "/checkout", data=_checkout_payload(email), follow_redirects=False
        )

        assert response.status_code in (301, 302, 303)
        location = response.headers["Location"]
        assert "paypal.com" not in location
        assert "/success" not in location
        assert "/verifier" in location


class TestQuotaBlocksEleventhGeneration:
    """Scenario 3 : apres 10 generations comptabilisees dans le mois, la 11e n'est
    plus autorisee en acces direct (quota du plan starter_10 atteint) et /checkout
    repasse alors par un paiement Stripe a l'acte, exactement comme un compte non
    abonne. On mocke stripe.checkout.Session.create pour ne jamais appeler l'API
    Stripe reelle."""

    def test_tenth_allowed_eleventh_blocked_by_quota(self, app_module, client):
        from billing import get_access_status, record_usage

        email = "quota.test@example.com"
        _make_active_subscriber(app_module, email, plan="starter_10")

        with app_module.app.app_context():
            for _ in range(10):
                status = get_access_status(email)
                assert status["allowed"] is True
                record_usage(email)

            status_after_ten = get_access_status(email)
            assert status_after_ten["allowed"] is False
            assert status_after_ten["reason"] == "quota_exceeded"

        fake_session = SimpleNamespace(url="https://checkout.stripe.com/test-session-quota")
        with patch("billing.stripe.checkout.Session.create", return_value=fake_session) as mock_create:
            response = client.post(
                "/checkout", data=_checkout_payload(email), follow_redirects=False
            )

        mock_create.assert_called_once()
        location = response.headers["Location"]
        assert location == fake_session.url
        assert "abonne=1" not in location


class TestNonSubscriberChargedViaStripe:
    """Scenario 4 : un email qui n'a jamais souscrit d'abonnement est redirige vers
    un paiement Stripe a l'acte (mode=payment), sans regression introduite par la
    logique d'acces aux abonnements. On mocke stripe.checkout.Session.create pour
    ne jamais appeler l'API Stripe reelle."""

    def test_non_subscriber_redirected_to_stripe_checkout(self, client):
        email = "jamais.abonne@example.com"

        fake_session = SimpleNamespace(url="https://checkout.stripe.com/test-session-onetime")
        with patch("billing.stripe.checkout.Session.create", return_value=fake_session) as mock_create:
            response = client.post(
                "/checkout", data=_checkout_payload(email), follow_redirects=False
            )

        mock_create.assert_called_once()
        assert response.status_code in (301, 302, 303)
        location = response.headers["Location"]
        assert location == fake_session.url
        assert "abonne=1" not in location

        # Regression : /checkout construit success_url avec un ?token=... deja
        # present ; create_one_time_checkout ne doit pas y accoler un second '?'
        # (sinon Flask lit tout apres le premier '?' comme la valeur du token,
        # cf. bug "Commande introuvable" en prod).
        called_success_url = mock_create.call_args.kwargs["success_url"]
        assert called_success_url.count("?") == 1
        assert "&session_id={CHECKOUT_SESSION_ID}" in called_success_url


class TestSuccessPageInvoiceSourcing:
    """Scenario 5 : /success ne genere plus de facture PDF "maison" (montant en dur).
    Pour un paiement a l'acte reel, la facture affichee doit venir de Stripe
    (hosted_invoice_url) ; pour un abonne en bypass (aucun paiement Stripe pour cette
    generation), on ne doit jamais lui proposer une facture pretendant qu'il a paye
    2,40EUR cette fois-ci -- seulement un lien vers son espace client (/mon-compte)."""

    def test_one_time_payment_shows_real_stripe_invoice_link(self, client):
        import re

        email = "acte.reel@example.com"
        fake_checkout_session = SimpleNamespace(url="https://checkout.stripe.com/test-session-onetime")
        with patch("billing.stripe.checkout.Session.create", return_value=fake_checkout_session) as mock_create:
            client.post("/checkout", data=_checkout_payload(email), follow_redirects=False)

        called_success_url = mock_create.call_args.kwargs["success_url"]
        token = re.search(r"token=([0-9a-f]+)", called_success_url).group(1)
        assert mock_create.call_args.kwargs["metadata"]["token"] == token

        # La session Stripe simulee doit etre payee ET porter le token de CETTE
        # commande dans ses metadata : /success ne se contente plus de la
        # presence d'un session_id, il verifie que le paiement correspond.
        fake_stripe_session = {
            "invoice": "in_test_123",
            "payment_status": "paid",
            "metadata": {"token": token},
        }
        fake_invoice = {"hosted_invoice_url": "https://invoice.stripe.com/i/acct_test/in_test_123"}
        with patch("billing.stripe.checkout.Session.retrieve", return_value=fake_stripe_session), \
             patch("billing.stripe.Invoice.retrieve", return_value=fake_invoice):
            response = client.get(f"/success?token={token}&session_id=cs_test_123")

        body = response.get_data(as_text=True)
        assert "https://invoice.stripe.com/i/acct_test/in_test_123" in body
        assert "Voir mes factures" not in body  # pas le formulaire /mon-compte

    def test_unpaid_or_mismatched_session_blocked(self, client):
        """Regression : /success ne doit jamais generer le PNG sans preuve reelle
        de paiement -- ni pour une session non payee, ni en reutilisant le
        session_id d'un AUTRE paiement reellement paye (le token doit matcher)."""
        import re

        email = "acte.non.paye@example.com"
        fake_checkout_session = SimpleNamespace(url="https://checkout.stripe.com/test-session")
        with patch("billing.stripe.checkout.Session.create", return_value=fake_checkout_session) as mock_create:
            client.post("/checkout", data=_checkout_payload(email), follow_redirects=False)
        called_success_url = mock_create.call_args.kwargs["success_url"]
        token = re.search(r"token=([0-9a-f]+)", called_success_url).group(1)

        # Session non payee
        with patch("billing.stripe.checkout.Session.retrieve",
                    return_value={"payment_status": "unpaid", "metadata": {"token": token}}):
            response = client.get(f"/success?token={token}&session_id=cs_test_unpaid")
        assert response.status_code == 403

        # Session payee mais pour une AUTRE commande (token different) : reutiliser
        # un session_id legitime ne doit pas debloquer une commande differente.
        with patch("billing.stripe.checkout.Session.retrieve",
                    return_value={"payment_status": "paid", "metadata": {"token": "un-autre-token"}}):
            response = client.get(f"/success?token={token}&session_id=cs_test_reused")
        assert response.status_code == 403

    def test_subscriber_bypass_never_shows_fake_invoice(self, app_module, client):
        import re

        import email_verification

        email = "abonne.facture@example.com"
        _make_active_subscriber(app_module, email, plan="starter_10")

        response = client.post("/checkout", data=_checkout_payload(email), follow_redirects=False)
        assert "/verifier" in response.headers["Location"]
        token = re.search(r"token=([0-9a-f]+)", response.headers["Location"]).group(1)

        with patch.object(email_verification, "send_verification_email") as mock_send:
            client.get(f"/verifier?token={token}")
            code = mock_send.call_args[0][1]

        verify_response = client.post(
            "/verifier", data={"token": token, "code": code}, follow_redirects=False
        )
        assert "abonne=1" in verify_response.headers["Location"]

        with patch("billing.stripe.checkout.Session.retrieve") as mock_retrieve:
            success_response = client.get(verify_response.headers["Location"])
            # Aucun appel Stripe pour retrouver une facture : cette generation n'a
            # donne lieu a aucun paiement, ce serait fabriquer un montant errone.
            mock_retrieve.assert_not_called()

        body = success_response.get_data(as_text=True)
        assert "Voir mes factures" in body
        assert 'action="/mon-compte"' in body
        assert email in body  # email pre-rempli dans le formulaire /mon-compte
        assert "2.40" not in body and "2,40" not in body


class TestOrderConfirmationEmail:
    """L'email de confirmation (lien d'acces + instructions, pas seulement la
    facture) doit partir une seule fois, a la generation initiale du PNG --
    ni a chaque rafraichissement de /success, ni sans email connu."""

    def test_one_time_payment_triggers_confirmation_email_once(self, client):
        import re

        email = "confirmation.acte@example.com"
        fake_checkout_session = SimpleNamespace(url="https://checkout.stripe.com/test-session-confirm")
        with patch("billing.stripe.checkout.Session.create", return_value=fake_checkout_session) as mock_create:
            client.post("/checkout", data=_checkout_payload(email), follow_redirects=False)
        called_success_url = mock_create.call_args.kwargs["success_url"]
        token = re.search(r"token=([0-9a-f]+)", called_success_url).group(1)

        fake_stripe_session = {
            "invoice": "in_test_confirm",
            "payment_status": "paid",
            "metadata": {"token": token},
        }
        fake_invoice = {"hosted_invoice_url": "https://invoice.stripe.com/i/acct_test/in_test_confirm"}
        with patch("billing.stripe.checkout.Session.retrieve", return_value=fake_stripe_session), \
             patch("billing.stripe.Invoice.retrieve", return_value=fake_invoice), \
             patch("app.send_order_confirmation_email") as mock_confirm:
            client.get(f"/success?token={token}&session_id=cs_test_confirm")

        mock_confirm.assert_called_once()
        call_kwargs = mock_confirm.call_args
        assert call_kwargs.args[0] == email or call_kwargs.kwargs.get("email") == email
        assert "/download/png/" in call_kwargs.kwargs["download_url"]
        assert call_kwargs.kwargs["invoice_url"] == "https://invoice.stripe.com/i/acct_test/in_test_confirm"

        # Rafraichir /success (retour navigateur, lien enregistre...) ne doit
        # PAS renvoyer un deuxieme email de confirmation.
        with patch("billing.stripe.checkout.Session.retrieve", return_value=fake_stripe_session), \
             patch("billing.stripe.Invoice.retrieve", return_value=fake_invoice), \
             patch("app.send_order_confirmation_email") as mock_confirm_again:
            client.get(f"/success?token={token}&session_id=cs_test_confirm")
        mock_confirm_again.assert_not_called()

    def test_subscriber_bypass_also_triggers_confirmation_email(self, app_module, client):
        import re

        import email_verification

        email = "confirmation.abonne@example.com"
        _make_active_subscriber(app_module, email, plan="starter_10")

        response = client.post("/checkout", data=_checkout_payload(email), follow_redirects=False)
        token = re.search(r"token=([0-9a-f]+)", response.headers["Location"]).group(1)

        with patch.object(email_verification, "send_verification_email") as mock_send:
            client.get(f"/verifier?token={token}")
            code = mock_send.call_args[0][1]

        verify_response = client.post(
            "/verifier", data={"token": token, "code": code}, follow_redirects=False
        )

        with patch("app.send_order_confirmation_email") as mock_confirm:
            client.get(verify_response.headers["Location"])

        mock_confirm.assert_called_once()
        call_kwargs = mock_confirm.call_args
        assert call_kwargs.kwargs["invoice_url"] is None  # aucun paiement Stripe pour cette generation


class TestSubscriberConfirmationQuota:
    @pytest.mark.parametrize("plan", ["starter_10", "illimite"])
    def test_refresh_counts_each_order_only_once(self, app_module, client, plan):
        from urllib.parse import parse_qs, urlparse
        from models_billing import Customer, UsageRecord

        email = "refresh.quota@example.com"
        _make_active_subscriber(app_module, email, plan=plan)

        with patch("app.generate_png", return_value="/tmp/test-retroplanning.png"), \
             patch("app.send_order_confirmation_email") as mock_confirm:
            for expected_count in (1, 2):
                response = client.post("/checkout", data=_checkout_payload(email))
                token = parse_qs(urlparse(response.headers["Location"]).query)["token"][0]
                # Simule une commande dont la verification email a reussi.
                order = app_module.get_order(token)
                order["access_verified"] = True
                app_module.save_order(token, order)

                for _ in range(3):
                    result = client.get(f"/success?token={token}&abonne=1")
                    assert result.status_code == 200
                    with app_module.app.app_context():
                        customer = Customer.query.filter_by(email=email).one()
                        usage = UsageRecord.query.filter_by(customer_id=customer.id).one()
                        assert usage.count == expected_count

            assert mock_confirm.call_count == 2


class TestPaiementSuccesAccountLink:
    """Scenario 6 : la page de confirmation d'abonnement (/paiement/succes) doit
    proposer un lien vers /mon-compte (portail Stripe, resiliation en 1 clic promise
    sur /tarifs) uniquement pour un abonnement, jamais pour un paiement a l'acte."""

    def test_subscription_checkout_shows_manage_subscription_form(self, client):
        fake_session = {
            "mode": "subscription",
            "customer_email": "future.abonne@example.com",
            "customer_details": {"email": "future.abonne@example.com"},
        }
        with patch("billing.stripe.checkout.Session.retrieve", return_value=fake_session):
            response = client.get("/paiement/succes?session_id=cs_test_sub")

        body = response.get_data(as_text=True)
        assert 'action="/mon-compte"' in body
        assert "future.abonne@example.com" in body

    def test_one_time_checkout_hides_manage_subscription_form(self, client):
        fake_session = {"mode": "payment", "customer_email": "acte.rapide@example.com"}
        with patch("billing.stripe.checkout.Session.retrieve", return_value=fake_session):
            response = client.get("/paiement/succes?session_id=cs_test_onetime")

        body = response.get_data(as_text=True)
        assert 'action="/mon-compte"' not in body


class TestMonCompteAccessible:
    """Scenario 7 : /mon-compte est une vraie page (GET), accessible en permanence
    depuis le footer et /tarifs -- pas seulement juste apres un paiement -- pour que
    la promesse "resiliation en 1 clic depuis votre espace client" (/tarifs) soit
    honorable a tout moment, pas uniquement dans les quelques instants qui suivent
    un achat."""

    def test_get_renders_a_real_page(self, client):
        response = client.get("/mon-compte")
        assert response.status_code == 200
        body = response.get_data(as_text=True)
        assert '<form method="POST" action="/mon-compte"' in body

    def test_post_unknown_email_shows_friendly_error_not_raw_json(self, client):
        response = client.post("/mon-compte", data={"email": "inconnu@example.com"})
        assert response.status_code == 404
        assert response.content_type.startswith("text/html")
        assert "Aucun compte abonné trouvé" in response.get_data(as_text=True)

    def test_landing_footer_links_to_mon_compte(self, client):
        response = client.get("/")
        assert 'href="/mon-compte"' in response.get_data(as_text=True)

    def test_tarifs_promise_links_to_mon_compte(self, client):
        response = client.get("/tarifs")
        assert 'href="/mon-compte"' in response.get_data(as_text=True)


class TestNoDuplicateSubscriptionCheckout:
    """Scenario 8 : re-cliquer sur un plan d'abonnement avec un email deja abonne
    (actif) ne doit jamais recreer une session Stripe -- create_subscription_checkout
    cree systematiquement un nouveau Customer + une nouvelle souscription
    independante (customer_creation="always" en mode subscription), donc un second
    abonnement distinct et un double prelevement mensuel si on laisse passer."""

    def test_already_subscribed_email_blocked_before_stripe_call(self, app_module, client):
        email = "deja.abonne@example.com"
        _make_active_subscriber(app_module, email, plan="starter_10")

        with patch("billing.stripe.checkout.Session.create") as mock_create:
            response = client.post(
                "/checkout/abonnement/starter_10", data={"email": email}
            )
            mock_create.assert_not_called()

        assert response.status_code == 409
        body = response.get_data(as_text=True)
        assert email in body
        assert 'action="/mon-compte"' in body

    def test_already_subscribed_blocked_even_for_a_different_plan(self, app_module, client):
        """Un abonne starter_10 qui clique sur Illimite ne doit pas non plus creer
        une deuxieme souscription Stripe : la mise a niveau doit passer par le
        portail client (/mon-compte), qui agit sur l'abonnement existant."""
        email = "veut.upgrade@example.com"
        _make_active_subscriber(app_module, email, plan="starter_10")

        with patch("billing.stripe.checkout.Session.create") as mock_create:
            response = client.post(
                "/checkout/abonnement/illimite", data={"email": email}
            )
            mock_create.assert_not_called()

        assert response.status_code == 409

    def test_new_email_sent_to_verification_before_stripe(self, client):
        """Depuis l'ajout de l'OTP au flux d'abonnement, un email jamais abonne
        n'atteint plus Stripe directement : il doit d'abord prouver posseder
        cet email via /verifier-abonnement (cf. TestSubscriptionCheckoutEmailVerification)."""
        email = "jamais.abonne.subscription@example.com"
        with patch("billing.stripe.checkout.Session.create") as mock_create:
            response = client.post(
                "/checkout/abonnement/starter_10", data={"email": email}, follow_redirects=False
            )
            mock_create.assert_not_called()

        assert response.status_code in (301, 302, 303)
        assert "/verifier-abonnement" in response.headers["Location"]


class TestSubscriberEmailVerification:
    """Scenario 9 : correction de la faille de securite signalee -- un email
    d'abonne seul ne suffit plus a obtenir le bypass paiement sur /checkout, il
    faut prouver la possession de la boite mail via un code a usage unique
    (OTP) envoye par email. Aucun appel reseau reel : send_verification_email
    est mocke, seule la logique applicative (email_verification.py, /verifier)
    est testee ici."""

    def _start_verification(self, app_module, client, email, plan="starter_10"):
        import re

        _make_active_subscriber(app_module, email, plan=plan)
        response = client.post("/checkout", data=_checkout_payload(email), follow_redirects=False)
        assert "/verifier" in response.headers["Location"]
        return re.search(r"token=([0-9a-f]+)", response.headers["Location"]).group(1)

    def test_correct_code_grants_access_and_decrements_quota(self, app_module, client):
        import email_verification
        from billing import current_period_key
        from models_billing import Customer, UsageRecord

        email = "code.correct@example.com"
        token = self._start_verification(app_module, client, email)

        with patch.object(email_verification, "send_verification_email") as mock_send:
            client.get(f"/verifier?token={token}")
            code = mock_send.call_args[0][1]

        response = client.post(
            "/verifier", data={"token": token, "code": code}, follow_redirects=True
        )
        assert response.status_code == 200
        assert "Télécharger mon Rétroplanning" in response.get_data(as_text=True)

        order = app_module.get_order(token)
        assert order["paid"] is True
        assert order["access_verified"] is True

        with app_module.app.app_context():
            customer = Customer.query.filter_by(email=email).first()
            usage = UsageRecord.query.filter_by(
                customer_id=customer.id, period=current_period_key()
            ).first()
            assert usage is not None and usage.count == 1

    def test_wrong_code_rejected(self, app_module, client):
        import email_verification

        email = "code.faux@example.com"
        token = self._start_verification(app_module, client, email)

        with patch.object(email_verification, "send_verification_email"):
            client.get(f"/verifier?token={token}")

        response = client.post("/verifier", data={"token": token, "code": "000000"})
        assert response.status_code == 200
        assert "Code incorrect" in response.get_data(as_text=True)

    def test_expired_code_rejected_then_resend_works(self, app_module, client):
        import email_verification
        from datetime import datetime, timedelta
        from models_billing import EmailVerification

        email = "code.expire@example.com"
        token = self._start_verification(app_module, client, email)

        with patch.object(email_verification, "send_verification_email") as mock_send:
            client.get(f"/verifier?token={token}")
            code = mock_send.call_args[0][1]

        with app_module.app.app_context():
            verification = EmailVerification.query.filter_by(email=email).first()
            verification.expires_at = datetime.utcnow() - timedelta(minutes=1)
            # On recule aussi created_at pour ne pas se heurter au cooldown
            # anti-spam lors de la demande d'un nouveau code juste apres.
            verification.created_at = datetime.utcnow() - timedelta(minutes=5)
            app_module.db.session.commit()

        response = client.post("/verifier", data={"token": token, "code": code})
        assert "expiré" in response.get_data(as_text=True)

        with patch.object(email_verification, "send_verification_email") as mock_send:
            client.post("/verifier", data={"token": token, "action": "renvoyer"})
            new_code = mock_send.call_args[0][1]

        response = client.post(
            "/verifier", data={"token": token, "code": new_code}, follow_redirects=False
        )
        assert "abonne=1" in response.headers["Location"]

    def test_too_many_attempts_blocks_even_the_correct_code(self, app_module, client):
        import email_verification

        email = "trop.tentatives@example.com"
        token = self._start_verification(app_module, client, email)

        with patch.object(email_verification, "send_verification_email") as mock_send:
            client.get(f"/verifier?token={token}")
            code = mock_send.call_args[0][1]

        for _ in range(5):
            client.post("/verifier", data={"token": token, "code": "000000"})

        response = client.post("/verifier", data={"token": token, "code": code})
        assert "Trop de tentatives" in response.get_data(as_text=True)

    def test_remember_me_cookie_skips_verification_next_time(self, app_module, client):
        import email_verification

        email = "navigateur.memorise@example.com"
        token = self._start_verification(app_module, client, email)

        with patch.object(email_verification, "send_verification_email") as mock_send:
            client.get(f"/verifier?token={token}")
            code = mock_send.call_args[0][1]
        client.post("/verifier", data={"token": token, "code": code})

        # Nouvelle commande, meme navigateur (le client de test conserve les cookies)
        response = client.post(
            "/checkout", data=_checkout_payload(email), follow_redirects=False
        )
        assert "/verifier" not in response.headers["Location"]
        assert "abonne=1" in response.headers["Location"]

    def test_non_subscriber_never_triggers_a_verification_code(self, app_module, client):
        """Un email jamais abonne ne doit generer aucun code -- sinon la simple
        creation (ou non) d'un code en base laisserait deviner qui est abonne."""
        from models_billing import EmailVerification

        email = "jamais.abonne.otp@example.com"
        fake_session = SimpleNamespace(url="https://checkout.stripe.com/x")
        with patch("billing.stripe.checkout.Session.create", return_value=fake_session):
            client.post("/checkout", data=_checkout_payload(email), follow_redirects=False)

        with app_module.app.app_context():
            assert EmailVerification.query.filter_by(email=email).count() == 0


class TestMonCompteEmailVerification:
    """Meme mecanisme applique a /mon-compte (portail Stripe : factures, moyen
    de paiement, resiliation) -- un email seul ne doit plus donner acces au
    portail de facturation d'un abonne."""

    def test_email_alone_does_not_grant_portal_access(self, app_module, client):
        email = "portail.protege@example.com"
        _make_active_subscriber(app_module, email)

        with patch("routes_billing.create_customer_portal_session") as mock_portal:
            response = client.post("/mon-compte", data={"email": email})
            mock_portal.assert_not_called()

        assert response.status_code == 200
        assert "code" in response.get_data(as_text=True).lower()

    def test_correct_code_grants_portal_access(self, app_module, client):
        import email_verification

        email = "portail.code.correct@example.com"
        _make_active_subscriber(app_module, email)

        with patch.object(email_verification, "send_verification_email") as mock_send:
            client.post("/mon-compte", data={"email": email})
            code = mock_send.call_args[0][1]

        fake_portal = SimpleNamespace(url="https://billing.stripe.com/session/test")
        with patch("routes_billing.create_customer_portal_session", return_value=fake_portal) as mock_portal:
            response = client.post(
                "/mon-compte", data={"email": email, "code": code}, follow_redirects=False
            )

        mock_portal.assert_called_once()
        assert response.headers["Location"] == fake_portal.url

    def test_cookie_skips_code_on_next_visit(self, app_module, client):
        import email_verification

        email = "portail.memorise@example.com"
        _make_active_subscriber(app_module, email)

        with patch.object(email_verification, "send_verification_email") as mock_send:
            client.post("/mon-compte", data={"email": email})
            code = mock_send.call_args[0][1]

        fake_portal = SimpleNamespace(url="https://billing.stripe.com/session/test")
        with patch("routes_billing.create_customer_portal_session", return_value=fake_portal):
            client.post("/mon-compte", data={"email": email, "code": code}, follow_redirects=False)

        with patch("routes_billing.create_customer_portal_session", return_value=fake_portal) as mock_portal:
            response = client.post("/mon-compte", data={"email": email}, follow_redirects=False)

        mock_portal.assert_called_once()
        assert response.headers["Location"] == fake_portal.url


class TestSubscriptionCheckoutEmailVerification:
    """Un nouvel abonnement (/checkout/abonnement/<plan>) exige desormais lui aussi
    une preuve de possession de l'email (OTP) avant d'envoyer la personne payer sur
    Stripe -- pour ne jamais initier une souscription (et les emails de facturation
    qui suivent) au nom d'une adresse qui n'est pas la sienne. Aucun appel reseau
    reel : send_verification_email et stripe.checkout.Session.create sont mockes."""

    def _start_subscription_verification(self, client, email, plan="starter_10"):
        import re

        response = client.post(
            "/checkout/abonnement/" + plan, data={"email": email}, follow_redirects=False
        )
        assert "/verifier-abonnement" in response.headers["Location"]
        return re.search(r"token=([^&]+)", response.headers["Location"]).group(1)

    def test_correct_code_then_reaches_stripe_and_sets_cookie(self, client):
        import email_verification

        email = "nouvel.abonne.otp@example.com"
        token = self._start_subscription_verification(client, email, plan="starter_10")

        with patch.object(email_verification, "send_verification_email") as mock_send:
            client.get(f"/verifier-abonnement?token={token}")
            code = mock_send.call_args[0][1]

        fake_session = SimpleNamespace(url="https://checkout.stripe.com/test-session-subotp")
        with patch("billing.stripe.checkout.Session.create", return_value=fake_session) as mock_create:
            response = client.post(
                "/verifier-abonnement", data={"token": token, "code": code}, follow_redirects=False
            )

        mock_create.assert_called_once()
        assert mock_create.call_args.kwargs["customer_email"] == email
        assert response.status_code in (301, 302, 303)
        assert response.headers["Location"] == fake_session.url
        assert "verified_email" in response.headers.get("Set-Cookie", "")

        # Meme navigateur, nouvelle souscription : plus besoin de code.
        with patch("billing.stripe.checkout.Session.create", return_value=fake_session) as mock_create_2:
            second_response = client.post(
                "/checkout/abonnement/illimite", data={"email": email}, follow_redirects=False
            )
        mock_create_2.assert_called_once()
        assert "/verifier-abonnement" not in second_response.headers["Location"]

    def test_wrong_code_rejected_before_stripe(self, client):
        import email_verification

        email = "code.faux.abonnement@example.com"
        token = self._start_subscription_verification(client, email)

        with patch.object(email_verification, "send_verification_email"):
            client.get(f"/verifier-abonnement?token={token}")

        with patch("billing.stripe.checkout.Session.create") as mock_create:
            response = client.post("/verifier-abonnement", data={"token": token, "code": "000000"})
            mock_create.assert_not_called()

        assert response.status_code == 401
        assert "Code incorrect" in response.get_data(as_text=True)

    def test_expired_code_rejected_then_resend_works(self, app_module, client):
        import email_verification
        from datetime import datetime, timedelta
        from models_billing import EmailVerification

        email = "code.expire.abonnement@example.com"
        token = self._start_subscription_verification(client, email)

        with patch.object(email_verification, "send_verification_email") as mock_send:
            client.get(f"/verifier-abonnement?token={token}")
            code = mock_send.call_args[0][1]

        with app_module.app.app_context():
            verification = EmailVerification.query.filter_by(email=email).first()
            verification.expires_at = datetime.utcnow() - timedelta(minutes=1)
            verification.created_at = datetime.utcnow() - timedelta(minutes=5)
            app_module.db.session.commit()

        response = client.post("/verifier-abonnement", data={"token": token, "code": code})
        assert "expiré" in response.get_data(as_text=True)

        with patch.object(email_verification, "send_verification_email") as mock_send:
            client.post("/verifier-abonnement", data={"token": token, "action": "renvoyer"})
            new_code = mock_send.call_args[0][1]

        fake_session = SimpleNamespace(url="https://checkout.stripe.com/test-session-resend")
        with patch("billing.stripe.checkout.Session.create", return_value=fake_session):
            response = client.post(
                "/verifier-abonnement", data={"token": token, "code": new_code}, follow_redirects=False
            )
        assert response.headers["Location"] == fake_session.url

    def test_too_many_attempts_blocks_even_the_correct_code(self, client):
        import email_verification

        email = "trop.tentatives.abonnement@example.com"
        token = self._start_subscription_verification(client, email)

        with patch.object(email_verification, "send_verification_email") as mock_send:
            client.get(f"/verifier-abonnement?token={token}")
            code = mock_send.call_args[0][1]

        for _ in range(5):
            client.post("/verifier-abonnement", data={"token": token, "code": "000000"})

        response = client.post("/verifier-abonnement", data={"token": token, "code": code})
        assert "Trop de tentatives" in response.get_data(as_text=True)

    def test_already_active_at_verification_time_blocks_second_subscription(self, app_module, client):
        """TOCTOU : si l'email devient abonne actif pendant la saisie du code (deux
        onglets, double soumission...), la seconde souscription Stripe ne doit
        jamais etre creee -- meme controle qu'a l'entree du flux."""
        import email_verification

        email = "devient.abonne.pendant.otp@example.com"
        token = self._start_subscription_verification(client, email)

        with patch.object(email_verification, "send_verification_email") as mock_send:
            client.get(f"/verifier-abonnement?token={token}")
            code = mock_send.call_args[0][1]

        _make_active_subscriber(app_module, email, plan="starter_10")

        with patch("billing.stripe.checkout.Session.create") as mock_create:
            response = client.post("/verifier-abonnement", data={"token": token, "code": code})
            mock_create.assert_not_called()

        assert response.status_code == 409

    def test_invalid_or_missing_token_redirects_to_tarifs(self, client):
        response = client.get("/verifier-abonnement?token=invalide", follow_redirects=False)
        assert response.headers["Location"].endswith("/tarifs")

        response = client.get("/verifier-abonnement", follow_redirects=False)
        assert response.headers["Location"].endswith("/tarifs")

    def test_non_subscriber_form_never_reaches_stripe_without_code(self, client):
        """Regression du scenario signale : cliquer sur 'S'abonner' avec un simple
        email ne doit plus atteindre Stripe sans passer par /verifier-abonnement."""
        email = "direct.stripe.sans.otp@example.com"
        with patch("billing.stripe.checkout.Session.create") as mock_create:
            response = client.post(
                "/checkout/abonnement/starter_10", data={"email": email}, follow_redirects=False
            )
            mock_create.assert_not_called()
        assert "checkout.stripe.com" not in response.headers["Location"]


class TestSitemapModificationDates:
    def test_static_pages_do_not_claim_a_daily_modification(self, client):
        import xml.etree.ElementTree as ET

        response = client.get('/sitemap.xml')
        assert response.status_code == 200
        root = ET.fromstring(response.data)
        ns = {'s': 'http://www.sitemaps.org/schemas/sitemap/0.9'}
        assert len(root.findall('s:url', ns)) == 7
        assert root.findall('s:url/s:lastmod', ns) == []

    def test_published_article_uses_its_actual_update_date(self, app_module, client):
        from datetime import datetime
        import xml.etree.ElementTree as ET

        with app_module.app.app_context():
            for slug, status in [('article-publie', 'publie'), ('brouillon', 'brouillon')]:
                article = app_module.Article(
                    titre='Article test', slug=slug, extrait='Extrait', contenu='Contenu',
                    statut=status, published_at=datetime(2026, 8, 1),
                    updated_at=datetime(2026, 8, 15, 10, 30),
                )
                app_module.db.session.add(article)
            app_module.db.session.commit()

        response = client.get('/sitemap.xml')
        root = ET.fromstring(response.data)
        ns = {'s': 'http://www.sitemaps.org/schemas/sitemap/0.9'}
        entries = {node.find('s:loc', ns).text: node for node in root.findall('s:url', ns)}
        published = entries[app_module.SITE_URL + '/blog/article-publie']
        assert published.find('s:lastmod', ns).text == '2026-08-15'
        assert app_module.SITE_URL + '/blog/brouillon' not in entries
