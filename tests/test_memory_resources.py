"""Offline resource-readiness and reviewed connection boundaries."""
import copy
import hashlib
from io import BytesIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from tools.memory import bootstrap, skills
from tools.memory.client import MemoryClientError
from test_memory_client import CONFIG


def page(slug):
    return {'page_id': str(uuid4()), 'slug': slug, 'page_kind': 'skill',
            'title': slug, 'scope': {}, 'revision': 1, 'content_hash': 'a' * 64,
            'data_scope': 'owner', 'requires': [],
            'bundle': {'version': '1', 'sha256': 'b' * 64, 'size_bytes': 100,
                       'manifest_sha256': 'c' * 64, 'load_policy': 'always'}}


class ResourceReadinessTests(unittest.TestCase):
    def test_wrong_length_download_closes_response_body(self):
        body = BytesIO(b'corrupt')
        s3 = Mock()
        s3.get_object.return_value = {'Body': body, 'ContentLength': 7}
        with patch.object(skills, '_storage', return_value=(s3, 'fixture', 'bundle')):
            with self.assertRaises(MemoryClientError) as error:
                skills._download(Mock(), {'size_bytes': 100})
        self.assertEqual(error.exception.code, 'skill_bundle_invalid')
        self.assertTrue(body.closed)

    def test_unrelated_always_bundle_cannot_block_current_authority(self):
        required, unrelated = page('core'), page('optional')
        client = Mock()
        client.invoke.return_value = {'status': 'ready', 'data': {
            'pages': [required, unrelated], 'authority_generation': 4}}

        def fetch(slug):
            if slug == 'optional':
                raise MemoryClientError('skill_storage_unavailable')
            return {'data': {'slug': slug, 'revision': 1}}

        with tempfile.TemporaryDirectory() as root:
            cache = skills.SessionSkills(client, Path(root), str(uuid4()), str(uuid4()))
            with patch.object(cache, 'fetch', side_effect=fetch):
                result = cache.start(loaded=[required], authority_generation=4)
        self.assertEqual(result['data']['resources'], [{'slug': 'core', 'revision': 1}])
        headers = result['data']['available_skills']['pages']
        self.assertEqual({header['slug']: header['loaded'] for header in headers},
                         {'core': True, 'optional': False})

    def test_changed_or_missing_required_resource_refuses_stale_authority(self):
        required = page('core')
        changed = copy.deepcopy(required)
        changed['bundle']['sha256'] = 'd' * 64
        for generation, pages in ((5, [required]), (4, []), (4, [changed])):
            client = Mock()
            client.invoke.return_value = {'status': 'ready', 'data': {
                'pages': pages, 'authority_generation': generation}}
            with self.subTest(generation=generation, pages=pages), tempfile.TemporaryDirectory() as root:
                cache = skills.SessionSkills(client, Path(root), str(uuid4()), str(uuid4()))
                with patch.object(cache, 'fetch') as fetch:
                    with self.assertRaises(MemoryClientError) as error:
                        cache.start(loaded=[required], authority_generation=4)
                self.assertEqual(error.exception.code, 'context_stale')
                fetch.assert_not_called()


class ReviewedOutputTests(unittest.TestCase):
    def test_apply_rejects_changed_second_output_snapshot_without_rewriting_environment(self):
        template = '{}'
        manifest = {'fixture': 'reviewed'}
        change = {'ChangeSetId': 'change', 'StackId': 'stack', 'ChangeSetName': 'review',
                  'StackName': 'synthetic', 'ExecutionStatus': 'EXECUTE_COMPLETE', 'Changes': []}
        review = {'manifest_sha256': bootstrap._digest(manifest),
                  'template_sha256': hashlib.sha256(template.encode()).hexdigest(),
                  'change_set': bootstrap._review_fields(change),
                  'instance_id': CONFIG['instance_id'], 'recovery_epoch': CONFIG['recovery_epoch']}
        digest = bootstrap._digest(review)

        def stack(config):
            return {'StackStatus': 'UPDATE_COMPLETE', 'Outputs': [
                {'OutputKey': key, 'OutputValue': str(config[field])}
                for key, field in bootstrap.OUTPUT_CONFIG.items() if field in config]}

        for key, value in (('recovery_epoch', str(uuid4())),
                           ('endpoint', 'https://changed123.execute-api.us-east-1.amazonaws.com'),
                           ('journal_bucket', 'changed-synthetic-bucket')):
            changed = {**CONFIG, key: value}
            cloud = Mock()
            cloud.describe_change_set.return_value = change
            cloud.get_template.return_value = {'TemplateBody': json.loads(template)}
            cloud.describe_stacks.side_effect = [
                {'Stacks': [stack(CONFIG)]}, {'Stacks': [stack(changed)]}]
            with self.subTest(field=key), tempfile.TemporaryDirectory() as root:
                environment = Path(root) / '.env'
                original = b'MAVEN_STACK_NAME=synthetic\n'
                environment.write_bytes(original)
                environment.chmod(0o600)
                with patch.object(bootstrap, '_package', return_value=(manifest, template, [])), \
                        patch.object(bootstrap, '_read_private', return_value={
                            'review': review, 'review_sha256': digest}), \
                        patch.object(bootstrap, '_cloud', return_value=cloud):
                    with self.assertRaises(MemoryClientError) as error:
                        bootstrap.apply({'MAVEN_STACK_NAME': 'synthetic'}, root, 'review', environment,
                                        confirm_review=digest)
                self.assertEqual(error.exception.code, 'reviewed_connection_changed')
                self.assertEqual(environment.read_bytes(), original)
                cloud.execute_change_set.assert_not_called()


if __name__ == '__main__':
    unittest.main()
