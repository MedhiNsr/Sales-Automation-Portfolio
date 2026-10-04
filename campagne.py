"""Sales Automation 1.0. Local Windows / Outlook classic. No remote service."""
from __future__ import annotations
import csv
import io
import json
import os
import shutil
from pathlib import Path
import queue
import re
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
import tkinter as tk
from tkinter import ttk, messagebox, filedialog, simpledialog
from agent_sdr import CentralRegistry

ACCOUNT = 'sender@example.com'
SUBJECT = '[Sales Automation] - Suite à notre échange'
BODIES = ["""Bonjour {{prénom}},

Je viens de tenter de vous joindre sans succès.

Nous préparons actuellement la sélection et la validation technique de notre prochaine promotion de stagiaires Data, que nous proposons d’accueillir à certains de nos partenaires.

De votre côté, accueillir un étudiant en dernière année de master pour un stage de 6 mois, avec un démarrage entre janvier et mars 2027, pourrait-il avoir du sens ?

Ces stages n’engendrent aucun frais pour vous puisqu’ils sont intégralement pris en charge par ExampleCompany.

Quelles sont vos prochaines disponibilités pour en discuter ?

Merci et bonne journée,

Demo""", """Bonjour {{prénom}},

Avez-vous eu l’occasion de prendre connaissance de mon précédent mail ?

Un stage de 6 mois sur le volet Data, intégralement pris en charge par ExampleCompany, pourrait-il répondre à un besoin au sein de votre équipe ?

Quelles seraient vos prochaines disponibilités pour en discuter ?

Merci et bonne journée,

Demo""", """Bonjour {{prénom}},

Je reviens une dernière fois vers vous concernant notre prochaine promotion de stagiaires Data.

Est-ce un sujet que vous souhaitez explorer pour début 2027 ?

Merci pour votre retour et bonne journée,

Demo"""]
DEFAULTS = dict(subject=SUBJECT, bodies=BODIES, day2=5, day3=12, daily=100,
                start=7, end=20.5)
LABELS = {'ready':'À contacter', 'active':'En cours', 'done':'3 mails envoyés',
          'reply':'Réponse reçue / à examiner', 'excluded':'Exclu',
          'pending':'Envoi à confirmer', 'blocked':'Bloqué registre central'}


class CampaignRegistry:
    DEFAULT_NAME = 'Head of Data Luxembourg'

    def __init__(self, directory):
        self.directory = Path(directory)
        self.path = self.directory/'campaigns.json'
        self.data = self._load()

    def _load(self):
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding='utf-8'))
                if data.get('campaigns'):
                    return data
            except Exception:
                pass
        data = {
            'current': self.DEFAULT_NAME,
            'campaigns': {self.DEFAULT_NAME: 'campagne.sqlite3'},
        }
        self._save(data)
        return data

    def _save(self, data=None):
        self.path.write_text(json.dumps(data or self.data, ensure_ascii=False, indent=2), encoding='utf-8')

    def names(self):
        return sorted(self.data['campaigns'])

    def current(self):
        current = self.data.get('current')
        return current if current in self.data['campaigns'] else self.names()[0]

    def set_current(self, name):
        if name in self.data['campaigns']:
            self.data['current'] = name
            self._save()

    def db_path(self, name):
        return self.directory/self.data['campaigns'][name]

    def create(self, name):
        name = re.sub(r'\s+', ' ', (name or '').strip())
        if not name:
            raise ValueError('Nom de campagne vide.')
        if name in self.data['campaigns']:
            raise ValueError('Cette campagne existe déjà.')
        slug = re.sub(r'[^A-Za-z0-9_-]+', '_', name).strip('_').lower()[:50]
        if not slug:
            slug = 'campagne'
        filename = f'campagne_{slug}.sqlite3'
        existing = set(self.data['campaigns'].values())
        i = 2
        while filename in existing or (self.directory/filename).exists():
            filename = f'campagne_{slug}_{i}.sqlite3'
            i += 1
        self.data['campaigns'][name] = filename
        self.data['current'] = name
        self._save()
        return name

def now():
    return time.time()

def personalized(text, lead):
    for key in ('prénom', 'prenom', 'nom', 'entreprise', 'email'):
        text = text.replace('{{'+key+'}}', lead['prenom' if key == 'prénom' else key])
    if '{{' in text or '}}' in text:
        raise ValueError('Champ de personnalisation inconnu dans le message.')
    return text

def read_contacts(path):
    raw = Path(path).read_bytes()
    try:
        content = raw.decode('utf-8-sig')
    except UnicodeDecodeError:
        content = raw.decode('cp1252')
    dialect = csv.excel
    try:
        dialect = csv.Sniffer().sniff(content[:8192], delimiters=';,\t')
    except csv.Error:
        pass
    reader = csv.DictReader(io.StringIO(content), dialect=dialect)
    if not reader.fieldnames:
        raise ValueError('Fichier vide.')
    headers = [x.strip().lower().replace('prénom','prenom') for x in reader.fieldnames]
    if len(set(headers)) != len(headers) or not {'prenom','email'} <= set(headers):
        raise ValueError('Colonnes nécessaires : prenom et email. Colonnes facultatives : nom, entreprise.')
    reader.fieldnames = headers
    result = []
    for line, row in enumerate(reader, 2):
        if not any(row.values()):
            continue
        if None in row or any(v is None for v in row.values()):
            raise ValueError(f'Ligne {line} : nombre de colonnes incorrect.')
        item = {k:row.get(k,'').strip() for k in ('prenom','nom','entreprise','email')}
        item['email'] = item['email'].lower()
        if not item['prenom'] or not re.fullmatch(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+", item['email']):
            raise ValueError(f'Ligne {line} : prénom ou adresse email incorrect.')
        if any('\n' in v or '\r' in v or '{{' in v or '}}' in v for v in item.values()):
            raise ValueError(f'Ligne {line} : caractère interdit dans un champ.')
        result.append(item)
    return result

class Database:
    def __init__(self, path, campaign_name='', registry=None):
        self.c = sqlite3.connect(path, timeout=20)
        self.c.row_factory = sqlite3.Row
        self.campaign_name = campaign_name
        self.registry = registry
        self.c.execute('PRAGMA journal_mode=WAL')
        self.c.executescript('''
        CREATE TABLE IF NOT EXISTS config (id INTEGER PRIMARY KEY, data TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS leads (email TEXT PRIMARY KEY, prenom TEXT NOT NULL,
            nom TEXT NOT NULL, entreprise TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'ready',
            first REAL, last REAL, stage INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS messages (token TEXT PRIMARY KEY, email TEXT NOT NULL,
            stage INTEGER NOT NULL, created REAL NOT NULL, state TEXT NOT NULL,
            mid TEXT, sent REAL, UNIQUE(email,stage));
        CREATE TABLE IF NOT EXISTS events (stamp REAL NOT NULL, detail TEXT NOT NULL);
        ''')
        self._migrate()
        if not self.c.execute('SELECT 1 FROM config').fetchone():
            self.c.execute('INSERT INTO config VALUES(1,?)', (json.dumps(DEFAULTS),))
        self.c.commit()

    def _migrate(self):
        columns = {r[1] for r in self.c.execute('PRAGMA table_info(leads)')}
        if 'block_reason' not in columns:
            db_path = Path(self.c.execute('PRAGMA database_list').fetchone()[2])
            if db_path.exists():
                backup = db_path.with_suffix(db_path.suffix + '.bak_registry_migration')
                if not backup.exists():
                    shutil.copy2(db_path, backup)
            self.c.execute("ALTER TABLE leads ADD COLUMN block_reason TEXT NOT NULL DEFAULT ''")

    def close(self):
        self.c.close()

    def log(self, text):
        self.c.execute('INSERT INTO events VALUES(?,?)',(now(),text))
        self.c.commit()

    def config(self):
        return json.loads(self.c.execute('SELECT data FROM config WHERE id=1').fetchone()[0])

    def leads(self):
        return list(self.c.execute('SELECT * FROM leads ORDER BY prenom,email'))

    def add(self, rows):
        n = 0
        blocked = []
        with self.c:
            for row in rows:
                if self.registry and self.campaign_name:
                    decision = self.registry.reserve_for_campaign(row, self.campaign_name)
                    if not decision.allowed:
                        blocked.append((row['email'], decision.reason))
                        self.log(f"Import ignoré pour {row['email']} : {decision.reason}.")
                        continue
                inserted = self.c.execute('INSERT OR IGNORE INTO leads(email,prenom,nom,entreprise,block_reason) VALUES(?,?,?,?,?)',
                    tuple(row[k] for k in ('email','prenom','nom','entreprise')) + ('',)).rowcount
                if not inserted:
                    self.c.execute("UPDATE leads SET block_reason='' WHERE email=? AND status IN ('ready','active')", (row['email'],))
                n += inserted
        return (n, blocked) if self.registry else n

    def block(self, email, reason):
        with self.c:
            self.c.execute("UPDATE leads SET block_reason=? WHERE email=? AND status IN ('ready','active')", (reason, email))
        self.log(f'Envoi bloqué pour {email} : {reason}.')

    def clear_block(self, email):
        with self.c:
            self.c.execute("UPDATE leads SET block_reason='' WHERE email=?", (email,))

    def registry_decision(self, email):
        if not self.registry or not self.campaign_name:
            return None
        return self.registry.evaluate(email, self.campaign_name)
        return n

    def exclude(self, emails):
        with self.c:
            for email in emails:
                self.c.execute("UPDATE leads SET status='excluded' WHERE email=?",(email,))
                if self.registry:
                    self.registry.mark_excluded(email)
        self.log(f'{len(emails)} contact(s) exclu(s). Un nouvel import ne les réactive pas.')

    def reserve(self, email, stage):
        token = 'ExampleCompany-'+uuid.uuid4().hex
        with self.c:
            self.c.execute('INSERT INTO messages(token,email,stage,created,state) VALUES(?,?,?,?,?)',
                           (token,email,stage,now(),'reserved'))
        return token

    def confirm(self, row, mid, sent):
        with self.c:
            self.c.execute("UPDATE messages SET state='sent',mid=?,sent=? WHERE token=?",(mid,sent,row['token']))
            self.c.execute("UPDATE leads SET first=COALESCE(first,?),last=?,stage=?,status=CASE WHEN status IN ('reply','excluded') THEN status ELSE ? END WHERE email=?",
                           (sent,sent,row['stage'],'done' if row['stage']==3 else 'active',row['email']))
            if self.registry:
                if row['stage'] == 3:
                    self.registry.mark_sequence_done(row['email'], self.campaign_name, sent)
                else:
                    self.registry.mark_contacted(row['email'], self.campaign_name, row['stage'] > 1)
        self.log(f"Mail {row['stage']} confirmé dans les éléments envoyés : {row['email']}")

    def stop_reply(self, email):
        with self.c:
            changed = self.c.execute("UPDATE leads SET status='reply' WHERE email=? AND status NOT IN ('reply','excluded')",(email,)).rowcount
            if changed and self.registry:
                self.registry.mark_reply(email)
        if changed:
            self.log(f'Arrêt des relances : réponse ou message automatique reçu pour {email}. À examiner dans Outlook.')

def due(lead, cfg, stamp, test=False):
    if lead['status'] not in ('ready','active') or lead['stage']>=3:
        return False
    if lead['stage']==0:
        return True
    offsets = [0,2,4] if test else [0,cfg['day2'],cfg['day3']]
    unit = 60 if test else 86400
    # Never catch up two follow-ups in the same run after a long shutdown.
    gap = 60 if test else (cfg['day2'] if lead['stage']==1 else cfg['day3']-cfg['day2'])*86400
    return stamp >= max(lead['first']+offsets[lead['stage']]*unit, lead['last']+gap)

class Cancelled(Exception):
    pass

class Outlook:
    def __init__(self, cancel):
        import win32com.client
        self.w = win32com.client
        self.cancel = cancel
        self.app = self.w.gencache.EnsureDispatch('Outlook.Application')
        self.session = self.app.Session
        self.account = next((a for a in self.session.Accounts if self.account_smtp(a).lower()==ACCOUNT),None)
        if self.account is None:
            raise RuntimeError(f'Compte {ACCOUNT} introuvable dans Outlook classique.')
        self.store = self.account.DeliveryStore
        self.sent = self.store.GetDefaultFolder(5)

    @staticmethod
    def account_smtp(account):
        fields = [
            'http://schemas.microsoft.com/mapi/proptag/0x39FE001E',
            'http://schemas.microsoft.com/mapi/proptag/0x39FE001F',
        ]
        for field in fields:
            try:
                value = account.PropertyAccessor.GetProperty(field)
                if value:
                    return str(value)
            except Exception:
                pass
        try:
            user = account.CurrentUser
            for field in fields:
                try:
                    value = user.PropertyAccessor.GetProperty(field)
                    if value:
                        return str(value)
                except Exception:
                    pass
        except Exception:
            pass
        return ''

    def check(self):
        if self.cancel.is_set():
            raise Cancelled()
        if self.session.Offline:
            raise RuntimeError('Outlook est hors connexion. Campagne mise en pause.')

    def sync(self):
        import pythoncom
        self.check()
        class Events:
            def __init__(self):
                self.done = False
                self.error = None
            def OnSyncEnd(self):
                self.done = True
            def OnError(self, Code, Description):
                self.error = str(Description)
            OnOnError = OnError
        groups = self.session.SyncObjects
        if groups.Count==0:
            raise RuntimeError('Aucun groupe de synchronisation Outlook. Vérification nécessaire.')
        for i in range(1,groups.Count+1):
            group = groups.Item(i)
            handler = self.w.WithEvents(group,Events)
            try:
                group.Start()
                deadline = time.monotonic()+45
                while not handler.done:
                    self.check()
                    pythoncom.PumpWaitingMessages()
                    if handler.error:
                        raise RuntimeError('Synchronisation Outlook : '+handler.error)
                    if time.monotonic()>deadline:
                        raise RuntimeError('Synchronisation non confirmée en 45 secondes. Aucun nouvel envoi.')
                    time.sleep(.05)
                if handler.error:
                    raise RuntimeError('Synchronisation Outlook : '+handler.error)
            finally:
                handler.close()

    @staticmethod
    def props(item, codes):
        values = item.PropertyAccessor.GetProperties(['http://schemas.microsoft.com/mapi/proptag/'+c for c in codes])
        # MAPI_E_NOT_FOUND is normal for optional headers; other errors stop the scan.
        result = []
        for value in values:
            if isinstance(value,(str, datetime)):
                result.append(value)
            elif isinstance(value,int) and (value & 0xffffffff)==0x8004010f:
                result.append('')
            elif value is None:
                result.append('')
            else:
                raise RuntimeError('Impossible de lire une propriété Outlook : '+str(value))
        return result

    @staticmethod
    def filtered(folder, stamp, sent=False):
        field = 'http://schemas.microsoft.com/mapi/proptag/'+('0x00390040' if sent else '0x0E060040')
        date = datetime.fromtimestamp(stamp-120,timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
        items = folder.Items.Restrict('@SQL="'+field+'" >= '+chr(39)+date+chr(39))
        if items.Count>15000:
            raise RuntimeError('Trop de messages à vérifier dans un dossier. Aucun nouvel envoi.')
        return items

    def reconcile(self, db):
        pending = list(db.c.execute("SELECT * FROM messages WHERE state!='sent'"))
        if not pending:
            return True
        bytoken = {r['token']:r for r in pending}
        matches = {}
        for item in self.filtered(self.sent,min(r['created'] for r in pending),True):
            self.check()
            if item.Class!=43:
                continue
            token = item.BillingInformation
            if token in bytoken:
                if token in matches:
                    raise RuntimeError('Plusieurs messages portent le même identifiant. Vérifier les envois dans Outlook.')
                mid = self.props(item,['0x1035001F'])[0]
                if not mid:
                    raise RuntimeError('Identifiant du mail envoyé indisponible. Aucun nouvel envoi.')
                sent_value = self.props(item,['0x00390040'])[0]
                sent_stamp = sent_value.timestamp() if hasattr(sent_value, 'timestamp') else item.SentOn.timestamp()
                matches[token] = (mid, sent_stamp)
        for token,(mid,stamp) in matches.items():
            db.confirm(bytoken[token],mid,stamp)
        missing = [r for r in pending if r['token'] not in matches]
        if any(now()-r['created']>180 for r in missing):
            raise RuntimeError('Un envoi reste incertain. Vérifier Brouillons, Boîte d’envoi et Éléments envoyés. Aucun renvoi automatique ; conserver le journal.')
        return not missing

    def scan(self, db):
        leads = {r['email']:r for r in db.leads() if r['first'] is not None and r['status'] not in ('reply','excluded')}
        if not leads:
            return
        mids = {r['mid']:r['email'] for r in db.c.execute("SELECT * FROM messages WHERE state='sent'") if r['mid']}
        since = min(r['first'] for r in leads.values())
        skip = {self.store.GetDefaultFolder(n).EntryID for n in (4,5,16)}
        # Search folders are virtual; enumerate real mailbox folders instead.
        skip.update(f.EntryID for f in self.store.GetSearchFolders())
        folders = list(self.store.GetRootFolder().Folders)
        count = 0
        while folders:
            self.check()
            folder = folders.pop()
            if folder.EntryID in skip:
                continue
            folders.extend(list(folder.Folders))
            if folder.DefaultItemType!=0:
                continue
            count += 1
            for item in self.filtered(folder,since):
                self.check()
                if item.Class not in (43,46):
                    continue
                mid,parent,refs = self.props(item,['0x1035001F','0x1042001F','0x1039001F'])
                if mid and mid in mids:
                    continue  # Own original, especially during a self-test.
                linked = {mids[x] for x in re.findall(r'<[^<>]+>',parent+' '+refs) if x in mids}
                sender = ''
                if item.Class==43:
                    sender = next((s for s in self.props(item,['0x5D01001F','0x0C1F001F']) if s), '')
                    sender = sender.lower()
                    received = self.props(item,['0x0E060040'])[0]
                    received_stamp = received.timestamp() if hasattr(received, 'timestamp') else 0
                    if sender != ACCOUNT and sender in leads and received_stamp >= leads[sender]['first']:
                        linked.add(sender)
                for email in linked:
                    if email in leads:
                        db.stop_reply(email)
        if count==0:
            raise RuntimeError('Aucun dossier de courrier vérifié. Aucun nouvel envoi.')

    def create(self, lead, stage, cfg, parent='', token=''):
        mail = self.app.CreateItem(0)
        mail.SendUsingAccount = self.account
        mail.SaveSentMessageFolder = self.sent
        mail.DeleteAfterSubmit = False
        mail.To = lead['email']
        mail.Subject = personalized(cfg['subject'], lead) if stage == 1 else 'RE: ' + personalized(cfg['subject'], lead)
        mail.Body = personalized(cfg['bodies'][stage-1], lead)
        if parent:
            mail.PropertyAccessor.SetProperty('http://schemas.microsoft.com/mapi/proptag/0x1042001F', parent)
            mail.PropertyAccessor.SetProperty('http://schemas.microsoft.com/mapi/proptag/0x1039001F', parent)
        if token:
            mail.BillingInformation = token
        return mail

def cycle(db, outlook, cancel, test, send=False):
    outlook.sync()
    confirmed = outlook.reconcile(db)
    outlook.scan(db)
    if not send or not confirmed:
        return
    cfg = db.config()
    local = datetime.now()
    current_hour = local.hour + local.minute / 60
    if not test and (local.weekday()>4 or not cfg['start']<=current_hour<cfg['end']):
        return
    midnight = local.replace(hour=0,minute=0,second=0,microsecond=0).timestamp()
    if db.c.execute('SELECT COUNT(*) FROM messages WHERE created>=?',(midnight,)).fetchone()[0] >= (3 if test else cfg['daily']):
        return
    for lead in db.leads():
        if not due(lead,cfg,now(),test):
            continue
        if test and lead['email']!=ACCOUNT:
            raise RuntimeError('Le mode test ne peut envoyer qu’à ta propre adresse.')
        decision = db.registry_decision(lead['email'])
        if decision and not decision.allowed:
            db.block(lead['email'], decision.reason)
            continue
        db.clear_block(lead['email'])
        outlook.check()
        if cancel.is_set():
            raise Cancelled()
        stage = lead['stage']+1
        parent = db.c.execute("SELECT mid FROM messages WHERE email=? AND state='sent' ORDER BY stage DESC LIMIT 1",(lead['email'],)).fetchone()
        # Construct and validate before reserving. Commit intent before calling Send.
        mail = outlook.create(lead,stage,cfg,parent[0] if parent else '')
        if cancel.is_set():
            raise Cancelled()
        token = db.reserve(lead['email'],stage)
        mail.BillingInformation = token
        mail.Save()
        outlook.check()
        mail.Send()
        with db.c:
            db.c.execute("UPDATE messages SET state='submitted' WHERE token=?",(token,))
        if db.registry:
            db.registry.mark_contacted(lead['email'], db.campaign_name, stage > 1)
        db.log(f"Mail {stage} confié à Outlook pour {lead['email']}. Confirmation d’envoi en attente.")
        return  # At most one send each minute, preceded by a new scan.

class App:
    def __init__(self, root, directory):
        self.root,self.directory = root,directory
        self.campaigns = CampaignRegistry(directory)
        self.registry = CentralRegistry(directory/'central_registry.sqlite3')
        self.test = True
        self.running = self.busy = False
        self.cancel = threading.Event()
        self.results = queue.Queue()
        self.root.title('ExampleCompany | Prospection Data 2027')
        self.root.geometry('1120x780')
        self.root.minsize(900,650)
        style = ttk.Style()
        style.theme_use('clam')
        style.configure('TButton',padding=7)
        style.configure('Title.TLabel',font=('Segoe UI',19,'bold'))
        top = ttk.Frame(root,padding=14); top.pack(fill='x')
        ttk.Label(top,text='ExampleCompany  |  Prospection Data 2027',style='Title.TLabel').pack(anchor='w')
        ttk.Label(top,text='Compte : '+ACCOUNT+'   •   Messages et suivi enregistrés sur ce PC').pack(anchor='w',pady=5)
        self.mode = tk.StringVar(value='Test sur mon adresse')
        modes = ttk.Combobox(top,textvariable=self.mode,values=['Test sur mon adresse','Campagne réelle'],state='readonly',width=28)
        modes.pack(side='left'); modes.bind('<<ComboboxSelected>>',self.switch)
        ttk.Label(top,text='Campagne :').pack(side='left',padx=(18,4))
        self.campaign = tk.StringVar(value=self.campaigns.current())
        self.campaign_box = ttk.Combobox(top,textvariable=self.campaign,values=self.campaigns.names(),state='readonly',width=34)
        self.campaign_box.pack(side='left'); self.campaign_box.bind('<<ComboboxSelected>>',self.switch_campaign)
        ttk.Button(top,text='Nouvelle campagne',command=self.new_campaign).pack(side='left',padx=6)
        self.status = tk.StringVar(value='En pause')
        ttk.Label(top,textvariable=self.status).pack(side='left',padx=20)
        controls = ttk.Frame(root,padding=(14,0)); controls.pack(fill='x')
        for label,fn in [('Vérifier Outlook',lambda:self.work('check')),('Prévisualiser',self.preview),('Démarrer',self.start),('Pause',self.pause)]:
            ttk.Button(controls,text=label,command=fn).pack(side='left',padx=(0,8))
        self.note = tk.StringVar()
        ttk.Label(root,textvariable=self.note,wraplength=1050,padding=14).pack(fill='x')
        tabs = ttk.Notebook(root); tabs.pack(fill='both',expand=True,padx=14,pady=(0,14))
        contacts = ttk.Frame(tabs,padding=10); tabs.add(contacts,text='Contacts et suivi')
        bar = ttk.Frame(contacts); bar.pack(fill='x')
        ttk.Button(bar,text='Importer un CSV',command=self.import_csv).pack(side='left')
        ttk.Button(bar,text='Exclure la sélection',command=self.exclude).pack(side='left',padx=8)
        ttk.Button(bar,text='Exporter le suivi',command=self.export).pack(side='left')
        cols = ('prenom','email','entreprise','stage','status','last')
        self.tree = ttk.Treeview(contacts,columns=cols,show='headings',selectmode='extended')
        for col,label,width in zip(cols,['Prénom','Email','Entreprise','Mails','État','Dernier envoi'],[110,250,140,55,225,145]):
            self.tree.heading(col,text=label); self.tree.column(col,width=width)
        scroll = ttk.Scrollbar(contacts,orient='vertical',command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        scroll.pack(side='right',fill='y',pady=10); self.tree.pack(fill='both',expand=True,pady=10)
        edit = ttk.Frame(tabs,padding=10); tabs.add(edit,text='Les trois mails')
        self.subject = tk.StringVar()
        ttk.Label(edit,text='Objet du premier mail (les relances ajoutent RE:)').pack(anchor='w')
        ttk.Entry(edit,textvariable=self.subject).pack(fill='x',pady=5)
        mailtabs = ttk.Notebook(edit); mailtabs.pack(fill='both',expand=True)
        self.bodies = []
        for i in range(3):
            frame = ttk.Frame(mailtabs); mailtabs.add(frame,text=['Premier mail','Relance 1','Relance 2'][i])
            text = tk.Text(frame,wrap='word',font=('Segoe UI',11),undo=True)
            text.pack(fill='both',expand=True); self.bodies.append(text)
        ttk.Label(edit,text='Champs : {{prénom}}, {{nom}}, {{entreprise}}, {{email}}. Signature : Demo, telle que validée.').pack(anchor='w',pady=5)
        ttk.Button(edit,text='Enregistrer les mails et réglages',command=self.save_config).pack(anchor='w')
        settings = ttk.Frame(tabs,padding=16); tabs.add(settings,text='Délais et horaires')
        self.settings = {}
        for i,(key,label) in enumerate([('day2','Relance 1 : jours après le premier mail'),('day3','Relance 2 : jours après le premier mail'),('daily','Maximum de mails par jour (relances comprises)'),('start','Début des envois (heure locale du PC)'),('end','Fin des envois (heure locale du PC)')]):
            ttk.Label(settings,text=label).grid(row=i,column=0,sticky='w',pady=9)
            var = tk.StringVar(); self.settings[key]=var
            ttk.Entry(settings,textvariable=var,width=8).grid(row=i,column=1,padx=20)
        ttk.Label(settings,text='Campagne réelle : lundi à vendredi. Les échéances sont en jours calendaires.\nUn seul mail maximum par minute. Après une interruption, les relances restent espacées.\nLe mode test utilise 2 puis 4 minutes, sans restriction d’horaire.',wraplength=850).grid(row=5,column=0,columnspan=2,sticky='w',pady=15)
        ttk.Button(settings,text='Enregistrer les mails et réglages',command=self.save_config).grid(row=6,column=0,sticky='w')
        logs = ttk.Frame(tabs,padding=10); tabs.add(logs,text='Journal')
        self.logs = tk.Text(logs,wrap='word',font=('Consolas',10),state='disabled'); self.logs.pack(fill='both',expand=True)
        self.load(); self.refresh()
        root.protocol('WM_DELETE_WINDOW',self.close)
        root.after(200,self.poll)
        root.after(60000,self.tick)

    def path(self):
        return self.directory/'test.sqlite3' if self.test else self.campaigns.db_path(self.campaign.get())

    def db(self):
        if self.test:
            return Database(self.path())
        return Database(self.path(), self.campaign.get(), self.registry)

    def idle(self):
        if self.busy or self.running:
            messagebox.showinfo('En cours','Clique sur Pause et attends la fin de la vérification avant de modifier la campagne.')
            return False
        return True

    def load(self):
        db = self.db()
        if self.test:
            db.add([dict(email=ACCOUNT,prenom='Demo',nom='Nasri',entreprise='ExampleCompany')])
        cfg=db.config(); db.close()
        self.subject.set(cfg['subject'])
        for box,value in zip(self.bodies,cfg['bodies']):
            box.delete('1.0','end'); box.insert('1.0',value)
        for k,v in self.settings.items():
            v.set(str(cfg[k]))
        self.note.set('MODE TEST : envois uniquement à '+ACCOUNT+'. Relances à 2 et 4 minutes. Réponds au premier mail pour tester l’arrêt.' if self.test else 'CAMPAGNE RÉELLE — '+self.campaign.get()+' : Démarrer autorise l’envoi aux contacts importés dans cette campagne uniquement. Toute réponse détectée, même automatique, arrête la séquence du contact. Garde le PC éveillé et cette fenêtre ouverte.')

    def switch(self,event=None):
        if not self.idle():
            self.mode.set('Test sur mon adresse' if self.test else 'Campagne réelle'); return
        self.test=self.mode.get()=='Test sur mon adresse'
        self.load(); self.refresh()


    def switch_campaign(self,event=None):
        if not self.idle():
            self.campaign.set(self.campaigns.current())
            return
        self.campaigns.set_current(self.campaign.get())
        if not self.test:
            self.load(); self.refresh()

    def new_campaign(self):
        if not self.idle():
            return
        name = simpledialog.askstring('Nouvelle campagne','Nom de la campagne :',parent=self.root)
        if not name:
            return
        try:
            created = self.campaigns.create(name)
            self.campaign_box.configure(values=self.campaigns.names())
            self.campaign.set(created)
            self.mode.set('Campagne réelle')
            self.test = False
            self.load(); self.refresh()
            messagebox.showinfo('Campagne créée',f'Campagne « {created} » créée. Tu peux importer un CSV sans toucher aux autres campagnes.')
        except Exception as e:
            messagebox.showerror('Nouvelle campagne',str(e))

    def save_config(self):
        if not self.idle():
            return False
        try:
            cfg=dict(subject=self.subject.get().strip(),bodies=[b.get('1.0','end-1c').strip() for b in self.bodies])
            for k,v in self.settings.items():
                value = v.get().strip().replace(',', '.')
                cfg[k] = float(value) if k in ('start','end') else int(value)
            if not cfg['subject'] or '\n' in cfg['subject'] or '\r' in cfg['subject'] or not all(cfg['bodies']):
                raise ValueError('L’objet et les trois mails doivent être renseignés.')
            if not 1<=cfg['day2']<cfg['day3']<=365 or not 1<=cfg['daily']<=500 or not 0<=cfg['start']<cfg['end']<=24:
                raise ValueError('Vérifie les jours, le plafond (1 à 500) et les horaires (0 à 24).')
            sample=dict(prenom='Demo',nom='Nasri',email=ACCOUNT,entreprise='ExampleCompany')
            for text in [cfg['subject']]+cfg['bodies']:
                personalized(text,sample)
            db=self.db()
            try:
                if db.c.execute('SELECT COUNT(*) FROM messages').fetchone()[0] and cfg!=db.config():
                    raise ValueError('Cette séquence a déjà commencé : ses mails et délais sont figés pour préserver le suivi.')
                with db.c:
                    db.c.execute('UPDATE config SET data=? WHERE id=1',(json.dumps(cfg,ensure_ascii=False),))
            finally:
                db.close()
            return True
        except Exception as e:
            messagebox.showerror('Réglages',str(e)); return False

    def refresh(self):
        db=self.db()
        selected=set(self.tree.selection()); self.tree.delete(*self.tree.get_children())
        pending={r[0] for r in db.c.execute("SELECT email FROM messages WHERE state!='sent'")}
        for r in db.leads():
            state='pending' if r['email'] in pending and r['status'] not in ('reply','excluded') else r['status']
            label = r['block_reason'] if r['block_reason'] and state in ('ready','active') else LABELS[state]
            self.tree.insert('', 'end',iid=r['email'],values=(r['prenom'],r['email'],r['entreprise'],r['stage'],label,datetime.fromtimestamp(r['last']).strftime('%d/%m %H:%M') if r['last'] else ''))
        for email in selected & set(self.tree.get_children()):
            self.tree.selection_add(email)
        events=list(db.c.execute('SELECT * FROM events ORDER BY stamp DESC LIMIT 200')); db.close()
        self.logs.configure(state='normal'); self.logs.delete('1.0','end')
        self.logs.insert('end','\n'.join(datetime.fromtimestamp(r['stamp']).strftime('%d/%m %H:%M:%S')+'  '+r['detail'] for r in events))
        self.logs.configure(state='disabled')

    def import_csv(self):
        if not self.idle(): return
        if self.test:
            messagebox.showinfo('Mode test','Le contact de test est déjà présent. Choisis Campagne réelle pour importer tes contacts.'); return
        path=filedialog.askopenfilename(filetypes=[('Contacts CSV','*.csv'),('Tous les fichiers','*.*')])
        if not path: return
        try:
            rows=read_contacts(path)
            if any(r['email']==ACCOUNT for r in rows):
                raise ValueError('Utilise le mode test pour ta propre adresse.')
            db=self.db(); added, blocked=db.add(rows); db.log(f'Import : {added} nouveau(x), {len(rows)-added-len(blocked)} doublon(s) déjà présents, {len(blocked)} bloqué(s) par le registre central.'); db.close()
            self.refresh()
            details = ''
            if blocked:
                preview = '\n'.join(f'- {email} : {reason}' for email, reason in blocked[:12])
                more = f'\n... et {len(blocked)-12} autre(s).' if len(blocked) > 12 else ''
                details = '\n\nNon importés :\n' + preview + more
            messagebox.showinfo('Import terminé',f'{added} contacts ajoutés. Aucun mail envoyé.{details}')
        except Exception as e:
            messagebox.showerror('Import impossible',str(e))

    def exclude(self):
        if not self.idle(): return
        emails=self.tree.selection()
        if not emails: return
        db=self.db(); db.exclude(emails); db.close(); self.refresh()

    def export(self):
        path=filedialog.asksaveasfilename(defaultextension='.csv',initialfile='campaign_tracking.csv')
        if not path:return
        db=self.db()
        try:
            with open(path,'w',encoding='utf-8-sig',newline='') as f:
                writer=csv.writer(f,delimiter=';'); writer.writerow(['prenom','email','entreprise','mails_envoyes','etat'])
                for r in db.leads():
                    cells=[r['prenom'],r['email'],r['entreprise'],str(r['stage']),LABELS[r['status']]]
                    writer.writerow(["'"+c if c.startswith(('=','+','-','@')) else c for c in cells])
        finally: db.close()

    def preview(self):
        if not self.idle() or not self.save_config(): return
        db=self.db(); leads=db.leads(); cfg=db.config(); db.close()
        selected=self.tree.selection()
        lead=next((r for r in leads if selected and r['email']==selected[0]),leads[0] if leads else dict(prenom='Prénom',nom='Nom',entreprise='Entreprise',email='contact@example.com'))
        win=tk.Toplevel(self.root); win.title('Aperçu : '+lead['email']); win.geometry('800x680')
        text=tk.Text(win,wrap='word',font=('Segoe UI',11)); text.pack(fill='both',expand=True,padx=15,pady=15)
        for i,body in enumerate(cfg['bodies']):
            text.insert('end',f'MAIL {i+1}\nÀ : {lead["email"]}\nObjet : '+('RE: ' if i else '')+personalized(cfg['subject'],lead)+'\n\n'+personalized(body,lead)+'\n\n'+'· '*40+'\n\n')
        text.configure(state='disabled')

    def start(self):
        if not self.idle() or not self.save_config(): return
        db=self.db(); leads=db.leads(); db.close()
        active=[r for r in leads if r['status'] in ('ready','active') and not r['block_reason']]
        if not active:
            messagebox.showinfo('Aucun contact actif','Ajoute des contacts à la campagne réelle. Un contact ayant répondu ou exclu ne sera pas réactivé.'); return
        detail=('Les mails de test seront réellement envoyés uniquement à '+ACCOUNT+'.' if self.test else f'Campagne : {self.campaign.get()}\nLes mails seront réellement envoyés à {len(active)} contact(s) depuis {ACCOUNT}.')
        if not messagebox.askyesno('Démarrer les envois',detail+'\n\nAs-tu vérifié les messages avec Prévisualiser ?\nDémarrer maintenant ?'):return
        self.running=True; self.work('send')

    def pause(self):
        self.running=False; self.cancel.set(); self.status.set('Pause demandée' if self.busy else 'En pause')

    def work(self,kind):
        if self.busy:return
        self.busy=True; self.cancel.clear(); self.status.set('Synchronisation et vérification…')
        path,test=self.path(),self.test
        def run():
            db=None; registry=None; initialized=False
            try:
                import pythoncom
                pythoncom.CoInitialize(); initialized=True
                registry = None if test else CentralRegistry(self.directory/'central_registry.sqlite3')
                db=Database(path, self.campaign.get() if not test else '', registry); outlook=Outlook(self.cancel)
                cycle(db,outlook,self.cancel,test,kind=='send')
                if kind=='check':db.log('Connexion, synchronisation et lecture vérifiées. Aucun nouvel envoi par l’outil.')
                self.results.put(('ok',''))
            except Cancelled:
                self.results.put(('cancel',''))
            except Exception as e:
                if db:db.log('PAUSE : '+str(e))
                self.results.put(('error',str(e)))
            finally:
                if db:db.close()
                if registry:registry.close()
                if initialized:pythoncom.CoUninitialize()
        threading.Thread(target=run,daemon=True).start()

    def poll(self):
        try:
            kind,text=self.results.get_nowait(); self.busy=False
            if kind=='error':
                self.running=False; messagebox.showerror('Campagne en pause',text)
            self.status.set('En cours : prochaine vérification sous 60 secondes' if self.running else 'En pause')
            self.refresh()
        except queue.Empty:pass
        self.root.after(200,self.poll)

    def tick(self):
        if self.running and not self.busy:self.work('send')
        self.root.after(60000,self.tick)

    def close(self):
        self.pause()
        if self.busy:
            messagebox.showinfo('Patiente un instant','La vérification s’arrête. Ferme la fenêtre quand « En pause » apparaît.'); return
        self.registry.close()
        self.root.destroy()

def main():
    root=tk.Tk()
    if os.name!='nt':
        messagebox.showerror('Windows requis','Ce programme utilise Outlook classique sur Windows.'); root.destroy(); return
    directory=Path(os.environ.get('LOCALAPPDATA',str(Path.home())))/'ExampleCompany_Prospection'
    directory.mkdir(parents=True,exist_ok=True)
    # Keep the lock handle alive for the full application lifetime.
    import msvcrt
    lock_path = directory/'app.lock'
    if not lock_path.exists():
        lock_path.write_bytes(b'0')
    lock=open(lock_path,'r+b'); lock.seek(0)
    lock.seek(0)
    try:msvcrt.locking(lock.fileno(),msvcrt.LK_NBLCK,1)
    except OSError:
        messagebox.showinfo('Déjà ouvert','Une fenêtre Sales Automation est déjà ouverte.'); root.destroy(); lock.close(); return
    try:
        App(root,directory); root.mainloop()
    finally:
        lock.seek(0); msvcrt.locking(lock.fileno(),msvcrt.LK_UNLCK,1); lock.close()

if __name__=='__main__':
    main()

