import streamlit as st
import pandas as pd
import firebase_admin
from firebase_admin import credentials, firestore
from datetime import datetime
import time
from fpdf import FPDF
import os
import traceback

# --- CONFIGURATION DE LA PAGE ---
st.set_page_config(
    page_title="Chariot Urgence RME",
    page_icon="🚑",
    layout="wide",
    initial_sidebar_state="collapsed"
)

# --- CSS PERSONNALISÉ ---
st.markdown("""
<style>
    .stButton>button {
        width: 100%;
        border-radius: 8px;
        font-weight: bold;
        height: auto;
        padding: 0.5rem;
    }
    input[type="text"] {
        border: 2px solid #ff4b4b;
        border-radius: 10px;
    }
    .panier-box {
        padding: 15px;
        background-color: #1E88E5; 
        color: white;
        border-radius: 10px;
        margin-bottom: 20px;
        font-size: 1.2rem;
        font-weight: bold;
        text-align: center;
        box-shadow: 0 4px 6px rgba(0,0,0,0.1);
    }
    .stCheckbox {
        padding-top: 10px;
    }
    .security-box {
        border: 2px solid #d32f2f;
        background-color: #ffebee;
        padding: 15px;
        border-radius: 10px;
        margin-top: 20px;
        margin-bottom: 20px;
    }
    .icon-text {
        font-size: 1.5rem;
        text-align: center;
        margin: 0;
    }
</style>
""", unsafe_allow_html=True)

# --- CONSTANTES ---
SHARED_ACCOUNTS = ["infirmier", "resident", "interne"]

# --- 1. BACKEND (FIRESTORE) ---
@st.cache_resource
def get_db():
    """
    Initialise Firestore de façon robuste:
    - priorise st.secrets["firestore"] si disponible
    - fallback vers firestore_key.json local
    - réutilise l'app Firebase existante si déjà initialisée
    """
    try:
        # Si déjà initialisé, on réutilise
        if firebase_admin._apps:
            return firestore.client()

        cred = None

        if "firestore" in st.secrets:
            key_dict = dict(st.secrets["firestore"])
            if "private_key" in key_dict and isinstance(key_dict["private_key"], str):
                key_dict["private_key"] = key_dict["private_key"].replace("\\n", "\n")
            cred = credentials.Certificate(key_dict)
        elif os.path.exists("firestore_key.json"):
            cred = credentials.Certificate("firestore_key.json")
        else:
            print("[Firestore] Aucune config trouvée (ni st.secrets['firestore'] ni firestore_key.json).")
            return None

        firebase_admin.initialize_app(cred)
        return firestore.client()

    except Exception as e:
        print("[Firestore] Erreur init:", e)
        traceback.print_exc()
        return None


db = get_db()

# --- 2. FONCTIONS MÉTIER ---

def get_inventaire_df():
    if db is None:
        return pd.DataFrame()
    try:
        docs = db.collection("INVENTAIRE").stream()
        items = []
        for doc in docs:
            data = doc.to_dict() or {}
            data['ID'] = doc.id
            items.append(data)

        if not items:
            return pd.DataFrame()

        df = pd.DataFrame(items)
        if 'ID' in df.columns:
            df = df.drop_duplicates(subset=['ID'], keep='first').sort_values(by='ID')
        return df
    except Exception as e:
        print("Erreur get_inventaire_df:", e)
        return pd.DataFrame()


def maj_panier():
    # Parcours sécurisé des clés (copie de liste)
    for key in list(st.session_state.keys()):
        if key.startswith("input_"):
            item_id = key.replace("input_", "", 1)
            qty = st.session_state.get(key, 0)
            try:
                qty = int(qty)
            except Exception:
                qty = 0

            if qty > 0:
                st.session_state['panier'][item_id] = qty
            elif item_id in st.session_state['panier']:
                del st.session_state['panier'][item_id]


def valider_panier(panier, ip, utilisateur):
    if db is None:
        return False
    try:
        batch = db.batch()
        details_list = []
        details_texte = []

        for item_id, qte in panier.items():
            doc_ref = db.collection("INVENTAIRE").document(item_id)
            doc = doc_ref.get()
            if doc.exists:
                data = doc.to_dict() or {}
                nom = data.get('Nom', 'Inconnu')
                stock_actuel = int(data.get('Stock_Actuel', 0))
                tiroir = data.get('Tiroir', '?')
                nouveau_stock = max(0, stock_actuel - int(qte))
                batch.update(doc_ref, {"Stock_Actuel": nouveau_stock})

                details_texte.append(f"{qte}x {nom}")
                details_list.append({
                    "ID": item_id,
                    "Nom": nom,
                    "Qte": int(qte),
                    "Tiroir": tiroir,
                    "EstRemplace": False
                })

        log_data = {
            "Date": datetime.now(),
            "Utilisateur": utilisateur,
            "IP_Patient": ip,
            "Action": "Consommation",
            "Details_Complets": details_texte,
            "Details_Struct": details_list,
            "Nb_Produits": len(panier),
            "Statut": "Non remplacé",
            "Date_Remplacement": None,
            "Utilisateur_Remplacement": None,
            "Historique_Remplacements": []
        }

        db.collection("LOGS").add(log_data)
        batch.commit()
        return True
    except Exception as e:
        print("Erreur valider_panier:", e)
        traceback.print_exc()
        return False


def effectuer_remplacement_partiel(log_id, log_data, items_coches, user_remplacant):
    if db is None:
        return False

    try:
        batch = db.batch()
        items_struct = log_data.get('Details_Struct', [])
        tout_est_remplace = True
        nouveaux_items_struct = []
        items_modifies_noms = []

        for item in items_struct:
            item_id = item.get('ID')
            if not item_id:
                nouveaux_items_struct.append(item)
                continue

            if item.get('EstRemplace', False):
                nouveaux_items_struct.append(item)
                continue

            if item_id in items_coches:
                qte_a_rendre = int(item.get('Qte', 0))
                doc_ref = db.collection("INVENTAIRE").document(item_id)
                doc = doc_ref.get()

                if doc.exists:
                    current = doc.to_dict() or {}
                    stock_now = int(current.get('Stock_Actuel', 0))
                    dotation = int(current.get('Dotation', 0))
                    nouveau_stock = min(dotation, stock_now + qte_a_rendre)
                    batch.update(doc_ref, {"Stock_Actuel": nouveau_stock})

                item['EstRemplace'] = True
                items_modifies_noms.append(item.get('Nom', item_id))
            else:
                tout_est_remplace = False

            nouveaux_items_struct.append(item)

        log_ref = db.collection("LOGS").document(log_id)
        updates = {"Details_Struct": nouveaux_items_struct}
        trace = {
            "Date": datetime.now(),
            "User": user_remplacant,
            "Items": items_modifies_noms
        }

        histo = log_data.get('Historique_Remplacements', [])
        if not isinstance(histo, list):
            histo = []
        histo.append(trace)
        updates["Historique_Remplacements"] = histo

        if tout_est_remplace:
            updates["Statut"] = "Remplacé"
            updates["Date_Remplacement"] = datetime.now()
            updates["Utilisateur_Remplacement"] = user_remplacant

        batch.update(log_ref, updates)
        batch.commit()
        return tout_est_remplace
    except Exception as e:
        print("Erreur remplacement partiel:", e)
        traceback.print_exc()
        return False


def supprimer_log(log_id):
    if db:
        try:
            db.collection("LOGS").document(log_id).delete()
        except Exception as e:
            print("Erreur supprimer_log:", e)


# --- GENERATION PDF ---
class PDF(FPDF):
    def header(self):
        if os.path.exists("logo_chu.png"):
            self.image("logo_chu.png", 170, 8, 30)
        if os.path.exists("logo_service.png"):
            self.image("logo_service.png", 10, 8, 30)
        self.set_font('Helvetica', 'B', 15)
        self.cell(0, 10, 'CHECKLISTE CHARIOT URGENCE', 0, 1, 'C')
        self.set_font('Helvetica', 'I', 10)
        self.cell(0, 10, 'Service Réanimation Mère-Enfant - CHU Hassan II', 0, 1, 'C')
        self.ln(20)

    def footer(self):
        self.set_y(-15)
        self.set_font('Helvetica', 'I', 8)
        self.cell(0, 10, f'Page {self.page_no()}', 0, 0, 'C')


def generer_pdf_checklist(data_checklist, user, date_check):
    pdf = PDF()
    pdf.add_page()
    pdf.set_font("Helvetica", size=12)
    date_fmt = date_check.strftime("%d/%m/%Y à %H:%M")
    pdf.multi_cell(
        0, 10,
        f"La checkliste de Chariot d'Urgence RME a été faite le {date_fmt} par l'utilisateur {user}.\nStatut : VALIDÉE (Conforme)",
        align='C'
    )
    pdf.ln(10)

    pdf.set_fill_color(200, 220, 255)
    pdf.set_font("Helvetica", 'B', 10)
    pdf.cell(90, 10, "Matériel", 1, 0, 'C', True)
    pdf.cell(30, 10, "Tiroir", 1, 0, 'C', True)
    pdf.cell(30, 10, "Dotation", 1, 0, 'C', True)
    pdf.cell(30, 10, "État", 1, 1, 'C', True)

    pdf.set_font("Helvetica", size=9)
    for item in data_checklist:
        nom = str(item.get('Nom', '')).encode('latin-1', 'replace').decode('latin-1')
        tiroir = str(item.get('Tiroir', ''))
        dotation = str(item.get('Dotation', ''))
        pdf.cell(90, 8, nom, 1)
        pdf.cell(30, 8, tiroir, 1, 0, 'C')
        pdf.cell(30, 8, dotation, 1, 0, 'C')
        pdf.cell(30, 8, "[X] OK", 1, 1, 'C')

    pdf.set_font("Helvetica", 'B', 9)
    pdf.set_fill_color(240, 240, 240)
    pdf.cell(150, 8, "Tiroirs verrouillés par la clé", 1, 0, 'L', True)
    pdf.cell(30, 8, "[X] OUI", 1, 1, 'C', True)
    pdf.cell(150, 8, "Clé et paire de ciseaux attachés au chariot (Ziplock)", 1, 0, 'L', True)
    pdf.cell(30, 8, "[X] OUI", 1, 1, 'C', True)
    pdf.ln(10)

    pdf.cell(0, 10, f"Checkliste générée le {datetime.now().strftime('%d/%m/%Y %H:%M')}", 0, 1, 'R')

    # FPDF output robuste en bytes
    pdf_str = pdf.output(dest='S')
    if isinstance(pdf_str, str):
        return pdf_str.encode('latin-1', 'replace')
    return bytes(pdf_str)


# --- AUTHENTIFICATION ---
def check_login(username, password):
    if db is None:
        return None
    if not username:
        return None

    try:
        username = username.strip()
        doc_ref = db.collection("UTILISATEURS").document(username)
        doc = doc_ref.get()

        if doc.exists:
            data = doc.to_dict() or {}
            pwd_db = data.get('password')

            # Compare en string (évite mismatch type int/str)
            if str(pwd_db) == str(password):
                return data
    except Exception as e:
        print(f"Erreur login: {e}")
        traceback.print_exc()
        return None
    return None


def login_page():
    st.image("https://cdn-icons-png.flaticon.com/512/3063/3063176.png", width=80)
    st.title("Chariot Urgence")

    if db is None:
        st.error("Connexion à la base impossible. Vérifie `st.secrets['firestore']` (surtout private_key) sur Streamlit Cloud.")
        st.stop()

    with st.form("login"):
        user_id = st.text_input("Identifiant")
        pwd = st.text_input("Mot de passe", type="password")
        submit = st.form_submit_button("Se Connecter")

        if submit:
            if not user_id or not pwd:
                st.warning("Veuillez remplir tous les champs.")
            else:
                user_info = check_login(user_id, pwd)
                if user_info:
                    st.session_state['logged_in'] = True
                    st.session_state['user_id'] = user_id.strip()

                    p = user_info.get('prenom', '')
                    n = user_info.get('nom', '')
                    st.session_state['user'] = f"{p} {n}".strip() or user_id.strip()
                    st.session_state['role'] = user_info.get('role', 'Utilisateur')

                    if 'panier' not in st.session_state:
                        st.session_state['panier'] = {}
                    if 'check_state' not in st.session_state:
                        st.session_state['check_state'] = {}
                    st.rerun()
                else:
                    st.error("Identifiant ou mot de passe incorrect.")


# --- INTERFACES PRECEDENTES ---
def afficher_ligne_conso(row):
    try:
        stock = int(row.get('Stock_Actuel', 0))
        dotation = int(row.get('Dotation', 0))
    except Exception:
        stock, dotation = 0, 0

    couleur = "red" if stock < dotation else "green"
    text_stock = f":{couleur}[**{stock}/{dotation}**]"
    cat = str(row.get('Categorie', ''))
    infos = f"| {cat}" if cat.lower() != 'nan' and cat.strip() != "" else ""

    with st.container(border=True):
        c1, c2, c3, c4 = st.columns([3, 1, 0.3, 1.5])
        with c1:
            st.markdown(f"**{row.get('Nom', 'Inconnu')}**")
            if infos:
                st.caption(infos)
        with c2:
            st.markdown(text_stock)
        with c3:
            st.markdown("<div class='icon-text'>📉</div>", unsafe_allow_html=True)
        with c4:
            valeur_actuelle = st.session_state['panier'].get(row['ID'], 0)
            st.number_input(
                "Qté",
                min_value=0,
                max_value=max(stock, 0),
                value=min(valeur_actuelle, max(stock, 0)),
                key=f"input_{row['ID']}",
                label_visibility="collapsed",
                on_change=maj_panier,
                step=1
            )


def interface_consommateur():
    st.header("💊 Consommation")
    df = get_inventaire_df()
    if df.empty:
        st.info("Inventaire vide ou indisponible.")
        return

    if st.session_state.get('panier'):
        nb_items = sum(st.session_state['panier'].values())
        st.markdown(f"""<div class="panier-box">🛒 PANIER : {nb_items} articles</div>""", unsafe_allow_html=True)

        with st.expander("✅ VALIDER LA CONSOMMATION", expanded=True):
            st.write("Récapitulatif :")
            for pid, q in st.session_state['panier'].items():
                nom_row = df[df['ID'] == pid]
                if not nom_row.empty:
                    st.write(f"- {q} x **{nom_row['Nom'].values[0]}**")

            st.divider()
            ip_input = st.text_input("🏥 IP DU PATIENT", placeholder="Ex: 24/12345")

            user_final = st.session_state['user']
            if st.session_state.get('user_id') in SHARED_ACCOUNTS:
                user_final = st.text_input("👤 Votre Nom et Prénom (Obligatoire pour traçabilité)", key="user_input_conso")

            c1, c2 = st.columns([2, 1])

            if c1.button("🚀 ENREGISTRER", type="primary"):
                if not ip_input:
                    st.error("⚠️ IP obligatoire")
                elif not user_final:
                    st.error("⚠️ Veuillez entrer votre Nom et Prénom.")
                else:
                    ok = valider_panier(st.session_state['panier'], ip_input, user_final)
                    if ok:
                        st.session_state['panier'] = {}
                        for k in list(st.session_state.keys()):
                            if k.startswith("input_"):
                                del st.session_state[k]
                        time.sleep(0.8)
                        st.success("Enregistré !")
                        st.rerun()
                    else:
                        st.error("Erreur d'enregistrement.")

            if c2.button("🗑️ Vider"):
                st.session_state['panier'] = {}
                for k in list(st.session_state.keys()):
                    if k.startswith("input_"):
                        del st.session_state[k]
                st.rerun()

    recherche = st.text_input("🔍 Rechercher...")
    if recherche:
        masque = df['Nom'].astype(str).str.contains(recherche, case=False, na=False)
        for _, row in df[masque].iterrows():
            afficher_ligne_conso(row)
    else:
        for tiroir in ["Dessus", "Tiroir 1", "Tiroir 2", "Tiroir 3", "Tiroir 4", "Tiroir 5"]:
            if tiroir in df['Tiroir'].astype(str).unique():
                with st.expander(f"🗄️ {tiroir}"):
                    for _, row in df[df['Tiroir'] == tiroir].iterrows():
                        afficher_ligne_conso(row)


def interface_remplacement():
    st.header("🔄 Remplacer")
    if db is None:
        st.error("Base indisponible.")
        return

    try:
        logs_ref = db.collection("LOGS").order_by("Date", direction=firestore.Query.DESCENDING).stream()
        count = 0

        for doc in logs_ref:
            l = doc.to_dict() or {}
            if l.get("Statut") != "Non remplacé":
                continue

            count += 1
            log_id = doc.id
            date_val = l.get('Date')
            date_str = date_val.strftime("%d/%m/%Y %H:%M") if date_val else "?"

            with st.container(border=True):
                st.markdown(f"📅 **{date_str}** | 👤 {l.get('Utilisateur', '?')} | 🏥 IP: **{l.get('IP_Patient', '?')}**")
                st.divider()

                with st.form(key=f"form_repl_{log_id}"):
                    to_repl = []
                    for item in l.get('Details_Struct', []):
                        nom = f"{item.get('Qte', 0)}x {item.get('Nom', 'Inconnu')}"
                        if item.get('EstRemplace', False):
                            st.markdown(f"~~✅ {nom}~~")
                        else:
                            item_id = item.get('ID')
                            if item_id and st.checkbox(f"🔴 {nom}", key=f"chk_{log_id}_{item_id}"):
                                to_repl.append(item_id)

                    user_final = st.session_state['user']
                    if st.session_state.get('user_id') in SHARED_ACCOUNTS:
                        user_final = st.text_input("👤 Votre Nom et Prénom", key=f"user_input_repl_{log_id}")

                    if st.form_submit_button("💾 Valider"):
                        if not to_repl:
                            st.warning("Rien coché")
                        elif not user_final:
                            st.error("⚠️ Nom obligatoire")
                        else:
                            fin = effectuer_remplacement_partiel(log_id, l, to_repl, user_final)
                            st.success("Fait !" if fin else "Partiel enregistré")
                            time.sleep(0.8)
                            st.rerun()

        if count == 0:
            st.success("Tout est à jour !")

    except Exception as e:
        st.error(f"Erreur interface remplacement: {e}")
        traceback.print_exc()


def interface_historique():
    st.header("📜 Historique de consommation")
    if db is None:
        st.error("Base indisponible.")
        return

    try:
        logs_ref = db.collection("LOGS").order_by("Date", direction=firestore.Query.DESCENDING).limit(50).stream()
        data = []

        for doc in logs_ref:
            l = doc.to_dict() or {}
            log_id = doc.id
            statut = l.get('Statut', 'Inconnu')

            if statut == "Non remplacé":
                st_fmt = "🟠 En cours"
                remp = ""
            else:
                d = l.get('Date_Remplacement')
                ds = d.strftime("%d/%m %H:%M") if d else "?"
                st_fmt = "🟢 Remplacé"
                remp = f"{l.get('Utilisateur_Remplacement', '?')} ({ds})"

            det = "\n".join([
                f"{'✅' if i.get('EstRemplace') else '🔴'} {i.get('Qte', 0)}x {i.get('Nom', 'Inconnu')}"
                for i in l.get('Details_Struct', [])
            ])

            date_txt = l.get('Date').strftime("%d/%m %H:%M") if l.get('Date') else "?"
            data.append({
                "Date": date_txt,
                "IP": l.get('IP_Patient', ''),
                "User": l.get('Utilisateur', ''),
                "Mat": det,
                "St": st_fmt,
                "Remp": remp,
                "ID": log_id,
                "Suppr": False
            })

        if data:
            df = pd.DataFrame(data)
            cfg = {
                "Mat": st.column_config.TextColumn("Détail", width="large"),
                "ID": None
            }

            if st.session_state.get('user_id') == 'admin':
                res = st.data_editor(
                    df,
                    column_config=cfg,
                    hide_index=True,
                    use_container_width=True,
                    disabled=["Date", "IP", "User", "Mat", "St", "Remp"]
                )
                to_del = res[res["Suppr"] == True]
                if not to_del.empty and st.button("🗑️ Confirmer suppression"):
                    for _, r in to_del.iterrows():
                        if "🟢" in str(r['St']):
                            supprimer_log(r['ID'])
                        else:
                            st.error("Impossible suppr. dossier en cours")
                    st.rerun()
            else:
                st.dataframe(
                    df.drop(columns=["Suppr", "ID"]),
                    column_config=cfg,
                    hide_index=True,
                    use_container_width=True
                )

    except Exception as e:
        st.error(f"Erreur interface historique: {e}")
        traceback.print_exc()


# --- INTERFACE CHECKLISTE ---
def verifier_blocage_checklist():
    if db is None:
        return False
    try:
        logs_ref = db.collection("LOGS").where("Statut", "==", "Non remplacé").stream()
        items_manquants = [doc.id for doc in logs_ref]
        return len(items_manquants) > 0
    except Exception as e:
        print("Erreur verifier_blocage_checklist:", e)
        return False


def save_checklist_history(user, data_items):
    if db:
        try:
            doc_data = {
                "Date": datetime.now(),
                "Utilisateur": user,
                "Statut": "Validé",
                "Contenu": data_items,
                "Securite_Verrou": True,
                "Securite_Attache": True
            }
            db.collection("CHECKLISTS").add(doc_data)
        except Exception as e:
            print("Erreur save_checklist_history:", e)


def interface_checklist():
    st.header("📋 Checkliste de Vérification")

    with st.expander("📂 Consulter les anciennes checklists (Historique)", expanded=False):
        if db:
            try:
                checks = db.collection("CHECKLISTS").order_by("Date", direction=firestore.Query.DESCENDING).limit(10).stream()
                history_data = []
                history_map = {}

                for c in checks:
                    d = c.to_dict() or {}
                    d_date = d.get('Date')
                    date_str = d_date.strftime("%d/%m/%Y %H:%M") if d_date else "?"
                    user_txt = d.get('Utilisateur', '?')
                    label = f"{date_str} - {user_txt}"
                    entry_data = d.copy()
                    entry_data['ID'] = c.id
                    history_data.append({"Label": label, "Date": date_str, "User": user_txt})
                    history_map[label] = entry_data

                if not history_data:
                    st.info("Aucune archive.")
                else:
                    sel = st.selectbox("Choisir une checkliste", [h["Label"] for h in history_data])
                    c1, c2 = st.columns([3, 1])

                    with c1:
                        if st.button("📄 Régénérer PDF"):
                            s = history_map[sel]
                            pdf = generer_pdf_checklist(
                                s.get('Contenu', []),
                                s.get('Utilisateur', '?'),
                                s.get('Date', datetime.now())
                            )
                            st.download_button("📥 PDF", data=pdf, file_name="Archive.pdf", mime="application/pdf")

                    if st.session_state.get('user_id') == 'admin':
                        with c2:
                            st.markdown("<br>", unsafe_allow_html=True)
                            if st.button("🗑️ Suppr"):
                                db.collection("CHECKLISTS").document(history_map[sel]['ID']).delete()
                                st.toast("Supprimé !")
                                time.sleep(0.8)
                                st.rerun()

            except Exception as e:
                st.error(f"Erreur lecture historique checklist: {e}")

    st.divider()

    if verifier_blocage_checklist():
        st.error("⛔ ACTIONS REQUISES : Matériel 'Non Remplacé' détecté.")
        return

    st.success("✅ Stock OK. Vérification en cours.")

    try:
        df = get_inventaire_df()
        if df.empty:
            st.info("Inventaire vide.")
            return

        if 'check_state' not in st.session_state:
            st.session_state['check_state'] = {}

        if 'pdf_ready' not in st.session_state:
            st.session_state['pdf_ready'] = None

        tiroirs = ["Dessus", "Tiroir 1", "Tiroir 2", "Tiroir 3", "Tiroir 4", "Tiroir 5"]

        for tiroir in tiroirs:
            if tiroir in df['Tiroir'].astype(str).unique():
                with st.expander(f"🗄️ {tiroir}", expanded=True):
                    col_batch, _ = st.columns([1, 2])

                    if col_batch.button(f"✅ Valider tout le {tiroir}", key=f"btn_batch_{tiroir}"):
                        for _, row in df[df['Tiroir'] == tiroir].iterrows():
                            iid = row['ID']
                            st.session_state['check_state'][iid] = "OK"
                            st.session_state[f"rad_{iid}"] = "Conforme"
                        st.rerun()

                    for _, row in df[df['Tiroir'] == tiroir].iterrows():
                        iid = row['ID']
                        c1, c2, c3 = st.columns([3, 1, 2])

                        with c1:
                            st.markdown(f"**{row.get('Nom', 'Inconnu')}**")
                        with c2:
                            st.markdown(f"Dot: **{row.get('Dotation', '?')}**")
                        with c3:
                            # index robuste (pas de None)
                            current = st.session_state.get(f"rad_{iid}")
                            options = ["Conforme", "Manquant"]
                            if current not in options:
                                current = "Conforme"  # choix par défaut stable
                            idx = options.index(current)

                            status = st.radio(
                                f"Etat {iid}",
                                options,
                                key=f"rad_{iid}",
                                horizontal=True,
                                label_visibility="collapsed",
                                index=idx
                            )

                            if status == "Manquant":
                                st.session_state['check_state'][iid] = "KO"
                                if st.button(f"🔄 Remplacer ({row.get('Nom', 'Inconnu')})", key=f"fix_{iid}"):
                                    st.toast("Noté")
                            elif status == "Conforme":
                                st.session_state['check_state'][iid] = "OK"
                            else:
                                st.session_state['check_state'][iid] = "PENDING"

        st.divider()

        all_ids = df['ID'].tolist()
        missing = 0
        pending = 0

        for iid in all_ids:
            state = st.session_state['check_state'].get(iid, "PENDING")
            if state == "KO":
                missing += 1
            elif state == "PENDING":
                pending += 1

        if pending == 0 and missing == 0:
            st.markdown("<div class='security-box'>", unsafe_allow_html=True)
            st.markdown("### 🔒 Sécurisation Finale")
            st.markdown("Avez-vous verrouillé les tiroirs par la clé et attaché la clé et paires ciseaux par ziplock au dessus du chariot ?")
            securite = st.checkbox("✅ OUI, je confirme la sécurisation (Clé + Ciseaux + Verrouillage)")
            st.markdown("</div>", unsafe_allow_html=True)

            if securite:
                user_final = st.session_state['user']
                if st.session_state.get('user_id') in SHARED_ACCOUNTS:
                    user_final = st.text_input("👤 Votre Nom et Prénom (Obligatoire)", key="user_input_check")

                if st.button("💾 VALIDER CHECKLISTE", type="primary"):
                    if not user_final:
                        st.error("⚠️ Nom obligatoire.")
                    else:
                        checklist_export = []
                        for _, row in df.iterrows():
                            checklist_export.append({
                                "Nom": row.get('Nom', ''),
                                "Tiroir": row.get('Tiroir', ''),
                                "Dotation": row.get('Dotation', '')
                            })

                        save_checklist_history(user_final, checklist_export)
                        st.session_state['pdf_ready'] = generer_pdf_checklist(
                            checklist_export, user_final, datetime.now()
                        )
                        st.success("✅ Checkliste validée et archivée !")
                        st.balloons()
                        st.rerun()

                if st.session_state.get('pdf_ready'):
                    st.download_button(
                        "📥 Télécharger le PDF",
                        data=st.session_state['pdf_ready'],
                        file_name=f"Checklist_{datetime.now().strftime('%Y%m%d')}.pdf",
                        mime="application/pdf"
                    )
            else:
                st.warning("⚠️ Confirmez la sécurité pour valider.")
        else:
            if pending > 0:
                st.warning(f"⚠️ Reste à vérifier : {pending} lignes.")
            if missing > 0:
                st.error(f"⚠️ Matériel manquant : {missing} lignes.")
            st.button("💾 Valider", disabled=True)

    except Exception as e:
        st.error(f"Une erreur est survenue : {e}")
        traceback.print_exc()


# --- RESPONSABLE ---
def interface_responsable():
    tab1, tab2, tab3 = st.tabs(["📊 Stock", "📜 Historique Conso", "📋 Historique Checklists"])

    with tab1:
        st.header("État du stock")
        df = get_inventaire_df()
        if not df.empty:
            cols = [c for c in ['Nom', 'Tiroir', 'Stock_Actuel', 'Dotation'] if c in df.columns]
            st.dataframe(df[cols], use_container_width=True)
        else:
            st.info("Aucune donnée stock.")

    with tab2:
        interface_historique()

    with tab3:
        st.header("Historique des Checklists")
        if db:
            try:
                checks = db.collection("CHECKLISTS").order_by("Date", direction=firestore.Query.DESCENDING).limit(20).stream()
                data = []
                for c in checks:
                    d = c.to_dict() or {}
                    date_txt = d.get('Date').strftime("%d/%m/%Y %H:%M") if d.get('Date') else "?"
                    data.append({
                        "Date": date_txt,
                        "Utilisateur": d.get('Utilisateur', '?'),
                        "Statut": d.get('Statut', '?')
                    })
                if data:
                    st.dataframe(pd.DataFrame(data), use_container_width=True)
                else:
                    st.info("Aucune checkliste.")
            except Exception as e:
                st.error(f"Erreur historique checklists: {e}")


# --- MENU PRINCIPAL ---
def main():
    if 'logged_in' not in st.session_state:
        st.session_state['logged_in'] = False
    if 'panier' not in st.session_state:
        st.session_state['panier'] = {}

    if not st.session_state['logged_in']:
        login_page()
    else:
        with st.sidebar:
            st.write(f"👤 {st.session_state.get('user', 'Utilisateur')}")
            st.write(f"Role : {st.session_state.get('role', 'Utilisateur')}")

            if st.button("Déconnexion"):
                # reset propre
                keys_to_keep = []
                for k in list(st.session_state.keys()):
                    if k not in keys_to_keep:
                        del st.session_state[k]
                st.session_state['logged_in'] = False
                st.session_state['panier'] = {}
                st.rerun()

            st.divider()
            menu_options = ["Consommation", "Remplacer", "Historique", "Checkliste"]
            choix = st.radio("Navigation", menu_options)

        if choix == "Consommation":
            interface_consommateur()
        elif choix == "Remplacer":
            interface_remplacement()
        elif choix == "Checkliste":
            interface_checklist()
        elif choix == "Historique":
            if st.session_state.get('role') in ["Responsable", "Administrateur", "SuperAdmin"]:
                interface_responsable()
            else:
                interface_historique()


if __name__ == "__main__":
    main()
