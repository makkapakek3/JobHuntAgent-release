"""Read-only view of existing user-supplied facts; no inferred qualifications."""
import re

from ..config import UserError
from ..models import redact


class ResumeManager:
    def __init__(self, ctx):
        if not hasattr(ctx, 'safe_profile'):
            ctx.load()
        if not ctx.permissions.read_profile:
            raise UserError('PROFILE_READ_DENIED')
        self.profile_version = ctx.profile_version
        self.resume_version = ctx.resume_version
        self.facts = {}
        profile = ctx.safe_profile
        for i, item in enumerate(profile.get('education', [])):
            for key in ('degree', 'major', 'graduation_date'):
                self._add(f'education.{i}.{key}', item.get(key))
        for i, skill in enumerate(profile.get('skills', [])):
            self._add(f'skills.{i}', skill)
        for i, item in enumerate(profile.get('projects', [])):
            for key in ('name', 'role', 'description', 'stack', 'responsibilities'):
                value = item.get(key)
                if isinstance(value, list):
                    for j, text in enumerate(value):
                        self._add(f'projects.{i}.{key}.{j}', text)
                else:
                    self._add(f'projects.{i}.{key}', value)
        for i, paragraph in enumerate(re.split(r'\n\s*\n', profile.get('summary', ''))):
            self._add(f'summary.{i}', paragraph)
        if not self.facts:
            raise UserError('V2_PROFILE_FACTS_EMPTY')

    def _add(self, key, value):
        if isinstance(value, str) and value.strip():
            self.facts[key] = redact(value.strip())
