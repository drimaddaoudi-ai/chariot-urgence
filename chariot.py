import streamlit as st
import pandas as pd
import firebase_admin
from firebase_admin import credentials, firestore
from datetime import datetime
import time
from fpdf import FPDF
import os

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

# --- 1. BACKEND (FIRESTORE) ---
@st.cache_resource
def get_db():
    try:
        if not firebase_admin._apps:
            cred = credentials.Certificate("firestore_key.json")
            firebase_admin.initialize_app(cred)
        return firestore.client()
    except Exception as e:
        st.error(f"🚨 Erreur BDD: {e}")
        return None

db = get_db()

# --- 2. FONCTIONS MÉTIER ---

def get_inventaire_df():
    docs = db.collection("INVENTAIRE").stream()
    items = []
    for doc in docs:
        data = doc.to_dict()
        data['ID'] = doc.id
        items.append(data)
    if not items: return pd.DataFrame()
    
    df = pd.DataFrame(items)
    if 'ID' in df.columns:
        df = df.drop_duplicates(subset=['ID'], keep='first')
        df = df.sort_values(by='ID')
    return df

def maj_panier():
    for key in st.session_state:
        if key.startswith("input_"):
            item_id = key.replace("input_", "", 1)
            qty = st.session_state[key]
            if qty > 0:
                st.session_state['panier'][item_id] = qty
            elif item_id in st.session_state['panier']:
                del st.session_state['panier'][item_id]

def valider_panier(panier, ip, utilisateur):
    batch = db.batch()
    details_list = [] 
    details_texte = []
    for item_id, qte in panier.items():
        doc_ref = db.collection("INVENTAIRE").document(item_id)
        doc = doc_ref.get()
        if doc.exists:
            data = doc.to_dict()
            nom = data.get('Nom', 'Inconnu')
            stock_actuel = int(data.get('Stock_Actuel', 0))
            tiroir = data.get('Tiroir', '?')
            nouveau_stock = max(0, stock_actuel - int(qte))
            batch.update(doc_ref, {"Stock_Actuel": nouveau_stock})
            details_texte.append(f"{qte}x {nom}")
            details_list.append({"ID": item_id, "Nom": nom, "Qte": qte, "Tiroir": tiroir, "EstRemplace": False})

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

def effectuer_remplacement_partiel(log_id, log_data, items_coches, user_remplacant):
    batch = db.batch()
    items_struct = log_data.get('Details_Struct', [])
    tout_est_remplace = True
    nouveaux_items_struct = []
    items_modifies_noms = []

    for item in items_struct:
        item_id = item['ID']
        if item.get('EstRemplace', False):
            nouveaux_items_struct.append(item)
            continue
        if item_id in items_coches:
            qte_a_rendre = item['Qte']
            doc_ref = db.collection("INVENTAIRE").document(item_id)
            doc = doc_ref.get()
            if doc.exists:
                current = doc.to_dict()
                stock_now = int(current.get('Stock_Actuel', 0))
                dotation = int(current.get('Dotation', 0))
                nouveau_stock = min(dotation, stock_now + qte_a_rendre)
                batch.update(doc_ref, {"Stock_Actuel": nouveau_stock})
            item['EstRemplace'] = True
            items_modifies_noms.append(item['Nom'])
        else:
            tout_est_remplace = False
        nouveaux_items_struct.append(item)

    log_ref = db.collection("LOGS").document(log_id)
    updates = {"Details_Struct": nouveaux_items_struct}
    trace = {"Date": datetime.now(), "User": user_remplacant, "Items": items_modifies_noms}
    histo = log_data.get('Historique_Remplacements', [])
    histo.append(trace)
    updates["Historique_Remplacements"] = histo

    if tout_est_remplace:
        updates["Statut"] = "Remplacé"
        updates["Date_Remplacement"] = datetime.now()
        updates["Utilisateur_Remplacement"] = user_remplacant
    
    batch.update(log_ref, updates)
    batch.commit()
    return tout_est_remplace

def supprimer_log(log_id):
    db.collection("LOGS").document(log_id).delete()

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
    pdf.multi_cell(0, 10, f"La checkliste de Chariot d'Urgence RME a été faite le {date_fmt} par l'utilisateur {user}.\nStatut : VALIDÉE (Conforme)", align='C')
    pdf.ln(10)
    
    pdf.set_fill_color(200, 220, 255)
    pdf.set_font("Helvetica", 'B', 10)
    pdf.cell(90, 10, "Matériel", 1, 0, 'C', True)
    pdf.cell(30, 10, "Tiroir", 1, 0, 'C', True)
    pdf.cell(30, 10, "Dotation", 1, 0, 'C', True)
    pdf.cell(30, 10, "État", 1, 1, 'C', True)
    
    pdf.set_font("Helvetica", size=9)
    for item in data_checklist:
        try:
            nom = item['Nom'].encode('latin-1', 'replace').decode('latin-1')
        except:
            nom = item['Nom']
            
        tiroir = str(item['Tiroir'])
        dotation = str(item['Dotation'])
        
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
    
    return bytes(pdf.output())

# --- AUTHENTIFICATION ---
def check_login(username, password):
    """Vérifie le login dans Firestore"""
    doc_ref = db.collection("UTILISATEURS").document(username)
    doc = doc_ref.get()
    
    if doc.exists:
        data = doc.to_dict()
        if data['password'] == password:
            return data # Retourne les infos (Nom, Rôle...)
    return None

def login_page():
    st.image("https://cdn-icons-png.flaticon.com/512/3063/3063176.png", width=80)
    st.title("Chariot Urgence")
    with st.form("login"):
        user_id = st.text_input("Identifiant")
        pwd = st.text_input("Mot de passe", type="password")
        if st.form_submit_button("Se Connecter"):
            user_info = check_login(user_id, pwd)
            if user_info:
                st.session_state['logged_in'] = True
                # On stocke le vrai nom complet pour l'affichage
                st.session_state['user'] = f"{user_info['prenom']} {user_info['nom']}"
                st.session_state['role'] = user_info['role']
                # Init variables
                if 'panier' not in st.session_state: st.session_state['panier'] = {}
                if 'check_state' not in st.session_state: st.session_state['check_state'] = {}
                st.rerun()
            else:
                st.error("Identifiant ou mot de passe incorrect.")

# --- INTERFACE CONSOMMATION ---
def afficher_ligne_conso(row):
    try: stock = int(row['Stock_Actuel']); dotation = int(row['Dotation'])
    except: stock, dotation = 0, 0
    
    if stock < dotation: couleur = "red"
    else: couleur = "green"
    
    text_stock = f":{couleur}[**{stock}/{dotation}**]"
    cat = str(row['Categorie'])
    infos = f"| {cat}" if cat.lower() != 'nan' and cat.strip() != "" else ""
    
    with st.container(border=True):
        c1, c2, c3, c4 = st.columns([3, 1, 0.3, 1.5])
        with c1: 
            st.markdown(f"**{row['Nom']}**")
            if infos: st.caption(infos)
        with c2: st.markdown(text_stock)
        with c3: st.markdown("<div class='icon-text'>📉</div>", unsafe_allow_html=True) 
        with c4:
            valeur_actuelle = st.session_state['panier'].get(row['ID'], 0)
            st.number_input("Qté", min_value=0, max_value=stock, value=valeur_actuelle, key=f"input_{row['ID']}", label_visibility="collapsed", on_change=maj_panier, step=1)

def interface_consommateur():
    st.header(f"💊 Consommation")
    df = get_inventaire_df()
    if df.empty: return
    
    if st.session_state.get('panier'):
        nb_items = sum(st.session_state['panier'].values())
        st.markdown(f"""<div class="panier-box">🛒 PANIER : {nb_items} articles</div>""", unsafe_allow_html=True)
        with st.expander("✅ VALIDER LA CONSOMMATION", expanded=True):
            st.write("Récapitulatif :")
            for pid, q in st.session_state['panier'].items():
                nom_row = df[df['ID'] == pid]
                if not nom_row.empty: st.write(f"- {q} x **{nom_row['Nom'].values[0]}**")
            st.divider()
            ip_input = st.text_input("🏥 IP DU PATIENT", placeholder="Ex: 24/12345")
            c1, c2 = st.columns([2,1])
            if c1.button("🚀 ENREGISTRER", type="primary"):
                if not ip_input: st.error("⚠️ IP obligatoire")
                else:
                    valider_panier(st.session_state['panier'], ip_input, st.session_state['user'])
                    st.session_state['panier'] = {}
                    for k in list(st.session_state.keys()):
                        if k.startswith("input_"): del st.session_state[k]
                    time.sleep(1); st.success("Enregistré !"); st.rerun()
            if c2.button("🗑️ Vider"):
                st.session_state['panier'] = {}
                for k in list(st.session_state.keys()):
                    if k.startswith("input_"): del st.session_state[k]
                st.rerun()
    
    recherche = st.text_input("🔍 Rechercher...")
    if recherche:
        masque = df['Nom'].str.contains(recherche, case=False, na=False)
        for _, row in df[masque].iterrows(): afficher_ligne_conso(row)
    else:
        for tiroir in ["Dessus", "Tiroir 1", "Tiroir 2", "Tiroir 3", "Tiroir 4", "Tiroir 5"]:
            if tiroir in df['Tiroir'].unique():
                with st.expander(f"🗄️ {tiroir}"):
                    for _, row in df[df['Tiroir'] == tiroir].iterrows(): afficher_ligne_conso(row)

# --- INTERFACE REMPLACEMENT ---
def interface_remplacement():
    st.header("🔄 Remplacer")
    logs_ref = db.collection("LOGS").order_by("Date", direction=firestore.Query.DESCENDING).stream()
    count = 0
    for doc in logs_ref:
        l = doc.to_dict(); 
        if l.get("Statut") != "Non remplacé": continue
        count += 1
        log_id = doc.id
        date_str = l['Date'].strftime("%d/%m/%Y %H:%M") if l.get('Date') else "?"
        with st.container(border=True):
            st.markdown(f"📅 **{date_str}** | 👤 {l.get('Utilisateur')} | 🏥 IP: **{l.get('IP_Patient')}**")
            st.divider()
            with st.form(key=f"form_repl_{log_id}"):
                to_repl = []
                for item in l.get('Details_Struct', []):
                    nom = f"{item['Qte']}x {item['Nom']}"
                    if item.get('EstRemplace', False): st.markdown(f"~~✅ {nom}~~")
                    else:
                        if st.checkbox(f"🔴 {nom}", key=f"chk_{log_id}_{item['ID']}"): to_repl.append(item['ID'])
                if st.form_submit_button("💾 Valider"):
                    if not to_repl: st.warning("Rien coché")
                    else:
                        fin = effectuer_remplacement_partiel(log_id, l, to_repl, st.session_state['user'])
                        st.success("Fait !" if fin else "Partiel enregistré"); time.sleep(1); st.rerun()
    if count == 0: st.success("Tout est à jour !")

# --- INTERFACE HISTORIQUE ---
def interface_historique():
    st.header("📜 Historique de consommation")
    logs_ref = db.collection("LOGS").order_by("Date", direction=firestore.Query.DESCENDING).limit(50).stream()
    data = []
    for doc in logs_ref:
        l = doc.to_dict(); log_id = doc.id
        statut = l.get('Statut', 'Inconnu')
        if statut == "Non remplacé": st_fmt = "🟠 En cours"; remp = ""
        else: 
            d = l.get('Date_Remplacement'); ds = d.strftime("%d/%m %H:%M") if d else "?"
            st_fmt = "🟢 Remplacé"; remp = f"{l.get('Utilisateur_Remplacement')} ({ds})"
        det = "\n".join([f"{'✅' if i.get('EstRemplace') else '🔴'} {i['Qte']}x {i['Nom']}" for i in l.get('Details_Struct', [])])
        data.append({"Date": l['Date'].strftime("%d/%m %H:%M"), "IP": l.get('IP_Patient'), "User": l.get('Utilisateur'), "Mat": det, "St": st_fmt, "Remp": remp, "ID": log_id, "Suppr": False})
    
    if data:
        df = pd.DataFrame(data)
        cfg = {"Mat": st.column_config.TextColumn("Détail", width="large"), "ID": None}
        if st.session_state['user'] == 'admin':
            res = st.data_editor(df, column_config=cfg, hide_index=True, use_container_width=True, disabled=["Date","IP","User","Mat","St","Remp"])
            to_del = res[res["Suppr"]==True]
            if not to_del.empty and st.button("🗑️ Confirmer suppression"):
                for _, r in to_del.iterrows():
                    if "🟢" in r['St']: supprimer_log(r['ID'])
                    else: st.error("Impossible suppr. dossier en cours")
                st.rerun()
        else: st.dataframe(df.drop(columns=["Suppr", "ID"]), column_config=cfg, hide_index=True, use_container_width=True)

# --- INTERFACE CHECKLISTE ---
def verifier_blocage_checklist():
    logs_ref = db.collection("LOGS").where("Statut", "==", "Non remplacé").stream()
    items_manquants = []
    for doc in logs_ref: items_manquants.append(doc.id)
    return len(items_manquants) > 0

def save_checklist_history(user, data_items):
    doc_data = {"Date": datetime.now(), "Utilisateur": user, "Statut": "Validé", "Contenu": data_items, "Securite_Verrou": True, "Securite_Attache": True}
    db.collection("CHECKLISTS").add(doc_data)

def interface_checklist():
    st.header("📋 Checkliste de Vérification")
    
    with st.expander("📂 Consulter les anciennes checklists (Historique)", expanded=False):
        checks = db.collection("CHECKLISTS").order_by("Date", direction=firestore.Query.DESCENDING).limit(10).stream()
        history_data = []
        history_map = {} 
        
        for c in checks:
            d = c.to_dict()
            date_str = d['Date'].strftime("%d/%m/%Y %H:%M")
            label = f"{date_str} - {d['Utilisateur']}"
            entry_data = d.copy(); entry_data['ID'] = c.id
            history_data.append({"Label": label, "Date": date_str, "User": d['Utilisateur']})
            history_map[label] = entry_data
            
        if not history_data:
            st.info("Aucune ancienne checkliste trouvée.")
        else:
            st.write("Sélectionnez une checkliste pour télécharger sa copie PDF :")
            selected_label = st.selectbox("Choisir une checkliste", [h["Label"] for h in history_data])
            
            c1, c2 = st.columns([3, 1])
            with c1:
                if st.button("📄 Régénérer le PDF de cette archive"):
                    sel_data = history_map[selected_label]
                    pdf_hist = generer_pdf_checklist(sel_data['Contenu'], sel_data['Utilisateur'], sel_data['Date'])
                    st.download_button(
                        label="📥 Télécharger PDF Archive",
                        data=pdf_hist,
                        file_name=f"Archive_Checklist_{sel_data['Date'].strftime('%Y%m%d')}.pdf",
                        mime="application/pdf"
                    )
            if st.session_state['user'] == 'admin':
                with c2:
                    st.markdown("<br>", unsafe_allow_html=True)
                    if st.button("🗑️ Supprimer", type="primary"):
                        doc_id_to_del = history_map[selected_label]['ID']
                        db.collection("CHECKLISTS").document(doc_id_to_del).delete()
                        st.toast("Supprimé !")
                        time.sleep(1)
                        st.rerun()
    
    st.divider()

    if verifier_blocage_checklist():
        st.error("⛔ ACTIONS REQUISES AVANT CHECKLISTE")
        st.warning("Il y a du matériel 'Non Remplacé'. Régularisez le stock d'abord.")
        return

    st.success("✅ Stock OK. Vérification en cours.")
    df = get_inventaire_df()
    if df.empty: return

    if 'check_state' not in st.session_state: st.session_state['check_state'] = {}

    for tiroir in ["Dessus", "Tiroir 1", "Tiroir 2", "Tiroir 3", "Tiroir 4", "Tiroir 5"]:
        if tiroir in df['Tiroir'].unique():
            with st.expander(f"🗄️ {tiroir}", expanded=True):
                for _, row in df[df['Tiroir'] == tiroir].iterrows():
                    iid = row['ID']
                    c1, c2, c3 = st.columns([3, 1, 2])
                    with c1: st.markdown(f"**{row['Nom']}**")
                    with c2: st.markdown(f"Dot: **{row['Dotation']}**")
                    with c3:
                        status = st.radio(f"S_{iid}", ["Conforme", "Manquant"], key=f"rad_{iid}", horizontal=True, label_visibility="collapsed", index=None)
                        if status == "Manquant":
                            st.session_state['check_state'][iid] = "KO"
                            if st.button(f"🔄 Remplacer ({row['Nom']})", key=f"fix_{iid}"): st.toast("Noté")
                        elif status == "Conforme": st.session_state['check_state'][iid] = "OK"
                        else: st.session_state['check_state'][iid] = "PENDING"

    st.divider()
    
    all_ok = True; missing_count = 0; pending_count = 0
    for iid in df['ID'].tolist():
        state = st.session_state['check_state'].get(iid, "PENDING")
        if state == "KO": all_ok = False; missing_count += 1
        elif state == "PENDING": all_ok = False; pending_count += 1
            
    if all_ok:
        st.markdown("<div class='security-box'>", unsafe_allow_html=True)
        st.markdown("### 🔒 Sécurisation Finale")
        st.markdown("Avez-vous verrouillé les tiroirs par la clé et attaché la clé et paires ciseaux par ziplock au dessus du chariot ?")
        securite_confirmee = st.checkbox("✅ OUI, je confirme avoir sécurisé le chariot (Clé + Ciseaux + Verrouillage)")
        st.markdown("</div>", unsafe_allow_html=True)
        
        if securite_confirmee:
            if st.button("💾 VALIDER LA CHECKLISTE & PDF", type="primary"):
                checklist_export = []
                for _, row in df.iterrows():
                    checklist_export.append({"Nom": row['Nom'], "Tiroir": row['Tiroir'], "Dotation": row['Dotation']})
                save_checklist_history(st.session_state['user'], checklist_export)
                pdf_bytes = generer_pdf_checklist(checklist_export, st.session_state['user'], datetime.now())
                st.download_button("📄 Télécharger le Rapport PDF", data=pdf_bytes, file_name=f"Checklist_{datetime.now().strftime('%Y%m%d')}.pdf", mime="application/pdf")
                st.balloons()
        else: st.warning("⚠️ Vous devez confirmer la sécurisation pour valider.")
    else:
        if pending_count > 0: st.warning(f"⚠️ Vous devez vérifier toutes les lignes ({pending_count} restants).")
        if missing_count > 0: st.error(f"⚠️ Il reste {missing_count} éléments manquants non remplacés.")
        st.button("💾 Valider", disabled=True)

# --- RESPONSABLE ---
def interface_responsable():
    tab1, tab2, tab3 = st.tabs(["📊 Stock", "📜 Historique Conso", "📋 Historique Checklists"])
    with tab1:
        st.header("État du stock")
        df = get_inventaire_df()
        if not df.empty: st.dataframe(df[['Nom', 'Tiroir', 'Stock_Actuel', 'Dotation']], use_container_width=True)
    with tab2: interface_historique()
    with tab3:
        st.header("Historique des Checklists")
        checks = db.collection("CHECKLISTS").order_by("Date", direction=firestore.Query.DESCENDING).limit(20).stream()
        data = []
        for c in checks:
            d = c.to_dict()
            data.append({"Date": d['Date'].strftime("%d/%m/%Y %H:%M"), "Utilisateur": d['Utilisateur'], "Statut": d['Statut']})
        if data: st.dataframe(pd.DataFrame(data), use_container_width=True)
        else: st.info("Aucune checkliste.")

# --- MENU PRINCIPAL ---
def main():
    if 'logged_in' not in st.session_state: st.session_state['logged_in'] = False
    if 'panier' not in st.session_state: st.session_state['panier'] = {}

    if not st.session_state['logged_in']:
        login_page()
    else:
        with st.sidebar:
            st.write(f"👤 {st.session_state['user']}")
            st.write(f"Role : {st.session_state['role']}")
            if st.button("Déconnexion"):
                st.session_state['logged_in'] = False; st.session_state['panier'] = {}; st.rerun()
            st.divider()
            
            menu_options = ["Consommation", "Remplacer", "Historique", "Checkliste"]
            choix = st.radio("Navigation", menu_options)
        
        if choix == "Consommation": interface_consommateur()
        elif choix == "Remplacer": interface_remplacement()
        elif choix == "Checkliste": interface_checklist()
        elif choix == "Historique": 
            if st.session_state['role'] in ["Responsable", "Administrateur", "SuperAdmin"]: interface_responsable()
            else: interface_historique()

if __name__ == "__main__":
    main()