import streamlit as st
import pandas as pd
import firebase_admin
from firebase_admin import credentials, firestore
from datetime import datetime
import time
from fpdf import FPDF
import os
import traceback
import json

# --- 0. CONFIGURATION ET SESSION ---
st.set_page_config(
    page_title="Chariot Urgence RME",
    page_icon="🚑",
    layout="wide",
    initial_sidebar_state="collapsed"
)

# Initialisation des variables de session
if 'logged_in' not in st.session_state:
    st.session_state['logged_in'] = False
if 'panier' not in st.session_state:
    st.session_state['panier'] = {}
if 'user' not in st.session_state:
    st.session_state['user'] = ""
if 'role' not in st.session_state:
    st.session_state['role'] = ""
if 'check_state' not in st.session_state:
    st.session_state['check_state'] = {}
if 'pdf_ready' not in st.session_state:
    st.session_state['pdf_ready'] = None

# --- CSS PERSONNALISÉ ---
st.markdown("""
<style>
    .stButton>button { width: 100%; border-radius: 8px; font-weight: bold; height: auto; padding: 0.5rem; }
    input[type="text"], input[type="password"] { border: 1px solid #6c757d !important; border-radius: 10px; }
    .panier-box { padding: 15px; background-color: #1E88E5; color: white; border-radius: 10px; margin-bottom: 20px; font-size: 1.2rem; font-weight: bold; text-align: center; box-shadow: 0 4px 6px rgba(0,0,0,0.1); }
    .stCheckbox { padding-top: 10px; }
    .security-box { border: 2px solid #d32f2f; background-color: #ffebee; padding: 15px; border-radius: 10px; margin-top: 20px; margin-bottom: 20px; }
    .icon-text { font-size: 1.5rem; text-align: center; margin: 0; }
</style>
""", unsafe_allow_html=True)

# --- CONSTANTES ---
SHARED_ACCOUNTS = ["infirmier", "resident", "interne"]
DEBUG_LOGIN = False

# --- 1. BACKEND (FIRESTORE) ---
@st.cache_resource
def get_db():
    try:
        if firebase_admin._apps:
            return firestore.client()

        cred = None
        source = None
        # 1) Fichier local
        local_path = "firestore_key.json"
        if os.path.exists(local_path):
            cred = credentials.Certificate(local_path)
            source = f"local file: {local_path}"
        # 2) Secrets Streamlit
        else:
            try:
                secret_keys = list(st.secrets.keys())
                key_dict = None
                if "firestore" in st.secrets:
                    key_dict = dict(st.secrets["firestore"])
                else:
                    required = ["type", "project_id", "private_key", "client_email", "token_uri"]
                    if all(k in st.secrets for k in required):
                        key_dict = {k: st.secrets[k] for k in st.secrets.keys()}

                if key_dict is None:
                    st.error("🚨 Secrets non trouvés.")
                    return None

                pk = key_dict.get("private_key", "")
                if isinstance(pk, str):
                    if "\\n" in pk:
                        key_dict["private_key"] = pk.replace("\\n", "\n")
                
                cred = credentials.Certificate(key_dict)
                source = "streamlit secrets"
            except Exception as se:
                st.error(f"🚨 Erreur lecture secrets: {se}")
                return None

        firebase_admin.initialize_app(cred)
        print(f"Firebase initialized from {source}")
        return firestore.client()
    except Exception as e:
        st.error(f"🚨 Erreur BDD: {e}")
        return None

db = get_db()

# --- 2. FONCTIONS DE LECTURE (OPTIMISÉES / CACHÉES) ---

# OPTIMISATION MAJEURE : On met en cache l'inventaire pour ne pas lire la BDD à chaque clic
@st.cache_data(ttl=600) # Garde en mémoire 10 minutes ou jusqu'à vidange manuelle
def get_inventaire_cached():
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
        print("Erreur get_inventaire_cached:", e)
        return pd.DataFrame()

@st.cache_data(ttl=300)
def get_logs_remplacement_cached():
    if db is None: return []
    try:
        # On ne récupère que les logs "Non remplacé" pour économiser
        logs_ref = db.collection("LOGS").where("Statut", "==", "Non remplacé").order_by("Date", direction=firestore.Query.DESCENDING).stream()
        data = []
        for doc in logs_ref:
            l = doc.to_dict()
            l['id_doc'] = doc.id
            data.append(l)
        return data
    except Exception as e:
        print("Erreur logs remplacement:", e)
        return []

@st.cache_data(ttl=300)
def get_historique_cached(limit=50):
    if db is None: return []
    try:
        logs_ref = db.collection("LOGS").order_by("Date", direction=firestore.Query.DESCENDING).limit(limit).stream()
        data = []
        for doc in logs_ref:
            l = doc.to_dict()
            l['id_doc'] = doc.id
            data.append(l)
        return data
    except Exception as e:
        print("Erreur historique:", e)
        return []

def clear_cache_app():
    """Vide le cache pour forcer une relecture après une modification"""
    st.cache_data.clear()

# --- 3. FONCTIONS D'ÉCRITURE (ACTIONS) ---

def valider_panier(panier, ip, utilisateur):
    if db is None: return False
    try:
        batch = db.batch()
        details_list = []
        details_texte = []
        
        # On lit les docs nécessaires (lecture unitaire inévitable pour la sécurité du stock)
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
                    "ID": item_id, "Nom": nom, "Qte": int(qte), 
                    "Tiroir": tiroir, "EstRemplace": False
                })

        log_data = {
            "Date": datetime.now(), "Utilisateur": utilisateur, "IP_Patient": ip,
            "Action": "Consommation", "Details_Complets": details_texte,
            "Details_Struct": details_list, "Nb_Produits": len(panier),
            "Statut": "Non remplacé", "Historique_Remplacements": []
        }
        db.collection("LOGS").add(log_data)
        batch.commit()
        
        # CRITIQUE : On vide le cache car le stock a changé
        clear_cache_app()
        return True
    except Exception as e:
        print("Erreur valider_panier:", e)
        return False

def effectuer_remplacement_partiel(log_id, log_data, items_coches, user_remplacant):
    if db is None: return False
    try:
        batch = db.batch()
        items_struct = log_data.get('Details_Struct', [])
        tout_est_remplace = True
        nouveaux_items_struct = []
        items_modifies_noms = []

        for item in items_struct:
            item_id = item.get('ID')
            if not item_id or item.get('EstRemplace', False):
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
        trace = {"Date": datetime.now(), "User": user_remplacant, "Items": items_modifies_noms}
        
        histo = log_data.get('Historique_Remplacements', [])
        if not isinstance(histo, list): histo = []
        histo.append(trace)
        updates["Historique_Remplacements"] = histo

        if tout_est_remplace:
            updates["Statut"] = "Remplacé"
            updates["Date_Remplacement"] = datetime.now()
            updates["Utilisateur_Remplacement"] = user_remplacant

        batch.update(log_ref, updates)
        batch.commit()
        
        # CRITIQUE : On vide le cache
        clear_cache_app()
        return tout_est_remplace
    except Exception as e:
        print("Erreur remplacement:", e)
        return False

def supprimer_log(log_id):
    if db:
        try:
            db.collection("LOGS").document(log_id).delete()
            clear_cache_app()
        except Exception as e:
            print("Erreur suppr:", e)

def save_checklist_history(user, data_items):
    if db:
        try:
            doc_data = {
                "Date": datetime.now(), "Utilisateur": user, "Statut": "Validé",
                "Contenu": data_items, "Securite_Verrou": True, "Securite_Attache": True
            }
            db.collection("CHECKLISTS").add(doc_data)
            clear_cache_app()
        except Exception as e:
            print("Erreur save checklist:", e)

# --- GENERATION PDF ---
class PDF(FPDF):
    def header(self):
        if os.path.exists("logo_chu.png"): self.image("logo_chu.png", 170, 8, 30)
        if os.path.exists("logo_service.png"): self.image("logo_service.png", 10, 8, 30)
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
    pdf.multi_cell(0, 10, f"Checkliste faite le {date_fmt} par {user}.\nStatut : VALIDÉE (Conforme)", align='C')
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
        pdf.cell(90, 8, nom, 1)
        pdf.cell(30, 8, str(item.get('Tiroir', '')), 1, 0, 'C')
        pdf.cell(30, 8, str(item.get('Dotation', '')), 1, 0, 'C')
        pdf.cell(30, 8, "[X] OK", 1, 1, 'C')
    
    pdf.ln(5)
    pdf.set_font("Helvetica", 'B', 9)
    pdf.cell(0, 8, "Sécurité : Tiroirs verrouillés + Clé/Ciseaux attachés [X] OUI", 0, 1, 'L')
    
    pdf_str = pdf.output(dest='S')
    if isinstance(pdf_str, str): return pdf_str.encode('latin-1', 'replace')
    return bytes(pdf_str)

# --- AUTHENTIFICATION ROBUSTE ---
def check_login(username, password):
    if db is None: return None, "DB_NONE"
    if not username: return None, "Vide"
    try:
        u, p = str(username).strip(), str(password).strip()
        user_doc, user_data = None, {}
        
        # 1. ID Doc
        doc = db.collection("UTILISATEURS").document(u).get()
        if doc.exists:
            user_doc, user_data = doc, doc.to_dict()
        else:
            # 2. Username
            docs = list(db.collection("UTILISATEURS").where("username", "==", u).limit(1).stream())
            if docs:
                user_doc, user_data = docs[0], docs[0].to_dict()
            else:
                # 3. Identifiant
                docs = list(db.collection("UTILISATEURS").where("identifiant", "==", u).limit(1).stream())
                if docs: user_doc, user_data = docs[0], docs[0].to_dict()
        
        if not user_doc: return None, "Utilisateur introuvable"
        
        # Recherche mot de passe flexible
        pwd_db = ""
        for champ in ["password", "mdp", "pass", "code", "motdepasse"]:
            if champ in user_data:
                pwd_db = str(user_data[champ]).strip()
                break
        
        if pwd_db == p: return user_data, None
        return None, "Mot de passe incorrect"
    except Exception as e:
        return None, str(e)

def login_page():
    st.markdown("<h1 style='text-align: center;'>🚑 Chariot Urgence</h1>", unsafe_allow_html=True)
    if db is None:
        st.error("Base de données non connectée.")
        st.stop()
    
    c1, c2, c3 = st.columns([1, 2, 1])
    with c2:
        with st.form("login_form"):
            user_id = st.text_input("Identifiant")
            pwd = st.text_input("Mot de passe", type="password")
            if st.form_submit_button("SE CONNECTER", type="primary"):
                user_info, err = check_login(user_id, pwd)
                if user_info:
                    st.session_state['logged_in'] = True
                    st.session_state['user_id'] = str(user_id).strip()
                    display = f"{user_info.get('prenom','')} {user_info.get('nom','')}".strip()
                    st.session_state['user'] = display or str(user_id).strip()
                    st.session_state['role'] = user_info.get('role', 'Utilisateur')
                    st.rerun()
                else:
                    st.error(f"Erreur : {err}")

# --- INTERFACES ---
def maj_panier():
    for key in list(st.session_state.keys()):
        if key.startswith("input_"):
            item_id = key.replace("input_", "", 1)
            try: qty = int(st.session_state.get(key, 0))
            except: qty = 0
            if qty > 0: st.session_state['panier'][item_id] = qty
            elif item_id in st.session_state['panier']: del st.session_state['panier'][item_id]

def afficher_ligne_conso(row):
    try: s, d = int(row.get('Stock_Actuel', 0)), int(row.get('Dotation', 0))
    except: s, d = 0, 0
    color = "red" if s < d else "green"
    
    with st.container(border=True):
        c1, c2, c3, c4 = st.columns([3, 1, 0.3, 1.5])
        c1.markdown(f"**{row.get('Nom', 'Inconnu')}**")
        c2.markdown(f":{color}[**{s}/{d}**]")
        c3.markdown("📉")
        curr = st.session_state['panier'].get(row['ID'], 0)
        st.number_input("Qté", min_value=0, max_value=max(s, 0), value=min(curr, max(s, 0)), 
                        key=f"input_{row['ID']}", label_visibility="collapsed", on_change=maj_panier, step=1)

def interface_consommateur():
    st.header("💊 Consommation")
    df = get_inventaire_cached() # UTILISE LE CACHE
    if df.empty:
        st.info("Inventaire vide ou erreur lecture.")
        return

    if st.session_state.get('panier'):
        st.markdown(f"""<div class="panier-box">🛒 PANIER : {sum(st.session_state['panier'].values())}</div>""", unsafe_allow_html=True)
        with st.expander("✅ VALIDER", expanded=True):
            ip = st.text_input("🏥 IP PATIENT")
            user_f = st.session_state['user']
            if st.session_state.get('user_id') in SHARED_ACCOUNTS:
                user_f = st.text_input("👤 Votre Nom (Obligatoire)", key="u_conso")
            
            c1, c2 = st.columns([2, 1])
            if c1.button("🚀 ENREGISTRER", type="primary"):
                if ip and user_f:
                    if valider_panier(st.session_state['panier'], ip, user_f):
                        st.session_state['panier'] = {}
                        st.success("Enregistré !"); time.sleep(0.5); st.rerun()
                    else: st.error("Erreur technique.")
                else: st.error("IP et Nom obligatoires.")
            if c2.button("🗑️ Vider"):
                st.session_state['panier'] = {}; st.rerun()

    rech = st.text_input("🔍 Rechercher...")
    if rech:
        for _, r in df[df['Nom'].astype(str).str.contains(rech, case=False, na=False)].iterrows(): afficher_ligne_conso(r)
    else:
        for t in ["Dessus", "Tiroir 1", "Tiroir 2", "Tiroir 3", "Tiroir 4", "Tiroir 5"]:
            sub = df[df['Tiroir'] == t]
            if not sub.empty:
                with st.expander(f"🗄️ {t}"):
                    for _, r in sub.iterrows(): afficher_ligne_conso(r)

def interface_remplacement():
    st.header("🔄 Remplacer")
    data_logs = get_logs_remplacement_cached() # UTILISE LE CACHE
    
    if not data_logs:
        st.success("Tout est à jour ! (Aucun produit manquant)")
        return

    for l in data_logs:
        log_id = l['id_doc']
        d_str = l.get('Date').strftime("%d/%m %H:%M") if l.get('Date') else "?"
        
        with st.container(border=True):
            st.markdown(f"📅 **{d_str}** | 👤 {l.get('Utilisateur')} | IP: **{l.get('IP_Patient')}**")
            with st.form(key=f"f_{log_id}"):
                to_repl = []
                for item in l.get('Details_Struct', []):
                    nom = f"{item.get('Qte')}x {item.get('Nom')}"
                    if item.get('EstRemplace'): st.markdown(f"~~✅ {nom}~~")
                    else:
                        if st.checkbox(f"🔴 {nom}", key=f"c_{log_id}_{item.get('ID')}"): to_repl.append(item.get('ID'))
                
                uf = st.session_state['user']
                if st.session_state.get('user_id') in SHARED_ACCOUNTS: uf = st.text_input("Votre Nom", key=f"ur_{log_id}")
                
                if st.form_submit_button("💾 Valider Remplacement"):
                    if to_repl and uf:
                        effectuer_remplacement_partiel(log_id, l, to_repl, uf)
                        st.success("Enregistré"); time.sleep(0.5); st.rerun()
                    else: st.warning("Cochez des items et mettez votre nom.")

def interface_historique():
    st.header("📜 Historique Global")
    raw_data = get_historique_cached(50) # UTILISE LE CACHE
    if not raw_data:
        st.info("Aucun historique.")
        return

    clean_data = []
    for l in raw_data:
        det = ", ".join([f"{i.get('Qte')}x {i.get('Nom')}" for i in l.get('Details_Struct', [])])
        st_txt = "🟢 Remplacé" if "Remplac" in l.get('Statut','') else "🟠 En cours"
        clean_data.append({
            "Date": l.get('Date').strftime("%d/%m %H:%M") if l.get('Date') else "?",
            "User": l.get('Utilisateur'), "IP": l.get('IP_Patient'),
            "Details": det, "Statut": st_txt, "ID": l['id_doc'], "Suppr": False
        })
    
    df = pd.DataFrame(clean_data)
    cfg = {"Details": st.column_config.TextColumn("Détail", width="large"), "ID": None}
    
    if st.session_state.get('user_id') == 'admin':
        res = st.data_editor(df, column_config=cfg, hide_index=True, use_container_width=True, disabled=["Date","User","IP","Details","Statut"])
        to_del = res[res["Suppr"] == True]
        if not to_del.empty and st.button("Confirmer Suppression"):
            for _, r in to_del.iterrows(): supprimer_log(r['ID'])
            st.rerun()
    else:
        st.dataframe(df.drop(columns=["Suppr", "ID"]), column_config=cfg, hide_index=True, use_container_width=True)

def interface_checklist():
    st.header("📋 Checkliste")
    
    # Historique Checklists
    if db:
        try:
            # On lit l'historique direct (faible volume)
            checks = list(db.collection("CHECKLISTS").order_by("Date", direction=firestore.Query.DESCENDING).limit(5).stream())
            if checks:
                opts = [f"{c.to_dict().get('Date').strftime('%d/%m %H:%M')} - {c.to_dict().get('Utilisateur')}" for c in checks]
                sel = st.selectbox("Archives", opts)
                if st.button("📄 PDF Archive"):
                    idx = opts.index(sel)
                    d = checks[idx].to_dict()
                    pdf = generer_pdf_checklist(d.get('Contenu',[]), d.get('Utilisateur'), d.get('Date'))
                    st.download_button("Télécharger", data=pdf, file_name="Arch.pdf", mime="application/pdf")
        except: pass

    st.divider()
    # Blocage si stock pas à jour
    logs_missing = get_logs_remplacement_cached()
    if logs_missing:
        st.error(f"⛔ Impossible : Il reste {len(logs_missing)} dossiers de consommation non remplacés.")
        return

    df = get_inventaire_cached()
    if df.empty: return

    # Logique de validation par lots
    for t in ["Dessus", "Tiroir 1", "Tiroir 2", "Tiroir 3", "Tiroir 4", "Tiroir 5"]:
        sub = df[df['Tiroir'] == t]
        if not sub.empty:
            with st.expander(f"🗄️ {t}", expanded=True):
                if st.button(f"✅ Valider {t}", key=f"b_{t}"):
                    for _, r in sub.iterrows():
                        st.session_state['check_state'][r['ID']] = "OK"
                        st.session_state[f"rad_{r['ID']}"] = "Conforme"
                    st.rerun()
                
                for _, r in sub.iterrows():
                    c1, c2, c3 = st.columns([3, 1, 2])
                    c1.markdown(f"**{r['Nom']}**")
                    c2.markdown(f"Dot: {r['Dotation']}")
                    
                    curr = st.session_state.get(f"rad_{r['ID']}", "Conforme")
                    idx = ["Conforme", "Manquant"].index(curr) if curr in ["Conforme", "Manquant"] else 0
                    stat = c3.radio(f"s_{r['ID']}", ["Conforme", "Manquant"], index=idx, key=f"rad_{r['ID']}", horizontal=True, label_visibility="collapsed")
                    
                    if stat == "Manquant":
                        st.session_state['check_state'][r['ID']] = "KO"
                        if c3.button("🔄 Fix", key=f"fx_{r['ID']}"): st.toast("Noté")
                    else:
                        st.session_state['check_state'][r['ID']] = "OK"

    # Validation Finale
    all_ids = df['ID'].tolist()
    missing = sum(1 for i in all_ids if st.session_state['check_state'].get(i) == "KO")
    pending = sum(1 for i in all_ids if st.session_state['check_state'].get(i) not in ["OK", "KO"])

    st.divider()
    if pending == 0 and missing == 0:
        st.markdown("<div class='security-box'><h5>🔒 Sécurisation</h5>", unsafe_allow_html=True)
        secu = st.checkbox("✅ Je confirme : Verrouillage + Clé + Ciseaux OK")
        st.markdown("</div>", unsafe_allow_html=True)
        
        if secu:
            uf = st.session_state['user']
            if st.session_state.get('user_id') in SHARED_ACCOUNTS: uf = st.text_input("Votre Nom", key="uchk")
            
            if st.button("💾 VALIDER ET TERMINER", type="primary"):
                if uf:
                    export = [{"Nom": r['Nom'], "Tiroir": r['Tiroir'], "Dotation": r['Dotation']} for _, r in df.iterrows()]
                    save_checklist_history(uf, export)
                    st.session_state['pdf_ready'] = generer_pdf_checklist(export, uf, datetime.now())
                    st.success("Validé !"); st.balloons(); st.rerun()
                else: st.error("Nom requis")
            
            if st.session_state.get('pdf_ready'):
                st.download_button("📥 PDF Checkliste", data=st.session_state['pdf_ready'], file_name=f"Check_{datetime.now().strftime('%d%m')}.pdf", mime="application/pdf")
    else:
        if pending > 0: st.warning(f"Reste à voir : {pending}")
        if missing > 0: st.error(f"Manquants : {missing}")

# --- MAIN ---
def main():
    if not st.session_state['logged_in']:
        login_page()
    else:
        with st.sidebar:
            st.write(f"👤 {st.session_state.get('user')}")
            st.write(f"Role : {st.session_state.get('role')}")
            
            # BOUTON CRUCIAL POUR LE QUOTA : Permet de rafraichir manuellement si besoin
            if st.button("🔄 Actualiser les données"):
                clear_cache_app()
                st.rerun()
                
            if st.button("Déconnexion"):
                for k in list(st.session_state.keys()): del st.session_state[k]
                st.rerun()
            
            st.divider()
            nav = st.radio("Navigation", ["Consommation", "Remplacer", "Historique", "Checkliste"])

        if nav == "Consommation": interface_consommateur()
        elif nav == "Remplacer": interface_remplacement()
        elif nav == "Historique": interface_historique()
        elif nav == "Checkliste": interface_checklist()

if __name__ == "__main__":
    main()
