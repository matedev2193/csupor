"""Regression coverage for translated administration and contract-based lists."""
import ast
import os
import re
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

from babel.messages.extract import DEFAULT_KEYWORDS, extract_from_dir
from babel.messages.frontend import parse_mapping_cfg
from babel.messages.pofile import read_po
from flask import g

from app import create_app, db
from app.models import Contract, ContractType, LegalEntity, PlaceOfWork, User, UserPrivilege

ROOT = Path(__file__).resolve().parents[1]


class AdministrationTests(unittest.TestCase):
    def setUp(self):
        with patch.dict(os.environ, {"DATABASE_URL": "sqlite://", "SECRET_KEY": "admin-test-only"}):
            self.app = create_app()
        self.app.config.update(TESTING=True)
        self.context = self.app.app_context()
        self.context.push()
        self.client = self.app.test_client()
        self.users = {}
        for role in UserPrivilege:
            user = User(username=role.value, email=f"{role.value}@example.invalid", password_hash="unused", privilege=role)
            db.session.add(user)
            self.users[role.value] = user
        self.entity = LegalEntity(name="Minta Óvoda", address="Minta cím", om_id="123456", tax_number="12345678901")
        self.place = PlaceOfWork(legal_entity=self.entity, address="Minta telephely")
        today = date.today()
        self.contracts = {}
        for name, start, end in [
            ("active", today - timedelta(days=30), None),
            ("boundary", today, today),
            ("upcoming", today + timedelta(days=1), None),
            ("ended", today - timedelta(days=90), today - timedelta(days=1)),
        ]:
            contract = Contract(user=self.users["employee"], employer=self.entity, place_of_work=self.place,
                                contract_type=ContractType.teacher, job_title=name, working_hours_per_week=40,
                                start_date=start, end_date=end)
            self.contracts[name] = contract
            db.session.add(contract)
        db.session.commit()
        self.login("hr")

    def tearDown(self):
        db.session.remove()
        db.engine.dispose()
        self.context.pop()

    def login(self, role, locale="hu"):
        with self.client.session_transaction() as session:
            session["_user_id"] = str(self.users[role].id)
            session["_fresh"] = True
            session["locale"] = locale
        for key in ("_login_user", "leave_approval_policy", "_flask_babel"):
            g.pop(key, None)

    def get_text(self, url):
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200, url)
        return response.get_data(as_text=True)

    def test_separate_forms_and_legacy_edit_links(self):
        for base, new, field in [("legal-entities", "Jogi személy hozzáadása", 'name="tax_number"'),
                                 ("places-of-work", "Munkahely hozzáadása", 'name="address"')]:
            listing = self.get_text(f"/{base}")
            self.assertIn(f'href="/{base}/new"', listing)
            self.assertNotIn(field, listing)
            form = self.get_text(f"/{base}/new")
            self.assertIn(new, form)
            self.assertIn(field, form)
            self.assertEqual(self.client.get(f"/{base}?edit=1").location, f"/{base}/1/edit")
            self.assertEqual(self.client.get(f"/{base}/99999/edit").status_code, 404)
        self.assertIn("Adószám", self.get_text("/legal-entities"))
        self.assertNotIn("Display format", self.get_text("/places-of-work"))
        self.assertNotIn("Megjelenítési formátum", self.get_text("/places-of-work"))

    def test_new_pages_preserve_manager_permissions(self):
        urls = ["/legal-entities", "/legal-entities/new", "/legal-entities/1/edit",
                "/places-of-work", "/places-of-work/new", "/places-of-work/1/edit",
                "/contracts", "/contracts/new", "/contracts/1/edit"]
        for role in self.users:
            self.login(role)
            for url in urls:
                self.assertEqual(self.client.get(url).status_code, 200 if role in ("ceo", "hr") else 403, (role, url))
                if role not in ("ceo", "hr") and (url.endswith("/edit") or url.endswith("/new") and url != "/contracts/new"):
                    self.assertEqual(self.client.post(url, data={}).status_code, 403)

    def test_legal_entity_create_edit_and_invalid_post(self):
        data = dict(name="Új óvoda", address="Másik cím", om_id="654321", tax_number="87654321-9-01")
        response = self.client.post("/legal-entities/new", data=data, follow_redirects=True)
        self.assertIn("A jogi személy mentve.", response.get_data(as_text=True))
        entity = LegalEntity.query.filter_by(name=data["name"]).one()
        self.assertEqual(entity.tax_number, "87654321901")
        data.update(name="Átnevezett óvoda", om_id="invalid")
        response = self.client.post(f"/legal-entities/{entity.id}/edit", data=data)
        self.assertEqual(response.status_code, 200)
        self.assertIn('value="Átnevezett óvoda"', response.get_data(as_text=True))
        self.assertIn("pontosan 6 számjeggyel", response.get_data(as_text=True))
        db.session.expire_all()
        self.assertEqual(entity.name, "Új óvoda")
        data["om_id"] = "654321"
        self.assertEqual(self.client.post(f"/legal-entities/{entity.id}/edit", data=data).status_code, 302)
        db.session.refresh(entity)
        self.assertEqual(entity.name, data["name"])

    def test_workplace_save_translates_and_rejects_unknown_employer(self):
        response = self.client.post("/places-of-work/new", data={"legal_entity_id": self.entity.id, "address": "Új telephely"}, follow_redirects=True)
        self.assertIn("A munkahely mentve.", response.get_data(as_text=True))
        place = PlaceOfWork.query.filter_by(address="Új telephely").one()
        response = self.client.post(f"/places-of-work/{place.id}/edit", data={"legal_entity_id": 99999, "address": "Megőrzött cím"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("A kiválasztott munkáltató nem létezik.", response.get_data(as_text=True))
        self.assertIn('value="Megőrzött cím"', response.get_data(as_text=True))
        db.session.refresh(place)
        self.assertEqual(place.address, "Új telephely")
        response = self.client.post(f"/places-of-work/{place.id}/edit", data={"legal_entity_id": self.entity.id, "address": "Módosított cím"}, follow_redirects=True)
        self.assertIn("A munkahely frissítve.", response.get_data(as_text=True))
        db.session.refresh(place)
        self.assertEqual(place.address, "Módosított cím")

    def test_contract_list_counts_contracts_and_handles_date_boundaries(self):
        def ids(url):
            return [int(value) for value in re.findall(r'data-contract-id="(\d+)"', self.get_text(url))]
        active = {self.contracts[key].id for key in ("active", "boundary")}
        self.assertEqual(set(ids("/contracts")), active)
        self.assertEqual(set(ids("/contracts?status=invalid")), active)
        for status in ("upcoming", "ended"):
            self.assertEqual(ids(f"/contracts?status={status}"), [self.contracts[status].id])
        all_ids = ids("/contracts?status=all")
        self.assertEqual(len(all_ids), 4)  # One employee, four distinct contracts.
        self.assertEqual(set(all_ids[:2]), active)
        self.assertEqual(all_ids[-1], self.contracts["ended"].id)
        self.assertEqual(ids("/contracts?status=all&q=upcoming"), [self.contracts["upcoming"].id])
        self.assertEqual(ids("/contracts?q=%25"), [])  # Literal %, not a SQL wildcard.
        self.assertEqual(ids("/contracts?legal_entity_id=99999"), [])
        self.assertEqual(ids(f"/contracts?status=all&q={self.contracts['ended'].id}"), [self.contracts["ended"].id])
        chooser = self.get_text("/contracts/new")
        self.assertIn(f'value="{self.users["ceo"].id}"', chooser)  # Has no existing contract.
        self.assertEqual(self.client.get(f"/contracts/new?user_id={self.users['ceo'].id}").location,
                         f"/users/{self.users['ceo'].id}/contracts/new")

    def test_contract_validation_does_not_write_and_retains_input(self):
        contract = self.contracts["active"]
        form = dict(contract_type="Teacher", teacher_classification="Trainee", legal_entity_id=self.entity.id,
                    place_of_work_id=self.place.id, start_date=date.today().isoformat(), job_title="Új munkakör",
                    working_hours_per_week="20")
        for invalid in ({"teacher_classification": "invalid"}, {"working_hours_per_week": "oops"},
                        {"start_date": "not-a-date"}, {"place_of_work_id": "99999"}):
            response = self.client.post(f"/contracts/{contract.id}/edit", data=form | invalid)
            self.assertEqual(response.status_code, 200)
            self.assertIn('value="Új munkakör"', response.get_data(as_text=True))
            db.session.refresh(contract)
            self.assertEqual(contract.job_title, "active")
            self.assertEqual(contract.working_hours_per_week, 40)
        response = self.client.post(f"/contracts/{contract.id}/edit", data=form, follow_redirects=True)
        self.assertIn("employee szerződése frissítve.", response.get_data(as_text=True))
        db.session.refresh(contract)
        self.assertEqual(contract.job_title, "Új munkakör")
        self.assertEqual(contract.contract_type, ContractType.teacher)

    def test_profile_and_contract_labels_in_both_languages(self):
        for locale in ("hu", "en"):
            self.login("hr", locale)
            profile = self.get_text("/profile")
            expected = ["Telefonszám", "Pedagógusigazolvány száma", "Fogyatékosság, tartós betegség"] if locale == "hu" else ["Phone number", "Teacher ID card number", "Disability, long-term illness"]
            for text in expected:
                self.assertIn(text, profile)
            form = self.get_text(f"/contracts/{self.contracts['active'].id}/edit")
            for text in (["Szerződés típusa *", "Besorolási fokozat", "Munkavégzés helye *", "Pedagógus I."] if locale == "hu" else ["Contract type *", "Teacher classification", "Place of work *", "Teacher I"]):
                self.assertIn(text, form)


class TranslationCatalogTests(unittest.TestCase):
    def test_every_extracted_message_has_a_valid_hungarian_translation(self):
        with (ROOT / "app/translations/hu/LC_MESSAGES/messages.po").open() as stream:
            catalog = read_po(stream)
        with (ROOT / "babel.cfg").open() as stream:
            methods, options = parse_mapping_cfg(stream)
        for filename, line, msgid, _comments, _context in extract_from_dir(
            ROOT, method_map=methods, options_map=options, keywords=DEFAULT_KEYWORDS | {"lazy_gettext": None}
        ):
            self.assertIn(msgid, catalog, (filename, line, msgid))
            self.assertTrue(catalog[msgid].string, msgid)
            self.assertNotIn("fuzzy", catalog[msgid].flags, msgid)
        self.assertEqual(list(catalog.check()), [])
        text = "\n".join(m.string for m in catalog if m.id)
        self.assertNotRegex(text, r"(?i)jogi entit|vezérigazgat|\bÖn\b|\bválasszon\b|\badja meg\b|\bhasználja\b")

    def test_flash_literals_are_marked_for_translation(self):
        tree = ast.parse((ROOT / "app/routes.py").read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "flash":
                self.assertNotIsInstance(node.args[0], (ast.Constant, ast.JoinedStr), node.lineno)
