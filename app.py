from flask import Flask, render_template, request, send_file, redirect, session, url_for, abort
from markupsafe import Markup
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch
from datetime import datetime
from flask_sqlalchemy import SQLAlchemy
import os
import uuid
import json
import re
import unicodedata
import urllib.parse

app = Flask(__name__)

CONTACT_EMAIL = "contact@retroplanning.eu"
PRIX_HT = 2.00
PRIX_TTC = round(PRIX_HT * 1.20, 2)
SITE_URL = "https://www.retroplanning.eu"

# Numero commercial affiche aux visiteurs (hero + footer de la landing) : standard
# d'accueil dedie au site, distinct du numero legal d'Omnipub ci-dessous. A ne PAS
# utiliser dans les mentions legales / le schema LocalBusiness, qui doivent rester
# sur les coordonnees de l'entite juridique (voir ENTREPRISE).
CONTACT_PHONE_DISPLAY = "01 59 48 00 94"
CONTACT_PHONE_TEL = "+33159480094"

# Coordonnees publiques de l'editeur (page /a-propos, footer, balisage LocalBusiness).
ENTREPRISE = {
    "nom": "Omnipub",
    "adresse": "Parc Mermoz - 199 rue Helene Boucher",
    "cp": "34170",
    "ville": "Castelnau-le-Lez",
    "region": "Occitanie",
    "pays": "FR",
    "telephone": "+33499136333",
    "telephone_affichage": "04 99 13 63 33",
    "siret": "432 764 785 00023",
}

# Profils reseaux sociaux officiels de la marque (footer + schema Organization/
# LocalBusiness "sameAs"). Renseigner une URL ici suffit a la propager partout ;
# laisser a None tant que le profil n'existe pas encore.
SOCIAL_LINKS = {
    "linkedin": "https://www.linkedin.com/company/137853902/",
    "instagram": "https://www.instagram.com/retroplanning/",
    "facebook": None,
    "x": None,
    "youtube": None,
}

ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD")
if not ADMIN_PASSWORD:
    raise RuntimeError("La variable d'environnement ADMIN_PASSWORD doit etre definie")

app.secret_key = os.environ.get("FLASK_SECRET_KEY")
if not app.secret_key:
    raise RuntimeError("La variable d'environnement FLASK_SECRET_KEY doit etre definie")
GA_ID = "G-NW27CME5X7"

database_url = os.environ.get("DATABASE_URL", "sqlite:////tmp/retroplanning_blog.db")
if database_url.startswith("postgres://"):
    database_url = database_url.replace("postgres://", "postgresql://", 1)

app.config["SQLALCHEMY_DATABASE_URI"] = database_url
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
db = SQLAlchemy(app)

# --- Modeles sectoriels de retroplanning : voir retroplanning-templates/INTEGRATION.md ---
from templates_secteurs import get_template, list_templates  # noqa: E402
from routes_templates import templates_bp  # noqa: E402
app.register_blueprint(templates_bp)
# --- Stripe billing (facturation) : voir retroplanning-stripe/INTEGRATION.md ---
# db est deja defini juste au-dessus : on importe les modeles ici (pour db.create_all())
# et on enregistre le blueprint des routes de facturation Stripe.
from models_billing import Customer, Subscription, UsageRecord, OneTimePayment, EmailVerification  # noqa: E402,F401
from routes_billing import billing_bp  # noqa: E402
app.register_blueprint(billing_bp)
# Acces direct au generateur pour un email d'abonne actif (bypass paiement) :
# voir routes_billing.check_access_or_redirect / billing.get_access_status / billing.record_usage
from routes_billing import check_access_or_redirect # noqa: E402
from billing import record_usage, create_one_time_checkout, get_checkout_session, get_invoice_url_for_session, _sget # noqa: E402
from routes_billing import SITE_URL as BILLING_SITE_URL # noqa: E402
# Verification qu'un email saisi est bien possede par la personne (OTP) : voir
# email_verification.py -- protege le bypass paiement abonne sur /checkout.
from email_verification import (  # noqa: E402
    create_and_send_code,
    verify_code,
    cookie_confirms_email,
    set_verified_email_cookie,
)
# Email de confirmation apres generation reussie (lien d'acces + instructions,
# pas seulement la facture) : voir notifications.py.
from notifications import send_order_confirmation_email  # noqa: E402

ORDERS_FILE = "/tmp/orders.json"

HOME_FAQS = [
    {
        "question": "Comment faire un rétroplanning événementiel efficace ?",
        "answer": "Un bon rétroplanning part de la date de l'événement et remonte le temps. Définissez les jalons clés afin de visualiser les dates limites."
    },
    {
        "question": "Qu'est-ce qu'un rétroplanning ?",
        "answer": "Un rétroplanning est une méthode de planification qui part de la date finale du projet pour identifier les étapes nécessaires."
    },
    {
        "question": "À qui s'adresse le générateur de rétroplanning ?",
        "answer": "Il est conçu pour les agences, organisateurs d'événements, imprimeurs et chefs de projet."
    },
    {
        "question": "Combien de jalons puis-je ajouter ?",
        "answer": "Le générateur permet de renseigner jusqu'à six étapes clés avant la date finale."
    },
    {
        "question": "Puis-je modifier les couleurs du graphique ?",
        "answer": "Oui. Vous choisissez une couleur principale et une couleur secondaire."
    },
    {
        "question": "Puis-je utiliser le rétroplanning pour un autre projet ?",
        "answer": "Oui. Il convient aussi aux projets de production, impression, lancement et logistique."
    },
    {
        "question": "Le fichier est-il modifiable après téléchargement ?",
        "answer": "Le téléchargement est un PNG HD prêt à intégrer dans vos documents. Les données sont modifiables avant export."
    },
    {
        "question": "Vais-je recevoir une facture ?",
        "answer": "Oui. Stripe génère automatiquement votre facture au moment du paiement ; vous y accédez depuis la page de confirmation ou, pour un abonnement, depuis votre espace client."
    },
    {
        "question": "Quelles sont les formules disponibles ?",
        "answer": "Paiement à l'acte à 2€ HT, abonnement Starter à 9,90€ HT/mois (10 rétroplannings), ou abonnement Illimité à 19,90€ HT/mois. Des modèles préétablis par secteur d'activité sont aussi disponibles pour démarrer plus vite."
    }
]


class Article(db.Model):
    __tablename__ = "articles"

    id = db.Column(db.Integer, primary_key=True)
    titre = db.Column(db.String(220), nullable=False)
    slug = db.Column(db.String(240), nullable=False, unique=True, index=True)
    categorie = db.Column(db.String(80), nullable=False, default="Guides")
    extrait = db.Column(db.Text, nullable=False)
    resume = db.Column(db.Text, default="")
    contenu = db.Column(db.Text, nullable=False)
    points_cles = db.Column(db.Text, default="")
    image_url = db.Column(db.String(1000), default="")
    image_alt = db.Column(db.String(300), default="")
    meta_title = db.Column(db.String(70), default="")
    meta_description = db.Column(db.String(170), default="")
    faq_json = db.Column(db.Text, default="[]")
    statut = db.Column(db.String(20), nullable=False, default="brouillon")
    published_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(
        db.DateTime,
        nullable=False,
        default=datetime.utcnow,
        onupdate=datetime.utcnow
    )

    @property
    def faqs(self):
        try:
            return json.loads(self.faq_json or "[]")
        except json.JSONDecodeError:
            return []

    @property
    def category_slug(self):
        return slugify(self.categorie)

    @property
    def reading_minutes(self):
        return max(1, round(len((self.contenu or "").split()) / 220))


def _obfuscate_email(email):
    # Encode chaque caractere en entite HTML numerique : illisible pour un
    # grattage regex naif sur le HTML brut, mais decode normalement par les
    # navigateurs, les crawlers et les LLM (contrairement a une obfuscation
    # 100% JS, qui rend l'adresse invisible sans execution de script).
    return Markup("".join(f"&#{ord(c)};" for c in email))


@app.context_processor
def inject_globals():
    contact_user, _, contact_domain = CONTACT_EMAIL.partition("@")
    return {
        "ga_id": GA_ID,
        "contact_email": CONTACT_EMAIL,
        "contact_user": contact_user,
        "contact_domain": contact_domain,
        "contact_email_obfuscated": _obfuscate_email(CONTACT_EMAIL),
        "contact_phone_display": CONTACT_PHONE_DISPLAY,
        "contact_phone_tel": CONTACT_PHONE_TEL,
        "site_url": SITE_URL,
        "entreprise": ENTREPRISE,
        "social_links": SOCIAL_LINKS,
        "social_links_sameas": [url for url in SOCIAL_LINKS.values() if url],
    }


@app.template_filter("content_blocks")
def content_blocks(content):
    blocks = []
    paragraph = []

    def flush():
        nonlocal paragraph
        if paragraph:
            blocks.append(("p", " ".join(paragraph)))
            paragraph = []

    for line in (content or "").splitlines():
        line = line.strip()
        if not line:
            flush()
        elif line.startswith("### "):
            flush()
            blocks.append(("h3", line[4:]))
        elif line.startswith("## "):
            flush()
            blocks.append(("h2", line[3:]))
        elif line.startswith("- "):
            flush()
            blocks.append(("li", line[2:]))
        else:
            paragraph.append(line)

    flush()
    return blocks


def slugify(value):
    text = unicodedata.normalize("NFKD", value or "")
    text = text.encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text.lower()).strip("-")
    return text or "article"


def unique_slug(title, article_id=None):
    base = slugify(title)
    candidate = base
    number = 2

    while True:
        existing = Article.query.filter_by(slug=candidate).first()
        if not existing or existing.id == article_id:
            return candidate
        candidate = f"{base}-{number}"
        number += 1


def is_admin():
    return bool(session.get("admin"))


def load_orders():
    if not os.path.exists(ORDERS_FILE):
        return {}
    with open(ORDERS_FILE, "r", encoding="utf-8") as file:
        return json.load(file)


def save_order(token, data):
    orders = load_orders()
    orders[token] = data
    with open(ORDERS_FILE, "w", encoding="utf-8") as file:
        json.dump(orders, file)


def get_order(token):
    return load_orders().get(token)


def get_steps_from_form(form):
    steps = []
    for index in range(1, 5):
        label = form.get(f"label{index}", "").strip()
        date_value = form.get(f"date{index}", "").strip()
        if label and date_value:
            try:
                steps.append({
                    "label": label,
                    "date": datetime.strptime(date_value, "%Y-%m-%d")
                })
            except ValueError:
                pass

    event_date = form.get("date_event", "").strip()
    if event_date:
        try:
            steps.append({
                "label": "ÉVÉNEMENT",
                "date": datetime.strptime(event_date, "%Y-%m-%d")
            })
        except ValueError:
            pass

    return sorted(steps, key=lambda item: item["date"])


def generate_png(nom_client, nom_evenement, steps, phone, email, web, footer_societe, c_main="#3F8078", c_alt="#75A097"):
    fig = plt.figure(figsize=(16, 8))
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_xlim(0, 16)
    ax.set_ylim(0, 8)
    ax.axis("off")

    ax.add_patch(plt.Rectangle((0, 6.5), 16, 1.5, color=c_main))
    ax.text(8, 7.5, "RETROPLANNING DE PRODUCTION", ha="center", va="center", fontsize=20, color="white", fontweight="bold")
    ax.text(8, 7.0, f"{nom_evenement} | {nom_client}", ha="center", va="center", fontsize=14, color="white")

    start_date = steps[0]["date"]
    end_date = steps[-1]["date"]
    total_days = max((end_date - start_date).days, 1)

    def get_x(date_value):
        return 1 + 14 * ((date_value - start_date).days / total_days)

    ax.add_patch(FancyBboxPatch((1, 3.8), 14, 0.4, boxstyle="round,pad=0.1", color=c_main, alpha=0.2))

    for index in range(len(steps) - 1):
        x0 = get_x(steps[index]["date"])
        x1 = get_x(steps[index + 1]["date"])
        color = c_main if index % 2 == 0 else c_alt
        ax.add_patch(plt.Rectangle((x0, 3.8), x1 - x0, 0.4, linewidth=0, facecolor=color, zorder=3))

    for index, step in enumerate(steps):
        x = get_x(step["date"])
        top = index % 2 == 0
        y = 5.2 if top else 2.8
        ax.scatter(x, 4, s=200, color=c_main, zorder=5)
        ax.plot([x, x], [4, y], color=c_main, linestyle="--", alpha=0.5)
        ax.text(x, y + (0.2 if top else -0.2), step["label"], ha="center", va="bottom" if top else "top", fontsize=10, fontweight="bold", color="#1A1A1A")
        ax.text(
            x, y + (0.6 if top else -0.6),
            step["date"].strftime("%d/%m/%Y"),
            ha="center", va="center", color="white", fontweight="bold",
            bbox={"boxstyle": "round,pad=0.3", "facecolor": c_main, "edgecolor": "none"}
        )

    footer = " | ".join(item for item in [footer_societe, phone, email, web] if item) or nom_client
    ax.text(8, 0.5, footer, ha="center", va="center", fontsize=11, color="#1A1A1A", fontweight="bold")

    filename = f"retroplanning_{nom_client.replace(' ', '_')}_{uuid.uuid4().hex[:6]}.png"
    filepath = os.path.join("/tmp", filename)
    plt.savefig(filepath, dpi=200, bbox_inches="tight")
    plt.close()
    return filepath


@app.route("/")
def landing():
    return render_template(
        "landing.html", faqs=HOME_FAQS, contact_email=CONTACT_EMAIL, secteurs=list_templates()
    )


@app.route("/a-propos")
def a_propos():
    return render_template("a_propos.html")


@app.route("/ressources")
def ressources():
    return render_template("ressources.html")


@app.route("/retroplanning-evenementiel")
def retroplanning_evenementiel():
    return render_template("retroplanning_evenementiel.html")


@app.route("/formulaire")
def index():
    modele_slug = request.args.get("modele")
    etapes_initiales = None
    if modele_slug:
        modele = get_template(modele_slug)
        if modele:
            # Le formulaire gere jusqu'a 6 etapes intermediaires + date evenement.
            # On exclut "Lancement du projet" (1ere etape) et "Jour J" (derniere etape),
            # deja couverts par les champs client/evenement/date_event. 6 est le nombre
            # maximum d'etapes intermediaires parmi les 8 modeles (mariage en compte 6).
            etapes_initiales = modele["etapes"][1:-1][:6]
    return render_template(
        "index.html",
        prix=f"{PRIX_TTC:.2f}",
        contact_email=CONTACT_EMAIL,
        etapes_initiales=etapes_initiales,
    )


@app.route("/checkout", methods=["POST"])
def checkout():
    token = uuid.uuid4().hex
    steps_raw = []

    for index in range(1, 7):
        label = request.form.get(f"label{index}", "").strip()
        date_value = request.form.get(f"date{index}", "").strip()
        if label and date_value:
            steps_raw.append((label, date_value))

    order = {
        "token": token,
        "paid": False,
        "access_verified": False,
        "nom_client": request.form.get("client", ""),
        "nom_evenement": request.form.get("evenement", ""),
        "phone": request.form.get("phone", ""),
        "email": request.form.get("email", ""),
        "web": request.form.get("web", ""),
        "footer_societe": request.form.get("footer_societe", ""),
        "c_main": request.form.get("c_main", "#3F8078"),
        "c_alt": request.form.get("c_alt", "#75A097"),
        "steps_raw": steps_raw,
        "date_event": request.form.get("date_event", ""),
    }
    save_order(token, order)

    # --- Stripe billing : un email d'abonne actif genere sans repasser par un
    # paiement -- mais seulement une fois prouve que cette personne possede bien
    # cet email (code envoye par email), sinon n'importe qui connaissant
    # l'adresse d'un abonne consommerait gratuitement ses credits.
    email = order.get("email", "").strip()
    if email and check_access_or_redirect(email) is None:
        if cookie_confirms_email(email):
            order["access_verified"] = True
            save_order(token, order)
            return redirect(url_for("success", token=token, abonne="1"))
        return redirect(url_for("verifier", token=token))

    session = create_one_time_checkout(
        email=email,
        success_url=BILLING_SITE_URL + f"/success?token={token}",
        cancel_url=BILLING_SITE_URL + "/cancel",
        extra_metadata={"token": token},
    )
    return redirect(session.url, code=303)


@app.route("/verifier", methods=["GET", "POST"])
def verifier():
    token = request.values.get("token", "")
    order = get_order(token)
    if not order or not order.get("email"):
        return "Commande introuvable.", 404

    email = order["email"].strip()

    if request.method == "GET" or request.form.get("action") == "renvoyer":
        status = create_and_send_code(email)
        return render_template("verifier.html", token=token, email=email, error=None, status=status)

    ok, reason = verify_code(email, request.form.get("code", ""))
    if not ok:
        messages = {
            "wrong_code": "Code incorrect.",
            "expired": "Ce code a expiré, demandez-en un nouveau.",
            "too_many_attempts": "Trop de tentatives avec ce code, demandez-en un nouveau.",
            "no_pending_code": "Aucun code en attente, demandez-en un nouveau.",
        }
        return render_template(
            "verifier.html", token=token, email=email,
            error=messages.get(reason, "Erreur de vérification."), status=None,
        )

    order["access_verified"] = True
    save_order(token, order)
    response = redirect(url_for("success", token=token, abonne="1"))
    set_verified_email_cookie(response, email)
    return response


@app.route("/success")
def success():
    token = request.args.get("token", "")
    order = get_order(token)
    if not order:
        return "Commande introuvable.", 404

    is_subscriber = request.args.get("abonne") == "1"
    invoice_url = None

    if is_subscriber:
        # L'acces abonne n'est jamais accorde sur la seule foi du parametre
        # d'URL : il doit avoir ete positionne par /verifier (code confirme)
        # ou par le cookie "navigateur deja verifie" repris dans /checkout.
        if not order.get("access_verified"):
            return "Accès non autorisé.", 403
    else:
        # Paiement a l'acte : on verifie reellement aupres de Stripe que CETTE
        # session a ete payee, et qu'elle correspond bien a CETTE commande (le
        # token est dans les metadata Stripe) -- pas de confiance dans l'URL,
        # jamais de generation sans paiement confirme.
        session_obj = get_checkout_session(request.args.get("session_id", ""))
        payment_status = _sget(session_obj, "payment_status") if session_obj else None
        session_token = _sget(_sget(session_obj, "metadata") or {}, "token") if session_obj else None
        if payment_status != "paid" or session_token != token:
            return "Accès non autorisé.", 403
        order["access_verified"] = True
        invoice_url = get_invoice_url_for_session(session_obj)

    # "paid" est deja vrai si la personne revisite /success (retour navigateur,
    # rafraichissement, lien enregistre) : on s'en sert pour n'envoyer l'email
    # de confirmation qu'une seule fois, a la generation initiale.
    already_confirmed = order.get("paid", False)
    order["paid"] = True
    steps = []

    for label, date_value in order.get("steps_raw", []):
        try:
            steps.append({"label": label, "date": datetime.strptime(date_value, "%Y-%m-%d")})
        except ValueError:
            pass

    if order.get("date_event"):
        try:
            steps.append({"label": "ÉVÉNEMENT", "date": datetime.strptime(order["date_event"], "%Y-%m-%d")})
        except ValueError:
            pass

    steps.sort(key=lambda item: item["date"])
    order["png_path"] = generate_png(
        order["nom_client"], order["nom_evenement"], steps,
        order.get("phone", ""), order.get("email", ""),
        order.get("web", ""), order.get("footer_societe", ""),
        order.get("c_main", "#3F8078"), order.get("c_alt", "#75A097")
    )
    save_order(token, order)

    # --- Stripe billing : decompte le quota mensuel si la generation vient d'un abonnement ---
    if is_subscriber and order.get("email"):
        record_usage(order["email"])

    # Email de confirmation avec lien d'acces + instructions (pas seulement la
    # facture) -- envoye une seule fois, a la generation initiale du PNG.
    if not already_confirmed:
        send_order_confirmation_email(
            order.get("email"),
            download_url=SITE_URL + url_for("download_png", token=token),
            invoice_url=invoice_url,
        )

    return render_template(
        "success.html", token=token, order=order, contact_email=CONTACT_EMAIL,
        is_subscriber=is_subscriber, invoice_url=invoice_url,
    )


@app.route("/download/png/<token>")
def download_png(token):
    order = get_order(token)
    if not order or not order.get("paid"):
        return "Acces non autorise.", 403
    return send_file(order["png_path"], as_attachment=True, download_name=f"retroplanning_{order['nom_client'].replace(' ', '_')}.png")


@app.route("/cancel")
def cancel():
    return render_template("cancel.html", contact_email=CONTACT_EMAIL)


@app.route("/blog")
def blog_index():
    articles = Article.query.filter_by(statut="publie").order_by(Article.published_at.desc()).all()
    return render_template("blog_index.html", articles=articles, current_category=None)


@app.route("/blog/categorie/<category_slug>")
def blog_category(category_slug):
    articles = [
        article for article in Article.query.filter_by(statut="publie").order_by(Article.published_at.desc()).all()
        if article.category_slug == category_slug
    ]
    if not articles:
        abort(404)
    return render_template("blog_index.html", articles=articles, current_category=articles[0].categorie)


@app.route("/blog/<slug>")
def blog_article(slug):
    article = Article.query.filter_by(slug=slug, statut="publie").first_or_404()
    related = Article.query.filter(
        Article.statut == "publie",
        Article.categorie == article.categorie,
        Article.id != article.id
    ).order_by(Article.published_at.desc()).limit(3).all()
    return render_template("blog_article.html", article=article, related=related)


@app.route("/admin", methods=["GET", "POST"])
def admin_login():
    error = None
    if request.method == "POST":
        if request.form.get("password", "") == ADMIN_PASSWORD:
            session["admin"] = True
            return redirect(url_for("admin_dashboard"))
        error = "Mot de passe incorrect."
    return render_template("admin_login.html", error=error)


@app.route("/admin/dashboard")
def admin_dashboard():
    if not is_admin():
        return redirect(url_for("admin_login"))
    return render_template("admin_dashboard.html")


@app.route("/admin/generate", methods=["GET", "POST"])
def admin_generate():
    if not is_admin():
        return redirect(url_for("admin_login"))

    if request.method == "POST":
        steps = get_steps_from_form(request.form)
        if len(steps) < 2:
            return "Il faut au moins une étape et la date événement.", 400
        file_path = generate_png(
            request.form.get("client", "CLIENT"),
            request.form.get("evenement", "ÉVÉNEMENT"),
            steps,
            request.form.get("phone", ""),
            request.form.get("email", ""),
            request.form.get("web", ""),
            request.form.get("footer_societe", ""),
            request.form.get("c_main", "#3F8078"),
            request.form.get("c_alt", "#75A097")
        )
        return send_file(file_path, as_attachment=True)

    return render_template("admin_generate.html")


@app.route("/admin/blog")
def admin_blog():
    if not is_admin():
        return redirect(url_for("admin_login"))
    articles = Article.query.order_by(Article.updated_at.desc()).all()
    return render_template("admin_blog_list.html", articles=articles)


def save_article_from_form(article):
    article.titre = request.form.get("titre", "").strip()
    article.slug = unique_slug(request.form.get("slug", "").strip() or article.titre, article.id)
    article.categorie = request.form.get("categorie", "").strip() or "Guides"
    article.extrait = request.form.get("extrait", "").strip()
    article.resume = request.form.get("resume", "").strip()
    article.contenu = request.form.get("contenu", "").strip()
    article.points_cles = request.form.get("points_cles", "").strip()
    article.image_url = request.form.get("image_url", "").strip()
    article.image_alt = request.form.get("image_alt", "").strip()
    article.meta_title = request.form.get("meta_title", "").strip() or article.titre
    article.meta_description = request.form.get("meta_description", "").strip() or article.extrait

    faqs = []
    for index in range(1, 4):
        question = request.form.get(f"faq_q{index}", "").strip()
        answer = request.form.get(f"faq_a{index}", "").strip()
        if question and answer:
            faqs.append({"question": question, "answer": answer})

    article.faq_json = json.dumps(faqs, ensure_ascii=False)
    article.statut = "publie" if request.form.get("statut") == "publie" else "brouillon"

    if article.statut == "publie" and not article.published_at:
        article.published_at = datetime.utcnow()
    if article.statut == "brouillon":
        article.published_at = None


@app.route("/admin/blog/nouveau", methods=["GET", "POST"])
def admin_blog_new():
    if not is_admin():
        return redirect(url_for("admin_login"))

    article = Article()
    if request.method == "POST":
        save_article_from_form(article)
        if not article.titre or not article.extrait or not article.contenu:
            return render_template(
                "admin_blog_form.html",
                article=article,
                error="Titre, extrait et contenu sont obligatoires."
            )
        db.session.add(article)
        db.session.commit()
        return redirect(url_for("admin_blog"))

    return render_template("admin_blog_form.html", article=article, error=None)


@app.route("/admin/blog/<int:article_id>/modifier", methods=["GET", "POST"])
def admin_blog_edit(article_id):
    if not is_admin():
        return redirect(url_for("admin_login"))

    article = Article.query.get_or_404(article_id)
    if request.method == "POST":
        save_article_from_form(article)
        if not article.titre or not article.extrait or not article.contenu:
            return render_template(
                "admin_blog_form.html",
                article=article,
                error="Titre, extrait et contenu sont obligatoires."
            )
        db.session.commit()
        return redirect(url_for("admin_blog"))

    return render_template("admin_blog_form.html", article=article, error=None)


@app.route("/admin/blog/<int:article_id>/supprimer", methods=["POST"])
def admin_blog_delete(article_id):
    if not is_admin():
        return redirect(url_for("admin_login"))

    article = Article.query.get_or_404(article_id)
    db.session.delete(article)
    db.session.commit()
    return redirect(url_for("admin_blog"))


@app.route("/admin/logout")
def admin_logout():
    session.pop("admin", None)
    return redirect("/")


@app.route("/llms.txt")
def llms_txt():
    lines = [
        "# Rétroplanning.eu",
        "",
        "> Générateur en ligne de rétroplannings événementiels et de production. "
        "L'utilisateur renseigne des jalons clés et une date d'échéance ; l'outil "
        "calcule et génère une frise chronologique visuelle, personnalisable aux "
        "couleurs de sa charte graphique et exportable en PNG haute définition.",
        "",
        "Rétroplanning.eu s'adresse aux agences de communication, organisateurs "
        "d'événements, chefs de projet et particuliers qui doivent visualiser les "
        "étapes clés menant à une date de livraison ou d'événement (mariage, salon "
        "professionnel, lancement de produit, déménagement, travaux, recrutement, "
        "événement associatif, soutenance de thèse).",
        "",
        "## Fonctionnement",
        "",
        "- Choix d'un modèle pré-rempli par type de projet, ou démarrage à vide, sur /modeles.",
        "- Saisie du client, de l'événement, des étapes clés avec leurs dates, des "
        "couleurs et du pied de page sur /formulaire.",
        "- Génération immédiate d'un export PNG haute résolution.",
        "",
        "## Tarifs",
        "",
        "- À l'acte : 2,40€ TTC, un rétroplanning, export PNG immédiat.",
        "- Starter 10 : 11,88€ TTC par mois, jusqu'à 10 rétroplannings par mois.",
        "- Illimité : 23,88€ TTC par mois, rétroplannings illimités.",
        "- Paiement sécurisé via Stripe, résiliation en libre-service à tout moment.",
        "",
        "## Pages principales",
        "",
        "- [Accueil](" + SITE_URL + "/) : présentation de l'outil et des tarifs.",
        "- [Rétroplanning événementiel](" + SITE_URL + "/retroplanning-evenementiel) : guide pour organiser un événement à rebours de sa date.",
        "- [Choix du modèle](" + SITE_URL + "/modeles) : modèles pré-remplis par type de projet.",
        "- [Créer un rétroplanning](" + SITE_URL + "/formulaire) : formulaire de génération.",
        "- [Tarifs](" + SITE_URL + "/tarifs) : détail des 3 formules.",
        "- [Blog](" + SITE_URL + "/blog) : guides sur la gestion de projet événementiel et de production.",
        "- [Gérer mon abonnement](" + SITE_URL + "/mon-compte) : portail client en libre-service.",
        "- [À propos](" + SITE_URL + "/a-propos) : qui édite Rétroplanning.eu.",
        "",
        "## Éditeur",
        "",
        "Rétroplanning.eu est édité par Omnipub (" + ENTREPRISE["ville"] + ", France). "
        "Contact : " + CONTACT_EMAIL + ".",
        "",
    ]
    return app.response_class("\n".join(lines), mimetype="text/plain")


@app.route("/robots.txt")
def robots():
    lines = [
        "User-agent: *",
        "Allow: /",
        "Disallow: /admin",
        "Disallow: /checkout",
        "Disallow: /success",
        "Disallow: /download",
        "",
        "Sitemap: " + SITE_URL + "/sitemap.xml",
        "",
    ]
    return app.response_class("\n".join(lines), mimetype="text/plain")


_sitemap_cache = {"content": None, "ts": 0}
SITEMAP_CACHE_TTL = 600


@app.route("/sitemap.xml")
def sitemap():
    import time
    now = time.time()
    if _sitemap_cache["content"] and (now - _sitemap_cache["ts"] < SITEMAP_CACHE_TTL):
        return app.response_class(_sitemap_cache["content"], mimetype="application/xml")

    urls = [
        (SITE_URL + "/", "weekly", "1.0"),
        (SITE_URL + "/retroplanning-evenementiel", "monthly", "0.9"),
        (SITE_URL + "/modeles", "monthly", "0.8"),
        (SITE_URL + "/tarifs", "monthly", "0.7"),
        (SITE_URL + "/blog", "weekly", "0.9"),
        (SITE_URL + "/ressources", "monthly", "0.6"),
        (SITE_URL + "/a-propos", "yearly", "0.4"),
    ]

    try:
        for article in Article.query.filter_by(statut="publie").all():
            urls.append((SITE_URL + "/blog/" + article.slug, "monthly", "0.8"))
    except Exception:
        # Base de donnees indisponible ou lente : on sert quand meme les pages statiques
        pass

    entries = []
    today = datetime.utcnow().date().isoformat()
    for location, frequency, priority in urls:
        entries.append(
            "<url>"
            + "<loc>" + location + "</loc>"
            + "<lastmod>" + today + "</lastmod>"
            + "<changefreq>" + frequency + "</changefreq>"
            + "<priority>" + priority + "</priority>"
            + "</url>"
        )

    content = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        + '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        + "".join(entries)
        + "</urlset>"
    )
    _sitemap_cache["content"] = content
    _sitemap_cache["ts"] = now

    return app.response_class(content, mimetype="application/xml")


with app.app_context():
    db.create_all()


if __name__ == "__main__":
    app.run(debug=True)
