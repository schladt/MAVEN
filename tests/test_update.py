"""Local update inspection and installer-owned skill upgrades in temporary homes."""
import base64
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from tools import update
from tools.harness import SKILLS, skill_plan, write_skill
from tools.uninstall import local


def _record_hash(data):
    return 'sha256=' + base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b'=').decode()


class LocalUpdateTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        environment = patch.dict(os.environ, {'HOME': str(self.base)})
        environment.start()
        self.addCleanup(environment.stop)
        self.home = self.base / 'home'
        self.home.mkdir(mode=0o700)
        self.checkout = self.base / 'checkout'
        for relative, text in {'install.py': '', 'tools/__init__.py': '', 'tools/kept.py': 'same\n',
                               'tools/changed.py': 'new\n', 'tools/added.py': 'added\n',
                               'maven_resources/__init__.py': '',
                               'maven_resources/templates/skills/maven/SKILL.md': 'skill v2\n'}.items():
            path = self.checkout / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        (self.checkout / 'pyproject.toml').write_text(
            '[tool.setuptools.package-data]\nmaven_resources = ["templates/skills/*/SKILL.md"]\n')
        installed = {'tools/__init__.py': b'', 'tools/kept.py': b'same\n', 'tools/changed.py': b'old\n',
                     'tools/removed.py': b'gone\n', 'maven_resources/__init__.py': b'',
                     'maven_resources/templates/skills/maven/SKILL.md': b'skill v2\n'}
        dist = self.home / 'venv/lib/python3.14/site-packages/maven_cloud_memory-0.1.0.dist-info'
        dist.mkdir(parents=True)
        (dist / 'RECORD').write_text(''.join(f'{name},{_record_hash(data)},{len(data)}\n'
                                             for name, data in installed.items()) +
                                     '../../../bin/maven-mcp,sha256=x,1\ntools/__pycache__/x.pyc,,\n')

    def test_report_lists_changed_added_and_removed_software(self):
        report = update.local_report(self.home, self.checkout)
        self.assertEqual(report['software'], {'installed': True, 'changed': ['tools/changed.py'],
                                              'added': ['tools/added.py'], 'removed': ['tools/removed.py']})
        self.assertFalse(report['current'])

    def test_report_is_current_when_installed_software_matches(self):
        (self.checkout / 'tools/changed.py').write_text('old\n')
        (self.checkout / 'tools/added.py').unlink()
        (self.checkout / 'tools/removed.py').write_text('gone\n')
        self.assertTrue(update.local_report(self.home, self.checkout)['current'])

    def test_owned_unmodified_skill_is_upgraded_and_modified_skill_is_preserved(self):
        roots = [self.base / '.omp/agent/skills']
        for target, body, expected in skill_plan(roots, self.home):
            self.assertIsNone(expected)
            write_skill(target, b'old ' + body, None)
        references = [root / name / 'SKILL.md' for root in roots for name in SKILLS]
        local.record_installation(self.home, self.checkout, references, references)
        plan = skill_plan(roots, self.home)
        self.assertEqual(len(plan), len(SKILLS))
        self.assertTrue(all(expected is not None for _, _, expected in plan))
        target, body, expected = plan[0]
        write_skill(target, body, expected)
        self.assertEqual(target.read_bytes(), body)
        modified = plan[1][0]
        modified.write_bytes(b'owner edit')
        with self.assertRaisesRegex(ValueError, 'preserved'):
            skill_plan(roots, self.home)
        with self.assertRaisesRegex(ValueError, 'changed since review'):
            write_skill(modified, plan[1][1], plan[1][2])
        self.assertEqual(modified.read_bytes(), b'owner edit')

    def test_owned_guidance_block_is_upgraded_and_edited_block_is_preserved(self):
        from tools.harness import _block, _text_change
        path = self.base / 'AGENTS.md'
        old = '\n' + _block('old guidance')
        path.write_text('# Mine\n' + old)
        path.chmod(0o600)
        prior = {'path': str(path), 'format': 'text-block', 'key': 'maven', 'value': old, 'created': False}
        change = _text_change(path, 'new guidance', prior=prior)
        self.assertEqual(change.after.decode(), '# Mine\n\n' + _block('new guidance'))
        self.assertEqual(change.receipt['value'], '\n' + _block('new guidance'))
        path.write_text('# Mine\n' + old.replace('old guidance', 'owner edit'))
        with self.assertRaisesRegex(ValueError, 'differs'):
            _text_change(path, 'new guidance', prior=prior)


class CloudInspectionTests(unittest.TestCase):
    def test_only_data_resource_removal_or_replacement_is_a_risk(self):
        changes = [
            {'Action': 'Modify', 'LogicalResourceId': 'MemoryFunction', 'ResourceType': 'AWS::Lambda::Function',
             'Replacement': 'True'},
            {'Action': 'Modify', 'LogicalResourceId': 'DatabaseCluster', 'ResourceType': 'AWS::RDS::DBCluster',
             'Replacement': 'False'},
            {'Action': 'Modify', 'LogicalResourceId': 'RecoveryBucket', 'ResourceType': 'AWS::S3::Bucket',
             'Replacement': 'Conditional'},
            {'Action': 'Remove', 'LogicalResourceId': 'AdminSecret', 'ResourceType': 'AWS::SecretsManager::Secret'},
        ]
        document = {'review': {'change_set': {'Changes': [{'ResourceChange': change} for change in changes]}}}
        self.assertEqual([row['logical_id'] for row in update.stateful_risks(document)],
                         ['RecoveryBucket', 'AdminSecret'])

    def test_instruction_changes_compare_active_content_hashes(self):
        from tools.memory.onboarding import catalog
        from tools.memory.validation import digest
        candidates, _ = catalog()
        current, changed = candidates[0], candidates[1]
        client = Mock()
        client.invoke.return_value = {'status': 'ready', 'data': {'authority_generation': 4, 'next_after_slug': None, 'pages': [
            {'slug': current['slug'], 'page_id': 'a', 'revision': 2, 'content_hash': digest(current)},
            {'slug': changed['slug'], 'page_id': 'b', 'revision': 5, 'content_hash': '0' * 64}]}}
        client.read_full.return_value = {'data': {**changed, 'body_markdown': 'older body\n',
                                                'candidate_authority_state': changed['authority_state']}}
        changes = {change['slug']: change for change in update.authority_changes(client)}
        self.assertNotIn(current['slug'], changes)
        self.assertEqual((changes[changed['slug']]['action'], changes[changed['slug']]['page_id'],
                          changes[changed['slug']]['revision']), ('update', 'b', 5))
        self.assertIn('-older body', changes[changed['slug']]['diff'])
        self.assertTrue(all(changes[candidate['slug']]['action'] == 'add' for candidate in candidates[2:]))

    def test_bundled_secret_description_reaches_prepared_proposal(self):
        from tools.memory.onboarding import prepare
        expected = ('Store, list, look up and use owner secrets (SSM + encrypted files) by <domain>.<service> '
                    'name via the Home catalog; values never enter chat, argv or memory. Load before handling any credential.')
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'drafts'
            prepare(target, source_origin='owner fixture', source_excerpt='Prepare local review only')
            proposal = json.loads((target / 'maven-secrets.json').read_text())
        self.assertEqual(proposal['candidate']['description'], expected)

    def test_omitted_description_is_preserved_without_spurious_update(self):
        from tools.memory.onboarding import catalog
        from tools.memory.validation import digest
        candidate = next(page for page in catalog()[0] if page['slug'] == 'maven-memory')
        for body_changed in (False, True):
            with self.subTest(body_changed=body_changed):
                published = {**candidate, 'description': 'Owner-approved discovery guidance.'}
                if body_changed:
                    published['body_markdown'] = 'Earlier approved instruction body.\n'
                client = Mock()
                client.invoke.return_value = {'status': 'ready', 'data': {
                    'authority_generation': 4, 'next_after_slug': None, 'pages': [{
                        'slug': candidate['slug'], 'page_id': '10000000-0000-4000-8000-000000000001',
                        'revision': 5, 'content_hash': digest(published)}]}}
                client.read_full.return_value = {'data': {**published, 'candidate_authority_state': 'active'}}
                with patch('tools.memory.onboarding.catalog', return_value=([candidate], {})):
                    changes = update.authority_changes(client)
                client.read_full.assert_called_once_with({'slug': candidate['slug'], 'expected_revision': 5})
                if not body_changed:
                    self.assertEqual(changes, [])
                else:
                    self.assertEqual(changes[0]['candidate']['description'], published['description'])
                    with tempfile.TemporaryDirectory() as directory:
                        target = Path(directory) / 'review'
                        update.prepare_authority_update(target, changes, {
                            'origin': 'owner fixture', 'excerpt': 'Prepare reviewed update'})
                        from tools.setup_starter import collection
                        review = collection(target, {})
                    self.assertEqual(review['pages'][0]['candidate']['description'], published['description'])

    def test_explicit_description_change_has_exact_metadata_diff_with_unchanged_body(self):
        from tools.memory.onboarding import catalog
        from tools.memory.validation import digest
        candidate = next(page for page in catalog()[0] if page['slug'] == 'maven-secrets')
        published = {**candidate, 'description': 'Earlier owner description.'}
        client = Mock()
        client.invoke.return_value = {'status': 'ready', 'data': {
            'authority_generation': 4, 'next_after_slug': None, 'pages': [{
                'slug': candidate['slug'], 'page_id': '10000000-0000-4000-8000-000000000001',
                'revision': 5, 'content_hash': digest(published)}]}}
        client.read_full.return_value = {'data': {**published, 'candidate_authority_state': 'active'}}
        with patch('tools.memory.onboarding.catalog', return_value=([candidate], {})):
            change = update.authority_changes(client)[0]
        self.assertEqual(change['candidate'], candidate)
        self.assertIn('-  "description": "Earlier owner description."', change['diff'])
        self.assertIn('+  "description": ' + json.dumps(candidate['description']), change['diff'])
        self.assertIn('.metadata.json', change['diff'])
