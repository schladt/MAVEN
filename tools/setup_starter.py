"""Exact, local starter collection review shared by setup presenters."""
from pathlib import Path
import re

from tools.memory.validation import digest, validate_create
from tools.setup_common import SetupPending, read_private


def collection(directory, binding):
    """Read every candidate so one approval binds the entire ordered collection."""
    directory = Path(directory)
    manifest = read_private(directory / 'manifest.json')
    pages, seen = [], set()
    for page in manifest['pages']:
        slug = page['slug']
        if (not re.fullmatch(r'[a-z0-9][a-z0-9-]*', slug) or slug in seen
                or page['file'] != slug + '.json'):
            raise SetupPending('Invalid or duplicate starter artifact path; preserve the original collection.')
        seen.add(slug)
        draft = validate_create(read_private(directory / page['file']))
        if (draft['candidate']['slug'] != slug or digest(draft['candidate']) != page['candidate_hash']
                or any(draft[key] != page[key] for key in ('proposal_id', 'page_id', 'request_key'))):
            raise SetupPending('Starter candidate or original request identity changed; preserve the original artifacts.')
        pages.append(draft)
    if not pages:
        raise SetupPending('The starter collection is empty; no instructions were approved.')
    return {'action': 'submit_and_activate_starter_collection', 'binding': dict(binding), 'pages': pages}


def summary(review, completed=0):
    pages = review['pages']
    lines = [
        'Install MAVEN’s starter instructions', '',
        'These are the bundled instructions agents use for memory, connections, setup, workflows, '
        'skills, secrets, workspace files and optional Google operations.',
        '“Authority” means owner-approved agent instructions, not AWS permissions.', '',
        'One approval authorizes uploading AND activating the exact collection below. '
        'Instructions become available to agents as each page is activated. '
        'This does not create more AWS infrastructure or connect Google/other external accounts.',
        'Local drafts are already prepared. Complete contents are available in Technical details.', '',
        f'{completed} of {len(pages)} pages already completed; remaining pages run automatically in dependency order:',
    ]
    for index, page in enumerate(pages):
        lines.append(f'{index + 1}. {page["candidate"]["title"]}' + (' — completed' if index < completed else ''))
    lines.extend(['', 'You may pause safely. Completed pages are retained and interrupted requests '
                  'resume with their original identifiers. This is not an all-or-nothing cloud transaction: '
                  'a pause or failure can leave some instructions active. Changed contents or a changed '
                  'connection cannot reuse this approval.'])
    return '\n'.join(lines)


def prepare(directory):
    """Generate drafts only, without claiming that the owner approved them."""
    from tools.memory import onboarding
    onboarding.prepare(directory, source_origin='MAVEN local starter draft preparation',
                       source_excerpt='Bundled defaults prepared locally for owner review; '
                                      'submission and activation have not been approved.')
