"""Moteur local de coordination SDR ExampleCompany.

Ce module garde un registre central SQLite partagé par toutes les campagnes.
Il ne contacte personne tout seul : il décide seulement si un prospect peut
être sollicité et quelles actions commerciales sont recommandées.
"""
from __future__ import annotations

import os
import json
import re
import sqlite3
import time
import tkinter as tk
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from tkinter import ttk, messagebox


COOLDOWN_DAYS = 7
COOLDOWN_SECONDS = COOLDOWN_DAYS * 86400

STATUS_TO_QUALIFY = 'À qualifier'
STATUS_TO_CONTACT = 'À contacter'
STATUS_CONTACTED = 'Contacté'
STATUS_FOLLOWUP = 'Relance en cours'
STATUS_POSITIVE = 'Réponse positive'
STATUS_NEGATIVE = 'Réponse négative'
STATUS_CALL_BACK = 'À rappeler'
STATUS_MEETING_DISCUSSION = 'RDV en discussion'
STATUS_MEETING_CONFIRMED = 'RDV confirmé'
STATUS_OOO = 'OOO'
STATUS_EXCLUDED = 'Exclu'
STATUS_REPLY = 'Réponse reçue'
STATUS_SEQUENCE_DONE = 'Séquence terminée'
STATUS_MANUAL_ACTION = 'Action commerciale manuelle'
STATUS_PROPOSED = 'Proposé'
STATUS_VALIDATED = 'Validé'

SDR_STATUSES = (
    STATUS_TO_QUALIFY,
    STATUS_PROPOSED,
    STATUS_VALIDATED,
    STATUS_TO_CONTACT,
    STATUS_CONTACTED,
    STATUS_FOLLOWUP,
    STATUS_POSITIVE,
    STATUS_NEGATIVE,
    STATUS_CALL_BACK,
    STATUS_MEETING_DISCUSSION,
    STATUS_MEETING_CONFIRMED,
    STATUS_OOO,
    STATUS_EXCLUDED,
)


@dataclass(frozen=True)
class RegistryDecision:
    allowed: bool
    reason: str = ''
    code: str = 'ok'


def local_data_dir() -> Path:
    base = os.environ.get('LOCALAPPDATA') or str(Path.home())
    path = Path(base) / 'ExampleCompany_Prospection'
    path.mkdir(parents=True, exist_ok=True)
    return path


def registry_path() -> Path:
    return local_data_dir() / 'central_registry.sqlite3'


def normalize_email(email: str) -> str:
    return (email or '').strip().lower()


def format_date(stamp: float) -> str:
    return datetime.fromtimestamp(stamp).strftime('%d/%m/%Y')


DEFAULT_CAMPAIGN_CONFIG = dict(
    subject='ExampleCompany - Échange Data / IA au Luxembourg',
    bodies=[
        """Bonjour {{prénom}},

Je me permets de vous contacter car ExampleCompany accompagne des organisations luxembourgeoises sur leurs sujets Data, BI, plateformes Azure/Fabric et IA sécurisée.

Au vu de votre rôle chez {{entreprise}}, je pense qu’un échange court pourrait être utile pour comprendre vos priorités actuelles : gouvernance data, industrialisation BI, plateforme data ou usages GenAI sécurisés.

Auriez-vous 20 minutes la semaine prochaine pour en discuter ?

Bonne journée,

Demo""",
        """Bonjour {{prénom}},

Je me permets de revenir vers vous concernant mon message précédent.

Chez ExampleCompany, nous intervenons notamment sur la mise en place de plateformes data, la gouvernance, Power BI/Fabric et les usages IA sécurisés en environnement sensible.

Est-ce un sujet ouvert chez {{entreprise}} dans les prochains mois ?

Bonne journée,

Demo""",
        """Bonjour {{prénom}},

Dernier message de ma part.

Si les sujets Data, BI ou IA sécurisée ne sont pas prioritaires chez {{entreprise}} actuellement, je ne vous relance pas davantage.

Sinon, je serais ravi d’échanger 20 minutes pour voir si ExampleCompany peut vous être utile.

Bonne journée,

Demo""",
    ],
    day2=5,
    day3=12,
    daily=25,
    start=8,
    end=18.5,
)


class CentralRegistry:
    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path else registry_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.c = sqlite3.connect(self.path, timeout=20)
        self.c.row_factory = sqlite3.Row
        self.c.execute('PRAGMA journal_mode=WAL')
        self._migrate()

    def close(self):
        self.c.close()

    def _migrate(self):
        self.c.executescript('''
        CREATE TABLE IF NOT EXISTS prospects (
            email TEXT PRIMARY KEY,
            linkedin_url TEXT NOT NULL DEFAULT '',
            prenom TEXT NOT NULL DEFAULT '',
            nom TEXT NOT NULL DEFAULT '',
            entreprise TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'À qualifier',
            prospect_type TEXT NOT NULL DEFAULT 'jamais_rencontre',
            active_campaign TEXT NOT NULL DEFAULT '',
            cooldown_until REAL,
            last_manual_action REAL,
            source TEXT NOT NULL DEFAULT '',
            source_week TEXT NOT NULL DEFAULT '',
            score INTEGER NOT NULL DEFAULT 0,
            campaign_angle TEXT NOT NULL DEFAULT '',
            priority TEXT NOT NULL DEFAULT '',
            last_proposed_at REAL,
            last_validated_at REAL,
            last_contacted_at REAL,
            next_action_at REAL,
            cadence_step INTEGER NOT NULL DEFAULT 0,
            validated_by_owner INTEGER NOT NULL DEFAULT 0,
            campaign_id TEXT NOT NULL DEFAULT '',
            do_not_repropose_before REAL,
            executive_client_status TEXT NOT NULL DEFAULT '',
            executive_client_context TEXT NOT NULL DEFAULT '',
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS registry_events (
            stamp REAL NOT NULL,
            email TEXT NOT NULL,
            detail TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS known_accounts (
            name TEXT PRIMARY KEY,
            source TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT '',
            relationship TEXT NOT NULL DEFAULT '',
            current_revenue REAL NOT NULL DEFAULT 0,
            next_deadline TEXT NOT NULL DEFAULT '',
            notes TEXT NOT NULL DEFAULT '',
            updated_at REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_prospects_status ON prospects(status);
        CREATE INDEX IF NOT EXISTS idx_prospects_active_campaign ON prospects(active_campaign);
        ''')
        self._add_missing_columns()
        self.c.execute('CREATE INDEX IF NOT EXISTS idx_prospects_next_action ON prospects(next_action_at)')
        self.c.commit()

    def _add_missing_columns(self):
        columns = {r[1] for r in self.c.execute('PRAGMA table_info(prospects)')}
        additions = {
            'source': "TEXT NOT NULL DEFAULT ''",
            'source_week': "TEXT NOT NULL DEFAULT ''",
            'score': "INTEGER NOT NULL DEFAULT 0",
            'campaign_angle': "TEXT NOT NULL DEFAULT ''",
            'priority': "TEXT NOT NULL DEFAULT ''",
            'last_proposed_at': "REAL",
            'last_validated_at': "REAL",
            'last_contacted_at': "REAL",
            'next_action_at': "REAL",
            'cadence_step': "INTEGER NOT NULL DEFAULT 0",
            'validated_by_owner': "INTEGER NOT NULL DEFAULT 0",
            'campaign_id': "TEXT NOT NULL DEFAULT ''",
            'do_not_repropose_before': "REAL",
            'executive_client_status': "TEXT NOT NULL DEFAULT ''",
            'executive_client_context': "TEXT NOT NULL DEFAULT ''",
        }
        for name, definition in additions.items():
            if name not in columns:
                self.c.execute(f'ALTER TABLE prospects ADD COLUMN {name} {definition}')

    def log(self, email: str, detail: str):
        self.c.execute('INSERT INTO registry_events VALUES(?,?,?)', (time.time(), normalize_email(email), detail))

    def get(self, email: str):
        return self.c.execute('SELECT * FROM prospects WHERE email=?', (normalize_email(email),)).fetchone()

    def upsert_identity(self, row: dict, status: str | None = None, active_campaign: str | None = None):
        email = normalize_email(row.get('email', ''))
        if not email:
            return
        stamp = time.time()
        existing = self.get(email)
        values = (
            email,
            (row.get('linkedin_url') or '').strip(),
            (row.get('prenom') or '').strip(),
            (row.get('nom') or '').strip(),
            (row.get('entreprise') or '').strip(),
            status or STATUS_TO_QUALIFY,
            active_campaign or '',
            (row.get('source') or '').strip(),
            (row.get('source_week') or '').strip(),
            int(row.get('score') or 0),
            (row.get('campaign_angle') or '').strip(),
            (row.get('priority') or '').strip(),
            (row.get('campaign_id') or '').strip(),
            (row.get('executive_client_status') or '').strip(),
            (row.get('executive_client_context') or '').strip(),
            stamp,
            stamp,
        )
        if existing:
            self.c.execute('''
                UPDATE prospects
                SET linkedin_url=COALESCE(NULLIF(?,''), linkedin_url),
                    prenom=COALESCE(NULLIF(?,''), prenom),
                    nom=COALESCE(NULLIF(?,''), nom),
                    entreprise=COALESCE(NULLIF(?,''), entreprise),
                    status=CASE WHEN ?!='' THEN ? ELSE status END,
                    active_campaign=CASE WHEN ?!='' THEN ? ELSE active_campaign END,
                    source=COALESCE(NULLIF(?,''), source),
                    source_week=COALESCE(NULLIF(?,''), source_week),
                    score=CASE WHEN ?>0 THEN ? ELSE score END,
                    campaign_angle=COALESCE(NULLIF(?,''), campaign_angle),
                    priority=COALESCE(NULLIF(?,''), priority),
                    campaign_id=COALESCE(NULLIF(?,''), campaign_id),
                    executive_client_status=COALESCE(NULLIF(?,''), executive_client_status),
                    executive_client_context=COALESCE(NULLIF(?,''), executive_client_context),
                    updated_at=?
                WHERE email=?
            ''', (values[1], values[2], values[3], values[4],
                  status or '', status or '', active_campaign or '', active_campaign or '',
                  values[7], values[8], values[9], values[9], values[10], values[11],
                  values[12], values[13], values[14], stamp, email))
        else:
            self.c.execute('''
                INSERT INTO prospects(email,linkedin_url,prenom,nom,entreprise,status,active_campaign,
                    source,source_week,score,campaign_angle,priority,campaign_id,executive_client_status,
                    executive_client_context,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ''', values)

    def evaluate(self, email: str, campaign_name: str = '', stamp: float | None = None) -> RegistryDecision:
        stamp = stamp or time.time()
        email = normalize_email(email)
        row = self.get(email)
        if not row:
            return RegistryDecision(True)
        status = row['status']
        active_campaign = row['active_campaign'] or ''
        if status == STATUS_EXCLUDED:
            return RegistryDecision(False, 'exclu globalement', 'global_excluded')
        if status in (STATUS_REPLY, STATUS_POSITIVE, STATUS_NEGATIVE):
            return RegistryDecision(False, 'réponse reçue', 'reply')
        if status == STATUS_MEETING_DISCUSSION:
            return RegistryDecision(False, 'RDV en discussion', 'meeting_discussion')
        if status == STATUS_MEETING_CONFIRMED:
            return RegistryDecision(False, 'RDV confirmé', 'meeting_confirmed')
        if active_campaign and campaign_name and active_campaign != campaign_name:
            return RegistryDecision(False, f'actif dans campagne {active_campaign}', 'active_other_campaign')
        if row['cooldown_until'] and row['cooldown_until'] > stamp:
            return RegistryDecision(False, f"cooldown jusqu'au {format_date(row['cooldown_until'])}", 'cooldown')
        if row['do_not_repropose_before'] and row['do_not_repropose_before'] > stamp:
            return RegistryDecision(False, f"ne pas reproposer avant le {format_date(row['do_not_repropose_before'])}", 'reproposal_cooldown')
        return RegistryDecision(True)

    def reserve_for_campaign(self, row: dict, campaign_name: str) -> RegistryDecision:
        email = normalize_email(row.get('email', ''))
        decision = self.evaluate(email, campaign_name)
        if not decision.allowed:
            return decision
        with self.c:
            self.upsert_identity(row, STATUS_TO_CONTACT, campaign_name)
            self.log(email, f'Réservé pour la campagne {campaign_name}.')
        return RegistryDecision(True)

    def mark_contacted(self, email: str, campaign_name: str, followup: bool = False):
        with self.c:
            self.upsert_identity({'email': email}, STATUS_FOLLOWUP if followup else STATUS_CONTACTED, campaign_name)
            self.c.execute('UPDATE prospects SET last_contacted_at=?, cadence_step=MAX(cadence_step, 1), next_action_at=?, updated_at=? WHERE email=?',
                           (time.time(), time.time() + COOLDOWN_SECONDS, time.time(), normalize_email(email)))
            self.log(email, f'Contacté par la campagne {campaign_name}.')

    def mark_reply(self, email: str):
        with self.c:
            self.upsert_identity({'email': email}, STATUS_REPLY, '')
            self.c.execute('UPDATE prospects SET active_campaign="", cooldown_until=NULL, updated_at=? WHERE email=?',
                           (time.time(), normalize_email(email)))
            self.log(email, 'Réponse reçue : prospection automatique bloquée.')

    def mark_excluded(self, email: str):
        with self.c:
            self.upsert_identity({'email': email}, STATUS_EXCLUDED, '')
            self.c.execute('UPDATE prospects SET active_campaign="", cooldown_until=NULL, updated_at=? WHERE email=?',
                           (time.time(), normalize_email(email)))
            self.log(email, 'Exclusion globale.')

    def mark_sequence_done(self, email: str, campaign_name: str, stamp: float | None = None):
        stamp = stamp or time.time()
        with self.c:
            self.upsert_identity({'email': email}, STATUS_SEQUENCE_DONE, '')
            self.c.execute('UPDATE prospects SET active_campaign="", cooldown_until=?, updated_at=? WHERE email=?',
                           (stamp + COOLDOWN_SECONDS, stamp, normalize_email(email)))
            self.log(email, f'Séquence terminée dans {campaign_name}; cooldown {COOLDOWN_DAYS} jours.')

    def mark_manual_action(self, email: str, detail: str = 'Action commerciale manuelle', stamp: float | None = None):
        stamp = stamp or time.time()
        with self.c:
            self.upsert_identity({'email': email}, STATUS_MANUAL_ACTION, '')
            self.c.execute('UPDATE prospects SET active_campaign="", last_manual_action=?, cooldown_until=?, updated_at=? WHERE email=?',
                           (stamp, stamp + COOLDOWN_SECONDS, stamp, normalize_email(email)))
            self.log(email, detail)

    def mark_proposed(self, email: str, source_week: str = '', score: int = 0, angle: str = '', priority: str = ''):
        stamp = time.time()
        with self.c:
            self.upsert_identity({'email': email, 'source_week': source_week, 'score': score,
                                  'campaign_angle': angle, 'priority': priority}, STATUS_PROPOSED, '')
            self.c.execute('''
                UPDATE prospects
                SET last_proposed_at=?, do_not_repropose_before=?, source_week=COALESCE(NULLIF(?,''), source_week),
                    score=CASE WHEN ?>0 THEN ? ELSE score END,
                    campaign_angle=COALESCE(NULLIF(?,''), campaign_angle),
                    priority=COALESCE(NULLIF(?,''), priority),
                    updated_at=?
                WHERE email=?
            ''', (stamp, stamp + 21 * 86400, source_week, score, score, angle, priority, stamp, normalize_email(email)))
            self.log(email, 'Proposé dans le batch SDR hebdomadaire.')

    def mark_validated(self, email: str):
        stamp = time.time()
        with self.c:
            self.upsert_identity({'email': email}, STATUS_VALIDATED, '')
            self.c.execute('UPDATE prospects SET validated_by_owner=1,last_validated_at=?,next_action_at=?,updated_at=? WHERE email=?',
                           (stamp, stamp, stamp, normalize_email(email)))
            self.log(email, 'Validé par Demo pour action SDR.')

    def due_for_followup(self, stamp: float | None = None):
        stamp = stamp or time.time()
        return list(self.c.execute('''
            SELECT * FROM prospects
            WHERE next_action_at IS NOT NULL
              AND next_action_at<=?
              AND status NOT IN (?,?,?,?,?)
            ORDER BY next_action_at, score DESC
        ''', (stamp, STATUS_EXCLUDED, STATUS_NEGATIVE, STATUS_MEETING_CONFIRMED, STATUS_REPLY, STATUS_MEETING_DISCUSSION)))

    def set_sdr_status(self, email: str, status: str, **identity):
        if status not in SDR_STATUSES:
            raise ValueError('Statut SDR inconnu.')
        data = {'email': email, **identity}
        with self.c:
            self.upsert_identity(data, status, '')
            if status in (STATUS_MEETING_DISCUSSION, STATUS_MEETING_CONFIRMED, STATUS_EXCLUDED):
                self.c.execute('UPDATE prospects SET active_campaign="", updated_at=? WHERE email=?',
                               (time.time(), normalize_email(email)))
            self.log(email, f'Statut SDR : {status}.')

    def upsert_known_account(self, name: str, **fields):
        name = ' '.join((name or '').split())
        if not name:
            return
        stamp = time.time()
        current = self.c.execute('SELECT * FROM known_accounts WHERE name=?', (name,)).fetchone()
        values = (
            name,
            fields.get('source', ''),
            fields.get('status', ''),
            fields.get('relationship', ''),
            float(fields.get('current_revenue') or 0),
            fields.get('next_deadline', ''),
            fields.get('notes', ''),
            stamp,
        )
        with self.c:
            if current:
                self.c.execute('''
                    UPDATE known_accounts
                    SET source=COALESCE(NULLIF(?,''), source),
                        status=COALESCE(NULLIF(?,''), status),
                        relationship=COALESCE(NULLIF(?,''), relationship),
                        current_revenue=CASE WHEN ?>0 THEN ? ELSE current_revenue END,
                        next_deadline=COALESCE(NULLIF(?,''), next_deadline),
                        notes=COALESCE(NULLIF(?,''), notes),
                        updated_at=?
                    WHERE name=?
                ''', (values[1], values[2], values[3], values[4], values[4], values[5], values[6], stamp, name))
            else:
                self.c.execute('INSERT INTO known_accounts VALUES(?,?,?,?,?,?,?,?)', values)

    def known_accounts(self):
        return list(self.c.execute('SELECT * FROM known_accounts ORDER BY relationship DESC, name'))

    def prospects(self):
        return list(self.c.execute('SELECT * FROM prospects ORDER BY updated_at DESC, entreprise, email'))

    def weekly_confirmed_count(self):
        return self.c.execute('SELECT COUNT(*) FROM prospects WHERE status=?', (STATUS_MEETING_CONFIRMED,)).fetchone()[0]

    def validated_for_campaign(self):
        return list(self.c.execute('''
            SELECT * FROM prospects
            WHERE status=? AND validated_by_owner=1
              AND email NOT IN (
                  SELECT email FROM prospects
                  WHERE status IN (?,?,?,?,?)
              )
            ORDER BY score DESC, updated_at DESC
        ''', (STATUS_VALIDATED, STATUS_EXCLUDED, STATUS_NEGATIVE, STATUS_REPLY,
              STATUS_MEETING_DISCUSSION, STATUS_MEETING_CONFIRMED)))

    def attach_campaign(self, email: str, campaign_name: str):
        stamp = time.time()
        with self.c:
            self.c.execute('''
                UPDATE prospects
                SET campaign_id=?, active_campaign=?, status=?, next_action_at=?, updated_at=?
                WHERE email=?
            ''', (campaign_name, campaign_name, STATUS_TO_CONTACT, stamp, stamp, normalize_email(email)))
            self.log(email, f'Ajouté à la campagne email {campaign_name}.')


def campaign_slug(name: str) -> str:
    slug = re.sub(r'[^A-Za-z0-9_-]+', '_', name).strip('_').lower()[:50]
    return slug or 'campagne_sdr'


def create_email_campaign_from_validated(registry: CentralRegistry, name: str | None = None):
    directory = local_data_dir()
    campaigns_path = directory / 'campaigns.json'
    if campaigns_path.exists():
        try:
            data = json.loads(campaigns_path.read_text(encoding='utf-8'))
        except Exception:
            data = {}
    else:
        data = {}
    data.setdefault('campaigns', {})
    stamp = datetime.now().strftime('%Y-%m-%d')
    campaign_name = name or f'ExampleCompany SDR Luxembourg {stamp}'
    base_name = campaign_name
    i = 2
    while campaign_name in data['campaigns']:
        campaign_name = f'{base_name} {i}'
        i += 1
    filename = f'campagne_{campaign_slug(campaign_name)}.sqlite3'
    existing = set(data['campaigns'].values())
    i = 2
    while filename in existing or (directory / filename).exists():
        filename = f'campagne_{campaign_slug(campaign_name)}_{i}.sqlite3'
        i += 1

    leads = registry.validated_for_campaign()
    if not leads:
        raise ValueError('Aucun prospect validé à ajouter à une campagne.')

    db_path = directory / filename
    conn = sqlite3.connect(db_path, timeout=20)
    try:
        conn.executescript('''
        CREATE TABLE IF NOT EXISTS config (id INTEGER PRIMARY KEY, data TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS leads (email TEXT PRIMARY KEY, prenom TEXT NOT NULL,
            nom TEXT NOT NULL, entreprise TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'ready',
            first REAL, last REAL, stage INTEGER NOT NULL DEFAULT 0, block_reason TEXT NOT NULL DEFAULT '');
        CREATE TABLE IF NOT EXISTS messages (token TEXT PRIMARY KEY, email TEXT NOT NULL,
            stage INTEGER NOT NULL, created REAL NOT NULL, state TEXT NOT NULL,
            mid TEXT, sent REAL, UNIQUE(email,stage));
        CREATE TABLE IF NOT EXISTS events (stamp REAL NOT NULL, detail TEXT NOT NULL);
        ''')
        conn.execute('INSERT OR REPLACE INTO config VALUES(1,?)', (json.dumps(DEFAULT_CAMPAIGN_CONFIG, ensure_ascii=False),))
        added = 0
        for row in leads:
            company = row['entreprise'].split(' - Luxembourg - ', 1)[0].strip() or row['entreprise']
            inserted = conn.execute(
                'INSERT OR IGNORE INTO leads(email,prenom,nom,entreprise,status,block_reason) VALUES(?,?,?,?,?,?)',
                (row['email'], row['prenom'], row['nom'], company, 'ready', ''),
            ).rowcount
            if inserted:
                added += 1
                registry.attach_campaign(row['email'], campaign_name)
        conn.execute('INSERT INTO events VALUES(?,?)', (time.time(), f'Campagne créée depuis Agent SDR : {added} prospect(s) validé(s).'))
        conn.commit()
    finally:
        conn.close()

    data['campaigns'][campaign_name] = filename
    data['current'] = campaign_name
    campaigns_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    return campaign_name, added


class SDRAgent:
    def __init__(self, registry: CentralRegistry | None = None, weekly_goal: int = 5):
        self.registry = registry or CentralRegistry()
        self.weekly_goal = weekly_goal

    def recommend_next_action(self, email: str) -> str:
        row = self.registry.get(email)
        if not row:
            return 'email'
        status = row['status']
        if status in (STATUS_EXCLUDED, STATUS_NEGATIVE, STATUS_MEETING_CONFIRMED, STATUS_REPLY):
            return 'stop'
        if status == STATUS_PROPOSED:
            return 'à valider'
        if status == STATUS_VALIDATED:
            return 'email'
        if status == STATUS_MEETING_DISCUSSION:
            return 'proposer des créneaux'
        if status == STATUS_OOO:
            return 'attendre'
        if status == STATUS_CALL_BACK:
            return 'téléphone'
        if status in (STATUS_TO_QUALIFY, STATUS_TO_CONTACT):
            return 'email'
        if status in (STATUS_CONTACTED, STATUS_FOLLOWUP):
            return 'email'
        if row['cooldown_until'] and row['cooldown_until'] > time.time():
            return 'attendre'
        return 'email'

    def mark_meeting_discussion(self, email: str, **identity):
        self.registry.set_sdr_status(email, STATUS_MEETING_DISCUSSION, **identity)

    def mark_meeting_confirmed(self, email: str, **identity):
        self.registry.set_sdr_status(email, STATUS_MEETING_CONFIRMED, **identity)


class SDRAgentApp:
    def __init__(self, root):
        self.root = root
        self.registry = CentralRegistry()
        self.agent = SDRAgent(self.registry)
        self.root.title('ExampleCompany | Agent SDR')
        self.root.geometry('1080x680')
        self.root.minsize(900, 560)
        style = ttk.Style()
        style.theme_use('clam')
        style.configure('TButton', padding=7)
        style.configure('Title.TLabel', font=('Segoe UI', 18, 'bold'))

        top = ttk.Frame(root, padding=14)
        top.pack(fill='x')
        ttk.Label(top, text='ExampleCompany | Agent SDR', style='Title.TLabel').pack(anchor='w')
        self.goal = tk.StringVar()
        ttk.Label(top, textvariable=self.goal).pack(anchor='w', pady=5)

        main = ttk.PanedWindow(root, orient='horizontal')
        main.pack(fill='both', expand=True, padx=14, pady=(0, 14))

        left = ttk.Frame(main, padding=8)
        main.add(left, weight=3)
        cols = ('email', 'entreprise', 'status', 'action', 'score', 'priority', 'source_week', 'next_action', 'client')
        self.tree = ttk.Treeview(left, columns=cols, show='headings', selectmode='browse')
        labels = ['Email', 'Entreprise', 'Statut', 'Action', 'Score', 'Priorité', 'Semaine', 'Prochaine action', 'Client']
        widths = [220, 230, 130, 130, 60, 80, 90, 110, 110]
        for col, label, width in zip(cols, labels, widths):
            self.tree.heading(col, text=label)
            self.tree.column(col, width=width)
        self.tree.pack(fill='both', expand=True)
        self.tree.bind('<<TreeviewSelect>>', self.load_selected)

        right = ttk.Frame(main, padding=12)
        main.add(right, weight=2)
        self.vars = {name: tk.StringVar() for name in ('email', 'prenom', 'nom', 'entreprise')}
        for i, (name, label) in enumerate([
            ('email', 'Email'),
            ('prenom', 'Prénom'),
            ('nom', 'Nom'),
            ('entreprise', 'Entreprise'),
        ]):
            ttk.Label(right, text=label).grid(row=i, column=0, sticky='w', pady=6)
            ttk.Entry(right, textvariable=self.vars[name]).grid(row=i, column=1, sticky='ew', pady=6)

        ttk.Label(right, text='Type').grid(row=4, column=0, sticky='w', pady=6)
        self.prospect_type = tk.StringVar(value='jamais_rencontre')
        ttk.Combobox(right, textvariable=self.prospect_type, values=['jamais_rencontre', 'deja_rencontre'], state='readonly').grid(row=4, column=1, sticky='ew', pady=6)

        ttk.Label(right, text='Statut').grid(row=5, column=0, sticky='w', pady=6)
        self.status = tk.StringVar(value=STATUS_TO_QUALIFY)
        ttk.Combobox(right, textvariable=self.status, values=SDR_STATUSES, state='readonly').grid(row=5, column=1, sticky='ew', pady=6)
        right.columnconfigure(1, weight=1)

        buttons = ttk.Frame(right)
        buttons.grid(row=6, column=0, columnspan=2, sticky='ew', pady=(14, 8))
        ttk.Button(buttons, text='Enregistrer', command=self.save).pack(side='left', padx=(0, 8))
        ttk.Button(buttons, text='Validé', command=self.validate_selected).pack(side='left', padx=(0, 8))
        ttk.Button(buttons, text='Valider A', command=self.validate_priority_a).pack(side='left', padx=(0, 8))
        ttk.Button(buttons, text='Rafraîchir', command=self.refresh).pack(side='left')

        campaign_buttons = ttk.Frame(right)
        campaign_buttons.grid(row=7, column=0, columnspan=2, sticky='ew', pady=(0, 8))
        ttk.Button(campaign_buttons, text='Créer campagne email', command=self.create_campaign).pack(side='left', padx=(0, 8))
        ttk.Button(campaign_buttons, text='Action manuelle', command=self.manual_action).pack(side='left')

        quick = ttk.LabelFrame(right, text='Marquages rapides', padding=10)
        quick.grid(row=8, column=0, columnspan=2, sticky='ew', pady=10)
        for label, status in [
            ('RDV en discussion', STATUS_MEETING_DISCUSSION),
            ('RDV confirmé', STATUS_MEETING_CONFIRMED),
            ('Réponse positive', STATUS_POSITIVE),
            ('Réponse négative', STATUS_NEGATIVE),
            ('Exclu', STATUS_EXCLUDED),
        ]:
            ttk.Button(quick, text=label, command=lambda s=status: self.quick_status(s)).pack(fill='x', pady=3)

        self.detail = tk.StringVar()
        ttk.Label(right, textvariable=self.detail, wraplength=360).grid(row=9, column=0, columnspan=2, sticky='w', pady=10)

        root.protocol('WM_DELETE_WINDOW', self.close)
        self.refresh()

    def selected_email(self):
        selection = self.tree.selection()
        return selection[0] if selection else normalize_email(self.vars['email'].get())

    def row_identity(self):
        return {
            'prenom': self.vars['prenom'].get().strip(),
            'nom': self.vars['nom'].get().strip(),
            'entreprise': self.vars['entreprise'].get().strip(),
        }

    def refresh(self):
        self.tree.delete(*self.tree.get_children())
        for row in self.registry.prospects():
            action = self.agent.recommend_next_action(row['email'])
            next_action = format_date(row['next_action_at']) if row['next_action_at'] else ''
            self.tree.insert('', 'end', iid=row['email'], values=(
                row['email'], row['entreprise'], row['status'], action, row['score'],
                row['priority'], row['source_week'], next_action, row['executive_client_status']
            ))
        confirmed = self.registry.weekly_confirmed_count()
        self.goal.set(f'Objectif : 5 RDV confirmés pour la semaine suivante | Confirmés dans le registre : {confirmed}')
        self.detail.set('Sélectionne un prospect ou saisis un email pour l’ajouter au registre SDR.')

    def load_selected(self, event=None):
        email = self.selected_email()
        row = self.registry.get(email)
        if not row:
            return
        for name in ('email', 'prenom', 'nom', 'entreprise'):
            self.vars[name].set(row[name])
        self.prospect_type.set(row['prospect_type'] or 'jamais_rencontre')
        self.status.set(row['status'] if row['status'] in SDR_STATUSES else STATUS_CONTACTED)
        decision = self.registry.evaluate(email, 'Agent SDR')
        action = self.agent.recommend_next_action(email)
        block = decision.reason if not decision.allowed else 'aucun blocage'
        client = row['executive_client_context'] or row['executive_client_status'] or 'aucun contexte client connu'
        self.detail.set(f'Action recommandée : {action}. Blocage campagnes email : {block}. Contexte : {client}.')

    def save(self):
        email = normalize_email(self.vars['email'].get())
        if not email or '@' not in email:
            messagebox.showerror('Agent SDR', 'Renseigne un email valide.')
            return
        identity = self.row_identity()
        identity['prospect_type'] = self.prospect_type.get()
        try:
            self.registry.set_sdr_status(email, self.status.get(), **identity)
            self.refresh()
            self.tree.selection_set(email)
            self.load_selected()
        except Exception as exc:
            messagebox.showerror('Agent SDR', str(exc))

    def quick_status(self, status):
        self.status.set(status)
        self.save()

    def validate_selected(self):
        email = self.selected_email()
        if not email:
            messagebox.showerror('Agent SDR', 'Sélectionne ou saisis un prospect.')
            return
        self.registry.mark_validated(email)
        self.refresh()
        self.tree.selection_set(email)
        self.load_selected()

    def validate_priority_a(self):
        rows = [r for r in self.registry.prospects() if r['status'] == STATUS_PROPOSED and r['priority'] == 'A']
        if not rows:
            messagebox.showinfo('Agent SDR', 'Aucun prospect priorité A à valider.')
            return
        if not messagebox.askyesno('Valider priorité A', f'Valider {len(rows)} prospect(s) priorité A pour campagne email ?'):
            return
        for row in rows:
            self.registry.mark_validated(row['email'])
        self.refresh()
        messagebox.showinfo('Agent SDR', f'{len(rows)} prospect(s) priorité A validé(s).')

    def create_campaign(self):
        try:
            count = len(self.registry.validated_for_campaign())
            if not count:
                messagebox.showinfo('Campagne email', 'Aucun prospect validé disponible.')
                return
            if not messagebox.askyesno('Créer campagne email', f'Créer une campagne Sales Automation avec {count} prospect(s) validé(s) ?'):
                return
            name, added = create_email_campaign_from_validated(self.registry)
            self.refresh()
            messagebox.showinfo('Campagne créée', f'Campagne créée : {name}\n{added} prospect(s) ajouté(s).\nOuvre DEMARRER pour la prévisualiser puis la lancer.')
        except Exception as exc:
            messagebox.showerror('Campagne email', str(exc))

    def manual_action(self):
        email = self.selected_email()
        if not email:
            messagebox.showerror('Agent SDR', 'Sélectionne ou saisis un prospect.')
            return
        self.registry.mark_manual_action(email)
        self.refresh()
        self.tree.selection_set(email)
        self.load_selected()

    def close(self):
        self.registry.close()
        self.root.destroy()


def main():
    root = tk.Tk()
    SDRAgentApp(root)
    root.mainloop()


if __name__ == '__main__':
    main()
