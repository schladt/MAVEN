"""Session-start taxonomy projections, using only disposable offline stores."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from uuid import uuid4

from tools.home import save_home
from tools.memory.boundary import save_private
from tools.reconcile_local import LocalStore
from tools.reconcile_records import combine_history, profile_id
from tools.turn_memory import LocalApplication, consult


def reference(name, **overrides):
    return {"name": name, "read_when": f"Before working on {name}", "body": "PRIVATE reference body",
            "state": "active", "as_of": "2026-11-27", **overrides}


def index(key, name, origin):
    return {"id": key, "name": name, "read_when": f"Before working on {name}", "origin": origin}


def put(store, kind, data, key=None):
    """Seed an existing current or displace it, including genuine V1 payloads."""
    key = key or str(uuid4())
    token = store.inventory()["token"]
    prior = store.lookup(key, token)
    record = {"id": key, "kind": kind, "data": data,
              "written_at": "2026-11-27T00:00:01Z" if prior else "2026-11-27T00:00:00Z"}
    store.apply([combine_history(record, [prior] if prior else [], "PRIVATE fixture correction")], token,
                strict_current=False)
    return key


class TaxonomyContextTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.instance = self.root / "installation"
        self.instance.mkdir(mode=0o700)
        self.app = LocalApplication(home=self.instance)
        self.addCleanup(self.app.close)
        self.identity = {"instance_id": str(uuid4()), "recovery_epoch": str(uuid4())}
        self.project = self.bind("project")
        self.home = self.bind("topics")
        save_home(self.instance, root=self.home.root, project_id=self.home.project_id, **self.identity,
                  label="Home", consent={"origin": "owner", "excerpt": "Use this fixture as Home"})

    def bind(self, name, key=None):
        root = self.root / name
        root.mkdir(mode=0o700)
        key = key or str(uuid4())
        save_private(root, ".maven/project.json", {"schema": 2, "root": str(root), "project_id": key, **self.identity})
        return LocalStore(root, key)

    def test_scoped_background_preferences_and_skills_in_all_three_workspaces(self):
        key = profile_id(self.home.project_id)
        put(self.home, "profile", {"body": "PRIVATE old background"}, key)
        put(self.home, "profile", {"body": "Current owner background"}, key)
        home_ref = put(self.home, "reference", reference("Home topic"))
        project_ref = put(self.project, "reference", reference("Project topic"))
        put(self.home, "preference", {"scope": "nexus", "title": "Everywhere", "summary": "Global instruction"})
        put(self.home, "setting", {"category": "preference", "scope": "project", "title": "Here",
                                   "summary": "Legacy Home instruction"})
        put(self.home, "setting", {"key": "workspace-status", "value": "PRIVATE machine setting"})
        put(self.project, "preference", {"scope": "project", "title": "Here", "summary": "Project instruction"})
        put(self.home, "skill", {"name": "Home procedure", "when_to_use": "In Home", "steps": ["PRIVATE steps"],
                                 "verification": "PRIVATE verification"})
        for cwd in (self.project.root, self.home.root, self.root):
            with self.subTest(cwd=cwd):
                result = consult(self.app, cwd)
                self.assertEqual(result["status"], "consulted")
                active = result["active"]
                if cwd == self.project.root:
                    home = result["nexus"]
                    self.assertNotIn("profile", active)
                    self.assertNotIn("skills", home)
                    self.assertEqual(active["references"], [index(project_ref, "Project topic", "project")])
                    self.assertEqual([p["summary"] for p in active["preferences"]], ["Project instruction"])
                    self.assertEqual([p["summary"] for p in home["preferences"]], ["Global instruction"])
                else:
                    self.assertEqual(result["nexus"], "same_as_active")
                    home = active
                    self.assertEqual([s["name"] for s in home["skills"]], ["Home procedure"])
                    self.assertCountEqual([p["summary"] for p in home["preferences"]],
                                          ["Global instruction", "Legacy Home instruction"])
                self.assertEqual(home["profile"], {"id": key, "label": "Owner background (not instructions)",
                                                   "body": "Current owner background"})
                self.assertEqual(home["references"], [index(home_ref, "Home topic", "home")])
                self.assertNotIn("PRIVATE", json.dumps(result))

    def test_legacy_malformed_and_displaced_topics_do_not_reach_the_index(self):
        for data in ({"summary": "PRIVATE unconverted V1 reference"}, reference("PRIVATE missing date", as_of=""),
                     reference("PRIVATE invalid date", as_of="2026-02-30"), reference("PRIVATE empty line", read_when=""),
                     reference("PRIVATE too long", body="a" * 7001), reference("PRIVATE extra data", notes="old schema")):
            put(self.home, "reference", data)
        retired = put(self.home, "reference", reference("PRIVATE retired", state="retired"))
        key = put(self.home, "reference", reference("PRIVATE superseded topic"))
        put(self.home, "reference", reference("Current"), key)
        separate = put(self.home, "reference", reference("Another"))
        before = consult(self.app, self.project.root)
        self.assertEqual(before["nexus"]["references"], sorted(
            [index(key, "Current", "home"), index(separate, "Another", "home")], key=lambda r: r["id"]))
        self.assertNotIn("PRIVATE", json.dumps(before))
        put(self.home, "reference", reference("Current", state="retired"), key)
        for cwd in (self.project.root, self.home.root, self.root):
            with self.subTest(cwd=cwd):
                result = consult(self.app, cwd)
                home = result["nexus"] if cwd == self.project.root else result["active"]
                self.assertEqual(home["references"], [index(separate, "Another", "home")])
                self.assertNotIn(retired, json.dumps(result))

    def test_only_configured_home_delivers_profile_even_if_project_uuid_matches(self):
        checkout = self.bind("another-checkout", self.home.project_id)
        key = profile_id(checkout.project_id)
        put(checkout, "profile", {"body": "PRIVATE other checkout"}, key)
        put(self.home, "profile", {"body": "Configured copy"}, key)
        topic = put(checkout, "reference", reference("Checkout"))
        result = consult(self.app, checkout.root)
        self.assertEqual(result["active"]["references"], [index(topic, "Checkout", "project")])
        self.assertNotIn("profile", result["active"])
        self.assertEqual(result["nexus"]["profile"]["body"], "Configured copy")
        self.assertNotIn("PRIVATE", json.dumps(result))

        (self.instance / "home.json").unlink()
        partial = consult(self.app, checkout.root)
        self.assertEqual((partial["status"], partial["error"]), ("partial", "nexus_unavailable"))
        self.assertIn("Owner background is unavailable", partial["next_action"])
        self.assertIsNone(partial["nexus"])
        self.assertEqual(partial["active"]["references"], [index(topic, "Checkout", "project")])
        self.assertNotIn("profile", partial["active"])
        self.assertNotIn("PRIVATE", json.dumps(partial))
